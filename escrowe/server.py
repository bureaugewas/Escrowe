"""The HTTP API: a thin layer that turns a bearer token into a Principal and
calls the same `Escrowe` object the CLI uses. Also serves the browser UI."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pyarrow.ipc as ipc
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, llm_login
from .config import load_settings
from .engines import EngineError
from .guard import Denied
from .metadata import render_schema
from .notebooks import NotebookError, Notebooks
from .service import AuthError, Escrowe, Principal
from .sources import Source, parse_dsn

LOOPBACK = ("127.0.0.1", "::1", "localhost")


class LoginBody(BaseModel):
    user: str
    password: str


class SqlBody(BaseModel):
    sql: str


class AskBody(BaseModel):
    question: str


class SourceBody(BaseModel):
    name: str | None = None
    kind: str | None = None
    params: dict = {}
    dsn: str | None = None      # alternative to kind+params


class ConnectBody(BaseModel):
    secret: str | None = None      # password, or a DuckLake token


class LlmBody(BaseModel):
    vendor: str
    method: str                 # browser | api_key
    api_key: str | None = None


class NotebookBody(BaseModel):
    cells: list[dict]
    source: dict | None = None


def create_app(escrowe: Escrowe | None = None, local_operator: bool = False) -> FastAPI:
    """`local_operator=True` (what `escrowe -ui` passes) lets a request from
    this machine with no bearer token run as the operator, exactly as the
    local CLI does. `escrowe serve` leaves it off: over the network a token
    is always required."""
    svc = escrowe or Escrowe(load_settings())
    notebooks = Notebooks(svc.settings.notebooks_dir)
    app = FastAPI(title="Escrowe", version=__version__)
    app.state.escrowe = svc

    def token_from(authorization: str) -> str:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        return authorization[7:].strip()

    def bearer(authorization: str = Header(default="")) -> str:
        return token_from(authorization)

    def principal(request: Request, authorization: str = Header(default="")) -> Principal:
        if not authorization.lower().startswith("bearer "):
            # The real peer address, never a header a remote caller could set.
            if local_operator and request.client and request.client.host in LOOPBACK:
                return svc.operator_principal()
            raise HTTPException(401, "Missing bearer token")
        try:
            return svc.principal(token_from(authorization))
        except AuthError as e:
            raise HTTPException(401, str(e))

    def operator(p: Principal = Depends(principal)) -> Principal:
        """Some things change escrowe itself, not a session: only the operator may."""
        if p.session is not None:
            raise HTTPException(403, "Only the operator can do this.")
        return p

    def run(fn):
        """Map service exceptions to HTTP status codes."""
        try:
            return fn()
        except AuthError as e:
            raise HTTPException(401, str(e))
        except Denied as e:
            headers = {"X-Escrowe-Needs-Login": "1"} if getattr(e, "needs_login", False) else None
            raise HTTPException(403, str(e), headers=headers)
        except (EngineError, ValueError, NotebookError) as e:
            raise HTTPException(400, str(e))

    def render(result, fmt: str):
        if fmt != "arrow":
            return result.to_dict()
        sink = io.BytesIO()
        with ipc.new_stream(sink, result.table.schema) as writer:
            writer.write_table(result.table)
        return Response(sink.getvalue(), media_type="application/vnd.apache.arrow.stream",
                        headers={"X-Escrowe-SQL": result.sql.replace("\n", " "),
                                 "X-Escrowe-Audit": str(result.audit_id)})

    # ------------------------------------------------------------ status

    @app.get("/health")
    def health():
        tables = svc.schema_for(svc.operator_principal()) or []
        return {"ok": True, "agent": svc.agent.status(),
                "source": svc.source.redacted() if svc.source else None,
                "connected": svc.engine is not None,
                "tables": sorted(t.fqn for t in tables)}

    # ---------------------------------------------------------- identity

    @app.post("/login")
    def login(body: LoginBody):
        return run(lambda: svc.login(body.user, body.password))

    @app.post("/logout")
    def logout(token: str = Depends(bearer)):
        svc.logout(token)
        return {"ok": True}

    @app.get("/me")
    def me(p: Principal = Depends(principal)):
        return {"user": p.user, "operator": p.session is None}

    # --------------------------------------------------------------- llm

    @app.get("/llm")
    def llm_status(p: Principal = Depends(principal)):
        return svc.agent.status()

    @app.post("/llm/connect")
    def llm_connect(body: LlmBody, p: Principal = Depends(operator)):
        """An API key is checked and stored here. A browser login is handed to
        the vendor's CLI, which opens the sign-in tab; the agent is rebuilt
        once it finishes, and the client polls /llm to see that happen."""
        v = llm_login.vendor(body.vendor)

        def chosen():
            svc.store.set_setting(llm_login.VENDOR_SETTING, v.name)
            svc.store.set_setting(llm_login.METHOD_SETTING, body.method)

        if body.method == "api_key":
            ok, why = llm_login.check_api_key(v, (body.api_key or "").strip())
            if not ok:
                raise HTTPException(400, f"That key was not accepted: {why}")
            llm_login.save_api_key(svc.store, v, body.api_key)
            chosen()
            svc.reload_agent()
            return svc.agent.status()
        try:
            proc = llm_login.browser_login_detached(v)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        chosen()

        def finish():
            proc.wait()
            svc.reload_agent()
        threading.Thread(target=finish, daemon=True).start()
        return {**svc.agent.status(), "pending": True}

    @app.post("/llm/logout")
    def llm_logout(p: Principal = Depends(operator)):
        v = llm_login.vendor(llm_login.status(svc.store)["vendor"])
        llm_login.forget_api_key(svc.store, v)
        llm_login.browser_logout(v)
        svc.store.set_setting(llm_login.VENDOR_SETTING, "")
        svc.reload_agent()
        return svc.agent.status()

    # ----------------------------------------------------------- queries

    @app.get("/metadata")
    def metadata(p: Principal = Depends(principal)):
        tables = run(lambda: svc.schema_for(p)) or []
        return {"tables": [t.to_dict() for t in tables], "text": render_schema(tables)}

    @app.post("/sql")
    def run_sql(body: SqlBody, p: Principal = Depends(principal), format: str = Query("json")):
        return render(run(lambda: svc.sql(p, body.sql)), format)

    @app.post("/ask")
    def ask(body: AskBody, p: Principal = Depends(principal), format: str = Query("json")):
        return render(run(lambda: svc.ask(p, body.question)), format)

    @app.get("/audit")
    def audit(p: Principal = Depends(principal), limit: int = 50):
        return {"rows": [dict(r) for r in svc.store.audit_rows(p.user, limit)]}

    # ------------------------------------------------------------ source

    @app.get("/sources")
    def get_sources(p: Principal = Depends(principal)):
        return {"sources": svc.saved_sources()}

    @app.post("/sources")
    def set_source(body: SourceBody, p: Principal = Depends(principal)):
        def do():
            if body.dsn:
                src = parse_dsn(body.dsn, name=body.name)
            elif body.kind:
                src = Source(body.name or body.kind, body.kind, body.params)
            else:
                raise ValueError("Provide either a connection string or a kind and its parameters.")
            return svc.set_source(src)
        return run(do)

    @app.post("/sources/{name}/connect")
    def connect_source(name: str, body: ConnectBody, p: Principal = Depends(principal)):
        return run(lambda: svc.connect_saved(name, body.secret))

    @app.delete("/sources/{name}")
    def delete_source(name: str, p: Principal = Depends(principal)):
        svc.remove_source(None if name == "current" else name)
        return {"ok": True}

    # --------------------------------------------------------- notebooks

    @app.get("/notebooks")
    def list_notebooks(p: Principal = Depends(principal)):
        return {"notebooks": notebooks.list()}

    @app.get("/notebooks/{name}")
    def get_notebook(name: str, p: Principal = Depends(principal)):
        doc = run(lambda: notebooks.get(name))
        if doc is None:
            raise HTTPException(404, f"No notebook named {name!r}.")
        return doc

    @app.put("/notebooks/{name}")
    def put_notebook(name: str, body: NotebookBody, p: Principal = Depends(principal)):
        doc = run(lambda: notebooks.save(name, body.cells, body.source, saved_by=p.user))
        return {"ok": True, "name": name, "cells": len(doc["cells"]), "saved_at": doc["saved_at"]}

    @app.delete("/notebooks/{name}")
    def delete_notebook(name: str, p: Principal = Depends(principal)):
        run(lambda: notebooks.delete(name))
        return {"ok": True}

    # ---------------------------------------------------------------- ui

    ui_dir = Path(__file__).parent / "static"
    if ui_dir.is_dir():
        @app.get("/", include_in_schema=False)
        def index():
            # Revalidate every load: after a reinstall the browser must not keep
            # showing the interface it cached from the previous version.
            return FileResponse(ui_dir / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/static", StaticFiles(directory=str(ui_dir)), name="static")

    return app
