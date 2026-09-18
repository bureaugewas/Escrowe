"""HTTP surface. Thin: authentication from the Bearer token, then Escrowe."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import pyarrow.ipc as ipc
from fastapi import Depends, FastAPI, Header, HTTPException, Query
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


def create_app(escrowe: Escrowe | None = None) -> FastAPI:
    svc = escrowe or Escrowe(load_settings())
    app = FastAPI(title="Escrowe", version="0.1.0")
    app.state.escrowe = svc

    def bearer(authorization: str = Header(default="")) -> str:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        return authorization[7:].strip()

    def principal(token: str = Depends(bearer)) -> Principal:
        try:
            return svc.principal(token)
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
        return {"user": p.user}

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

    ui_dir = Path(__file__).parent / "static"
    if ui_dir.is_dir():
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(ui_dir / "index.html")

        app.mount("/static", StaticFiles(directory=str(ui_dir)), name="static")

    return app
