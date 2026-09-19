"""HTTP surface. Thin: authentication from the Bearer token, then Escrowe."""

from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Optional

import pyarrow.ipc as ipc
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_settings
from .engines import EngineError
from .guard import Denied
from .service import AuthError, Escrowe, Principal
from .sources import Source, parse_source_dsn


class LoginBody(BaseModel):
    user: str
    password: str


class SqlBody(BaseModel):
    sql: str


class AskBody(BaseModel):
    question: str


class SourceBody(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    params: dict = {}
    dsn: Optional[str] = None      # alternative to kind+params: a connection string


class NotebookBody(BaseModel):
    cells: list[dict]


# A notebook is its cells: the SQL/question text, how to chart it, and (so
# opening one doesn't re-query or re-invoke the agent) its last result. Only
# these keys ever survive a save - a client sending anything else can't
# persist it - and a cached result is size-capped (see _MAX_RESULT_BYTES).
_NOTEBOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_CELL_KEYS = ("id", "kind", "title", "text", "chart", "result")
_CELL_KINDS = ("sql", "ask", "md")
_MAX_CELLS = 200
_MAX_TEXT = 20_000
# A cell's cached result (its last /sql or /ask response) is saved alongside the
# definition, so opening a notebook shows it instantly with no agent call and no
# re-query - "Run"/"Run all" refreshes it on request. Capped per cell so a huge
# result can't silently balloon the notebook file.
_MAX_RESULT_BYTES = 2_000_000


def create_app(escrowe: Escrowe | None = None, local_operator: bool = False) -> FastAPI:
    """`local_operator=True` is what `escrowe -ui` passes: a request from this
    machine with no bearer token runs as the operator - the account escrowe
    itself connected with - exactly as the local CLI does without a separate
    login. Anything bound beyond loopback (`escrowe serve`) leaves it off, so
    over the network a token is always required."""
    svc = escrowe or Escrowe(load_settings())
    app = FastAPI(title="Escrowe", version="0.1.0")
    app.state.escrowe = svc

    def bearer(authorization: str = Header(default="")) -> str:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        return authorization[7:].strip()

    def is_local(request: Request) -> bool:
        return bool(request.client) and request.client.host in ("127.0.0.1", "::1", "localhost")

    def principal(request: Request, authorization: str = Header(default="")) -> Principal:
        if not authorization.lower().startswith("bearer "):
            if local_operator and is_local(request):
                return svc.operator_principal()
            raise HTTPException(401, "Missing bearer token")
        try:
            return svc.principal(authorization[7:].strip())
        except AuthError as e:
            raise HTTPException(401, str(e))

    def _result(res, fmt: str) -> Response | dict:
        if fmt == "arrow":
            sink = io.BytesIO()
            with ipc.new_stream(sink, res.table.schema) as w:
                w.write_table(res.table)
            return Response(sink.getvalue(), media_type="application/vnd.apache.arrow.stream",
                            headers={"X-Escrowe-SQL": res.sql.replace("\n", " "), "X-Escrowe-Audit": str(res.audit_id)})
        return res.to_dict()

    @app.get("/health")
    def health():
        src = svc.source()
        cached = svc.catalog_for(svc.operator_principal()) or []
        return {"ok": True, "agent": svc.agent.status(),
                "source": src.redacted() if src else None,
                "connected": svc.engine is not None,   # configured (source set) is not the same as connected
                "tables": sorted({t.fqn for t in cached})}

    @app.post("/login")
    def login(body: LoginBody):
        try:
            return svc.login(body.user, body.password)
        except AuthError as e:
            raise HTTPException(401, str(e))

    @app.post("/logout")
    def logout(token: str = Depends(bearer)):
        svc.logout(token)          # closes this session's connection
        return {"ok": True}

    @app.get("/me")
    def me(p: Principal = Depends(principal)):
        return {"user": p.user, "operator": p.session is None}

    @app.get("/metadata")
    def metadata(p: Principal = Depends(principal)):
        from .metadata import Metadata
        try:
            tables = svc.catalog_for(p)
        except AuthError as e:
            raise HTTPException(401, str(e))
        if tables is None:
            return {"tables": [], "text": ""}
        return {"tables": [t.to_dict() for t in tables], "text": Metadata.render(tables)}

    @app.post("/sql")
    def run_sql(body: SqlBody, p: Principal = Depends(principal), format: str = Query("json")):
        try:
            return _result(svc.sql(p, body.sql), format)
        except AuthError as e:
            raise HTTPException(401, str(e))
        except Denied as e:
            raise HTTPException(403, str(e))
        except EngineError as e:
            raise HTTPException(400, str(e))

    @app.post("/ask")
    def ask(body: AskBody, p: Principal = Depends(principal), format: str = Query("json")):
        try:
            return _result(svc.ask(p, body.question), format)
        except AuthError as e:
            raise HTTPException(401, str(e))
        except Denied as e:
            raise HTTPException(403, str(e),
                                headers={"X-Escrowe-Needs-Login": "1"} if getattr(e, "needs_login", False) else None)
        except EngineError as e:
            raise HTTPException(400, str(e))

    @app.get("/sources")
    def list_sources(p: Principal = Depends(principal)):
        return {"sources": [s.redacted() for s in svc.sources()]}

    @app.post("/sources")
    def add_source(body: SourceBody, p: Principal = Depends(principal)):
        try:
            if body.dsn:
                src = parse_source_dsn(body.dsn, name=body.name)
            elif body.kind:
                src = Source(body.name or body.kind, body.kind, body.params)
            else:
                raise ValueError("Provide either a connection string or a type and its details.")
            return svc.set_source(src)
        except (ValueError, EngineError) as e:
            raise HTTPException(400, str(e))

    @app.delete("/sources/{name}")
    def delete_source(name: str, p: Principal = Depends(principal)):
        svc.remove_source(name)
        return {"ok": True}

    @app.get("/audit")
    def audit(p: Principal = Depends(principal), limit: int = 50):
        rows = svc.store.audit_rows(p.user, limit)
        return {"rows": [dict(r) for r in rows]}

    # ------------------------------------------------------------ notebooks
    nb_dir = svc.settings.home / "notebooks"

    def nb_path(name: str) -> Path:
        if not _NOTEBOOK_NAME.match(name):
            raise HTTPException(400, "Notebook names: letters, digits, spaces, . _ - (max 64).")
        return nb_dir / f"{name}.json"

    def clean_cells(cells: list[dict]) -> list[dict]:
        if len(cells) > _MAX_CELLS:
            raise HTTPException(400, f"At most {_MAX_CELLS} cells per notebook.")
        out = []
        for c in cells:
            if not isinstance(c, dict) or c.get("kind") not in _CELL_KINDS:
                raise HTTPException(400, "Each cell needs a kind: sql, ask or md.")
            kept = {k: c[k] for k in _CELL_KEYS if k in c}
            for k in ("id", "title", "text"):
                if k in kept and not isinstance(kept[k], str):
                    raise HTTPException(400, f"Cell {k} must be text.")
            if len(kept.get("text", "")) > _MAX_TEXT:
                raise HTTPException(400, f"A cell's text is limited to {_MAX_TEXT} characters.")
            if "chart" in kept and not isinstance(kept["chart"], dict):
                raise HTTPException(400, "Cell chart config must be an object.")
            if "result" in kept:
                if kept["result"] is not None and not isinstance(kept["result"], dict):
                    raise HTTPException(400, "Cell result must be an object.")
                if len(json.dumps(kept["result"])) > _MAX_RESULT_BYTES:
                    raise HTTPException(400, "A cell's cached result is too large to save "
                                         f"(limit {_MAX_RESULT_BYTES // 1_000_000} MB) - "
                                         "narrow the query or clear its cache before saving.")
            out.append(kept)
        return out

    @app.get("/notebooks")
    def list_notebooks(p: Principal = Depends(principal)):
        items = []
        if nb_dir.is_dir():
            for f in sorted(nb_dir.glob("*.json")):
                try:
                    d = json.loads(f.read_text())
                    items.append({"name": f.stem, "cells": len(d.get("cells", [])),
                                  "saved_at": d.get("saved_at"), "saved_by": d.get("saved_by")})
                except (OSError, ValueError):
                    continue
        return {"notebooks": items}

    @app.get("/notebooks/{name}")
    def get_notebook(name: str, p: Principal = Depends(principal)):
        path = nb_path(name)
        if not path.is_file():
            raise HTTPException(404, f"No notebook named {name!r}.")
        try:
            return json.loads(path.read_text())
        except ValueError:
            raise HTTPException(500, f"Notebook {name!r} is not valid JSON.")

    @app.put("/notebooks/{name}")
    def put_notebook(name: str, body: NotebookBody, p: Principal = Depends(principal)):
        path = nb_path(name)
        doc = {"name": name, "cells": clean_cells(body.cells),
               "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "saved_by": p.user}
        nb_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(path)
        return {"ok": True, "name": name, "cells": len(doc["cells"]), "saved_at": doc["saved_at"]}

    @app.delete("/notebooks/{name}")
    def delete_notebook(name: str, p: Principal = Depends(principal)):
        path = nb_path(name)
        if path.is_file():
            path.unlink()
        return {"ok": True}

    ui_dir = Path(__file__).parent / "static"
    if ui_dir.is_dir():
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(ui_dir / "index.html")

        app.mount("/static", StaticFiles(directory=str(ui_dir)), name="static")

    return app
