"""The Escrowe service: connect to one database directly, and let a person or
an agent query it as themselves. escrowe adds no access control of its own -
the account you connect with is the entire access decision, exactly as it
would be with any other client of that database.

This is also the seam that enforces the blind guarantee: `ask()` hands the
agent metadata and shape feedback only; the Arrow table from the engine goes
straight back to the caller and never re-enters the agent loop.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field

import pyarrow as pa

from .agent import Agent, Attempt, HistoryTurn
from .auth import AuthError as VerifyError, build_verifier
from .config import Settings
from .engines import REGISTRY, EngineError, build as build_engine
from .guard import Denied, check
from .metadata import Metadata, TableMeta
from .sources import Source
from .store import Store
from .transcript import Transcript, default_path
from .tokens import TokenError, issue, verify


class AuthError(Exception):
    pass


@dataclass
class Principal:
    """Who is asking. There are no roles: `user` is the account the query
    actually runs as, whether that's the one escrowe connected with at
    startup or one a person logged in with of their own."""
    user: str
    token: str | None = None
    session: str | None = None


@dataclass
class Session:
    """A login's own engine, connected as that person, closed when they log out."""
    id: str
    user: str
    engine: object
    created: float = field(default_factory=time.time)

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            pass


@dataclass
class Answer:
    """The agent answered in words, from the schema. No query ran, so no data moved."""
    text: str
    question: str
    audit_id: int
    provider: str | None = None
    attempts: int = 1

    def to_dict(self, max_rows: int | None = None) -> dict:
        return {"decision": "answered", "answer": self.text, "question": self.question,
                "sql": None, "columns": [], "rows": [], "row_count": 0,
                "audit_id": self.audit_id, "provider": self.provider, "attempts": self.attempts}


@dataclass
class QueryResult:
    table: pa.Table
    sql: str                 # the SQL the user (or agent) wrote, unchanged
    duration_ms: float
    audit_id: int
    attempts: int = 1
    provider: str | None = None
    note: str | None = None  # a short caveat the agent added after finalizing this query

    def to_dict(self, max_rows: int | None = None) -> dict:
        t = self.table if max_rows is None else self.table.slice(0, max_rows)
        return {
            "decision": "allowed", "sql": self.sql, "columns": self.table.column_names,
            "rows": [list(r.values()) for r in t.to_pylist()], "row_count": self.table.num_rows,
            "duration_ms": round(self.duration_ms, 1), "audit_id": self.audit_id,
            "attempts": self.attempts, "provider": self.provider, "note": self.note,
        }


class Escrowe:
    def __init__(self, settings: Settings, store: Store | None = None, agent: Agent | None = None):
        self.settings = settings
        self.store = store or Store(settings.store_path)
        self.secret = self.store.jwt_secret(settings.jwt_secret)
        self._source: Source | None = self._load_source()
        # A source loaded from disk never carries a password (see
        # Source.persisted_json), so it's known but not yet connected until
        # login() supplies one - that's what makes the machine two-command
        # flow work without ever writing a password to disk. A kind with no
        # per-account identity of its own (DuckLake) has no password to wait
        # for, so it connects immediately.
        connectable = self._source is not None and (
            self._source.params.get("password") is not None
            or not REGISTRY[self._source.kind].requires_credentials)
        # The catalog (table/column names, types, comments, row-count estimates)
        # is read once, right here, and never again: it is the only thing the
        # agent is ever shown, and the queries that produce it are fixed
        # (see engines/*.py catalog()/table_sizes()) and out of the agent's
        # reach - nothing it writes can trigger another catalog read.
        self._catalog_cache: dict[int, list[TableMeta]] = {}
        self.engine = self._connect(self._source) if connectable else None
        # A fixed-length id for the log, even when nobody logged in: the local
        # CLI's operator mode has no JWT session, but every exchange still
        # needs one consistent id to group it in the transcript.
        self._operator_session_id = secrets.token_hex(8)
        from . import llm_login
        self.transcript = Transcript(default_path(settings.home))
        self.agent = agent or Agent(settings.llm_provider, settings.llm_model,
                                    api_key=llm_login.stored_api_key(self.store), store=self.store,
                                    transcript=self.transcript, thinking_budget=settings.llm_thinking_budget)
        if agent is not None and getattr(agent, "transcript", None) is None:
            agent.transcript = self.transcript
        self.sessions: dict[str, Session] = {}
        self._session_lock = threading.Lock()
        # Per-session conversation memory: earlier questions, their SQL, and their
        # shape (never rows, unless \feed explicitly shared one - see ask()).
        # Keyed by session id so a server process with several logins keeps each
        # person's history separate.
        self._history: dict[str, list[HistoryTurn]] = {}

    def _load_source(self) -> Source | None:
        rows = self.store.sources()
        if rows:
            r = rows[0]
            return Source(r["name"], r["kind"], json.loads(r["params"]))
        if self.settings.attachments:
            return Source.from_attachment(self.settings.attachments[0])
        return None

    # Params an older escrowe (with the since-removed escrowe/passthrough/direct
    # modes) may have written to a store this process is now reopening.
    _LEGACY_PARAMS = ("dsn", "mode")

    def _connect(self, src: Source, credentials: tuple[str, str] | None = None):
        p = {k: v for k, v in src.params.items() if k not in self._LEGACY_PARAMS}
        if credentials is not None:
            p["user"], p["password"] = credentials
        engine = build_engine(src.kind, **p)
        self._catalog_cache[id(engine)] = Metadata(engine).all_tables()   # read once, here only
        return engine

    # -------------------------------------------------------------- source
    def source(self) -> Source | None:
        return self._source

    def sources(self) -> list[Source]:
        """Kept plural for callers written against the old multi-source shape;
        escrowe connects to exactly one database at a time."""
        return [self._source] if self._source else []

    def set_source(self, src: Source, persist: bool = True) -> dict:
        """Connect to a database, replacing whatever was configured before."""
        engine = self._connect(src)               # fail before touching current state; snapshots the catalog once
        if self.engine is not None:
            self._catalog_cache.pop(id(self.engine), None)
            self.engine.close()
        self.engine = engine
        self._source = src
        self.store.clear_sources()
        if persist:
            self.store.save_source(src.name, src.kind, src.persisted_json())
        self._history.clear()          # a different database makes old SQL/shape history stale
        tables = sorted({t.fqn for t in self._catalog_cache[id(engine)]})
        return {"name": src.name, "kind": src.kind, "tables": tables}

    # kept as an alias: cli/client code was written against add_source/remove_source
    add_source = set_source

    def remove_source(self, name: str) -> None:
        if self._source and self._source.name == name:
            if self.engine is not None:
                self._catalog_cache.pop(id(self.engine), None)
                self.engine.close()
            self.engine, self._source = None, None
            self._history.clear()
        self.store.delete_source(name)

    # ------------------------------------------------------------- identity
    def login(self, user: str, password: str) -> dict:
        """Verify the person against the connected database, open their own
        connection, forget the password. Not needed for the common single-user
        case, where escrowe already connected with the setup credentials."""
        if self._source is None:
            raise AuthError("No database connected yet.")
        try:
            identity = build_verifier(self._source).verify(user, password)
        except VerifyError as e:
            raise AuthError(str(e))
        engine = self._connect(self._source, credentials=(user, password))
        password = None                                     # nothing keeps a copy
        token = issue(self.secret, {"sub": identity.user}, self.settings.token_ttl_s)
        sid = verify(self.secret, token)["jti"]
        with self._session_lock:
            self._reap_sessions()
            self.sessions[sid] = Session(sid, identity.user, engine)
        return {"token": token, "sub": identity.user}

    def reload_agent(self) -> None:
        """After connecting Claude, pick the new credential up without a restart."""
        from . import llm_login
        self.agent = Agent(self.settings.llm_provider, self.settings.llm_model,
                           api_key=llm_login.stored_api_key(self.store), store=self.store,
                           transcript=self.transcript, thinking_budget=self.settings.llm_thinking_budget)

    def logout(self, token: str) -> None:
        try:
            sid = verify(self.secret, token).get("jti")
        except TokenError:
            return
        with self._session_lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.close()

    def _reap_sessions(self) -> None:
        cutoff = time.time() - self.settings.token_ttl_s
        for sid in [k for k, v in self.sessions.items() if v.created < cutoff]:
            self.sessions.pop(sid).close()

    def principal(self, token: str) -> Principal:
        try:
            claims = verify(self.secret, token)
        except TokenError as e:
            raise AuthError(str(e))
        return Principal(user=claims["sub"], token=token, session=claims.get("jti"))

    def operator_principal(self) -> Principal:
        """The account escrowe itself connected with at startup - what the
        local CLI queries as when nobody has logged in separately."""
        user = (self._source.params.get("user") if self._source else None) or "local"
        return Principal(user=user)

    def engine_for(self, principal: Principal):
        """A principal with no session at all is the local CLI's own operator
        mode (never logged in separately) - that's the main engine, correctly.
        A principal that DOES carry a session id but it's gone from
        self.sessions means a real login token whose session was revoked
        (/logout) or reaped (expired) - that must fail, not silently fall
        back to the main engine, which would run the request as a different,
        possibly more privileged, identity than the one that was logged out."""
        if principal.session is None:
            return self.engine
        s = self.sessions.get(principal.session)
        if s is None:
            raise AuthError("Session has ended; log in again.")
        return s.engine

    def catalog_for(self, principal: Principal) -> list[TableMeta] | None:
        """The schema snapshot taken once when this principal's engine was
        connected (see `_connect`) - never re-queried, so nothing an agent
        writes can ever trigger another catalog read."""
        engine = self.engine_for(principal)
        return self._catalog_cache.get(id(engine)) if engine is not None else None

    # -------------------------------------------------------------- queries
    def sql(self, principal: Principal, sql: str, mode: str = "sql", question: str | None = None,
            attempts: int = 1, provider: str | None = None, allow_write: bool = True) -> QueryResult:
        """`allow_write` defaults to true because this is the path a person uses.
        `ask()` sets it false, so no agent-written statement can change anything."""
        start = time.time()
        engine = self.engine_for(principal)
        if engine is None:
            raise EngineError("No database connected. Run `escrowe connect ...` first.")
        try:
            safe_sql = check(sql, allow_write=allow_write,
                             dialect=self._source.kind if self._source else "mysql")
        except Denied as e:
            self._audit(principal, mode, question, sql, None, "denied", str(e), None, start, attempts)
            raise
        try:
            table = engine.execute(safe_sql, self.settings.query_timeout_s)
        except EngineError as e:
            self._audit(principal, mode, question, sql, safe_sql, "error", str(e), None, start, attempts)
            raise
        except Exception as e:
            msg = _clean_engine_error(e)
            self._audit(principal, mode, question, sql, safe_sql, "error", msg, None, start, attempts)
            raise EngineError(msg)
        dur = (time.time() - start) * 1000
        aid = self._audit(principal, mode, question, sql, safe_sql, "allowed", None, table.num_rows, start, attempts)
        return QueryResult(table, sql, dur, aid, attempts, provider)

    HISTORY_MAX_TURNS = 20

    def _remember(self, session_id: str, turn: HistoryTurn) -> None:
        h = self._history.setdefault(session_id, [])
        h.append(turn)
        del h[:-self.HISTORY_MAX_TURNS]

    def ask(self, principal: Principal, question: str, on_status=None,
            feed_data: str | None = None, on_token=None) -> QueryResult:
        """Text -> agent -> SQL -> engine -> rows to the caller.
        The agent sees the schema, shape feedback, and this session's own history
        (earlier questions/SQL/shape - never rows); it never sees `result.table` -
        unless `feed_data` is given (the experimental \\feed mode, warned about at the
        prompt), in which case this skips straight to Agent.analyze: a follow-up
        question about a result already in hand, answered from that data alone, no
        schema, no new query. A \\feed turn's shared data is remembered from then on
        too - the one deliberate, person-approved exception to shape-only memory.
        `on_status`, if given, is called with "thinking" while the agent is composing
        a query and "running the query" once one is about to execute."""
        session_id = principal.session or self._operator_session_id
        history = self._history.get(session_id, [])
        if feed_data is not None:
            if on_status:
                on_status("thinking")
            proposal = self.agent.analyze(question, feed_data,
                                          context={"user": principal.user, "session": session_id},
                                          history=history, on_token=on_token)
            aid = self.store.audit(user=principal.user, mode="feed", question=question,
                                   decision="answered" if proposal.answer else "refused",
                                   reason=(proposal.answer or proposal.refusal or "")[:500], attempts=1)
            if not proposal.answer:
                refusal = Denied(proposal.refusal or "The agent could not answer from the fed data.")
                refusal.needs_login = proposal.needs_login
                raise refusal
            self._remember(session_id, HistoryTurn(question=question, fed=feed_data))
            return Answer(proposal.answer, question, aid, proposal.provider, 1)
        tables = self.catalog_for(principal)
        if tables is None:
            raise EngineError("No database connected. Run `escrowe connect ...` first.")
        schema_text = Metadata.render(tables)
        attempts: list[Attempt] = []
        last_error: Exception | None = None
        for n in range(1, self.settings.agent_attempts + 1):
            if on_status:
                on_status("thinking")
            proposal = self.agent.propose(question, schema_text, attempts,
                                          context={"user": principal.user, "session": session_id},
                                          history=history, on_token=on_token)
            if proposal.answer:
                aid = self.store.audit(user=principal.user, mode="ask", question=question,
                                       decision="answered", reason=proposal.answer[:500], attempts=n)
                self._remember(session_id, HistoryTurn(question=question,
                                                       shape=f"answered: {proposal.answer[:300]}"))
                return Answer(proposal.answer, question, aid, proposal.provider, n)
            if not proposal.sql:
                self.store.audit(user=principal.user, mode="ask", question=question,
                                 decision="refused", reason=proposal.refusal, attempts=n)
                refusal = Denied(proposal.refusal or "The agent could not produce a query.")
                refusal.needs_login = proposal.needs_login
                raise refusal
            if on_status:
                on_status("running the query")
            try:
                result = self.sql(principal, proposal.sql, mode="ask", question=question, attempts=n,
                                  provider=proposal.provider, allow_write=False)
            except (Denied, EngineError) as e:
                last_error = e
                attempts.append(Attempt(proposal.sql, feedback=str(e)))   # shape feedback only
                continue
            if proposal.probe and n < self.settings.agent_attempts:
                # The agent asked to check this one before trusting it: it gets the shape
                # (row count, which columns came back all-NULL), never the rows, and one
                # more turn to decide whether to finalize, refine, or probe again.
                attempts.append(Attempt(proposal.sql, feedback=_shape_feedback(result.table)))
                continue
            result.note = proposal.note
            self._remember(session_id, HistoryTurn(question=question, sql=result.sql,
                                                   shape=_shape_feedback(result.table)))
            return result
        raise Denied(f"No acceptable query after {self.settings.agent_attempts} attempts. Last: {last_error}")

    def _audit(self, p: Principal, mode, question, sql, compiled_sql, decision, reason, rows, start, attempts) -> int:
        return self.store.audit(user=p.user, mode=mode, question=question, candidate_sql=sql,
                                compiled_sql=compiled_sql, decision=decision, reason=reason, row_count=rows,
                                duration_ms=round((time.time() - start) * 1000, 1), attempts=attempts)


def _clean_engine_error(e: Exception) -> str:
    msg = str(e).split("\n")[0]
    return msg[:300]


def _shape_feedback(table: pa.Table) -> str:
    """Counts only, never values - what a probe gets back. Free: Arrow already
    knows null_count per column from the rows it just fetched, no extra query."""
    n = table.num_rows
    if n == 0:
        return "0 rows returned."
    nulls = [f"{name}: {table.column(name).null_count}/{n} null"
            for name in table.column_names if table.column(name).null_count]
    text = f"{n} row(s) returned."
    if nulls:
        text += " Null counts - " + ", ".join(nulls) + "."
    return text
