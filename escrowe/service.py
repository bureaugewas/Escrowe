"""The core of escrowe. One `Escrowe` object holds the connected database,
the agent, the audit store and the login sessions, and exposes two ways to
query: `sql()` for SQL a person wrote, `ask()` for a question the agent
turns into SQL.

`ask()` is where the blind guarantee lives: the agent is handed the schema
and shape feedback only; the Arrow table the engine returns goes straight
back to the caller and never re-enters the agent loop.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

import pyarrow as pa

from . import llm_login
from .agent import Agent, Attempt, HistoryTurn
from .config import Settings
from .engines import REGISTRY, DirectEngine, EngineError
from .engines import build as build_engine
from .guard import Denied, check
from .metadata import TableMeta, read_schema, render_schema
from .sources import Source
from .store import Store
from .tokens import TokenError, issue, verify
from .transcript import Transcript


class AuthError(Exception):
    pass


NOT_CONNECTED = "No database connected. Run `escrowe connect <dsn>` first."


@dataclass
class Principal:
    """Who is asking. `user` is the database account the query runs as. A
    principal without a session is the operator: the account escrowe itself
    connected with, which is what the local CLI runs as."""
    user: str
    session: str | None = None


@dataclass
class Session:
    """A login's own engine, connected as that person, closed on logout."""
    id: str
    user: str
    engine: DirectEngine
    created: float = field(default_factory=time.time)

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            pass


@dataclass
class Answer:
    """The agent replied in words, from the schema. No query ran."""
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
    sql: str                 # exactly what was run
    duration_ms: float
    audit_id: int
    attempts: int = 1
    provider: str | None = None

    def to_dict(self, max_rows: int | None = None) -> dict:
        t = self.table if max_rows is None else self.table.slice(0, max_rows)
        return {"decision": "allowed", "sql": self.sql, "columns": self.table.column_names,
                "rows": [list(r.values()) for r in t.to_pylist()], "row_count": self.table.num_rows,
                "duration_ms": round(self.duration_ms, 1), "audit_id": self.audit_id,
                "attempts": self.attempts, "provider": self.provider}


def login_error_message(e: Exception) -> str:
    """Never hand a caller the raw driver error: it names hosts and databases."""
    text = str(e).lower()
    if any(w in text for w in ("can't connect", "connection", "refused", "timeout")):
        return "The database is not reachable, so the login could not be checked."
    return "Invalid user or password."


def shape_feedback(table: pa.Table) -> str:
    """What a probe gets back: counts only, never values."""
    n = table.num_rows
    if n == 0:
        return "0 rows returned."
    nulls = [f"{name}: {table.column(name).null_count}/{n} null"
             for name in table.column_names if table.column(name).null_count]
    return f"{n} row(s) returned." + (" Null counts - " + ", ".join(nulls) + "." if nulls else "")


class Escrowe:
    HISTORY_MAX_TURNS = 20

    def __init__(self, settings: Settings, store: Store | None = None, agent: Agent | None = None):
        self.settings = settings
        self.store = store or Store(settings.store_path)
        self.secret = self.store.jwt_secret(settings.jwt_secret)
        self.transcript = Transcript(settings.transcript_path)
        self.agent = agent or self._build_agent()
        if self.agent.transcript is None:
            self.agent.transcript = self.transcript

        self.source: Source | None = None
        self.engine: DirectEngine | None = None
        # The schema is read once per engine, when it connects, and cached here
        # keyed by engine identity. Nothing the agent writes can trigger another
        # catalog read.
        self._schemas: dict[int, list[TableMeta]] = {}
        self.sessions: dict[str, Session] = {}
        self._session_lock = threading.Lock()
        # Per-session memory of earlier questions: SQL and shape, never rows
        # (unless the person ran \feed). The operator's own session id groups
        # the local CLI's questions when nobody logged in.
        self._history: dict[str, list[HistoryTurn]] = {}
        self._operator_session_id = secrets.token_hex(8)

        self._restore_saved_source()

    # -------------------------------------------------------------- setup

    def _build_agent(self) -> Agent:
        s = self.settings
        return Agent(s.llm_provider, s.llm_model, api_key=llm_login.stored_api_key(self.store),
                     store=self.store, transcript=self.transcript, thinking_budget=s.llm_thinking_budget)

    def reload_agent(self) -> None:
        """After connecting Claude, pick the new credential up without a restart."""
        self.agent = self._build_agent()

    def _restore_saved_source(self) -> None:
        """A source saved by `escrowe connect` carries no password (see
        Source.persisted_json). It is known but not connected until login()
        supplies one, unless its kind needs no credentials at all."""
        rows = self.store.sources()
        if not rows:
            return
        self.source = Source.from_row(rows[0]["name"], rows[0]["kind"], rows[0]["params"])
        if self.source.kind not in REGISTRY:
            return
        if REGISTRY[self.source.kind].requires_credentials and "password" not in self.source.params:
            return
        try:
            self.engine = self._connect(self.source)
        except EngineError:
            self.engine = None     # e.g. a DuckLake token that was not kept on disk

    def _connect(self, src: Source, credentials: tuple[str, str] | None = None) -> DirectEngine:
        params = dict(src.params)
        if credentials is not None:
            params["user"], params["password"] = credentials
        engine = build_engine(src.kind, **params)
        self._schemas[id(engine)] = read_schema(engine)   # the one and only catalog read
        return engine

    def _close_engine(self) -> None:
        if self.engine is not None:
            self._schemas.pop(id(self.engine), None)
            self.engine.close()
        self.engine = None
        self._history.clear()

    def set_source(self, src: Source, persist: bool = True) -> dict:
        """Connect to a database, replacing whatever was connected before.
        Fails before touching current state if the new one cannot connect."""
        engine = self._connect(src)
        self._close_engine()
        self.engine, self.source = engine, src
        if persist:
            self.store.save_source(src.name, src.kind, src.persisted_json())
        else:
            self.store.clear_sources()
        return {"name": src.name, "kind": src.kind, "tables": [t.fqn for t in self._schemas[id(engine)]]}

    def remove_source(self) -> None:
        self._close_engine()
        self.source = None
        self.store.clear_sources()

    # ------------------------------------------------------------ identity

    def login(self, user: str, password: str) -> dict:
        """Prove who someone is by opening a real connection as them, keep that
        connection as their session, and forget the password."""
        if self.source is None:
            raise AuthError("No database connected yet.")
        try:
            REGISTRY[self.source.kind].test_login(**{**self.source.params, "user": user, "password": password})
        except EngineError as e:
            raise AuthError(login_error_message(e))
        engine = self._connect(self.source, credentials=(user, password))
        del password
        token = issue(self.secret, {"sub": user.lower()}, self.settings.token_ttl_s)
        session_id = verify(self.secret, token)["jti"]
        with self._session_lock:
            self._reap_sessions()
            self.sessions[session_id] = Session(session_id, user.lower(), engine)
        return {"token": token, "sub": user.lower()}

    def logout(self, token: str) -> None:
        try:
            session_id = verify(self.secret, token).get("jti")
        except TokenError:
            return
        with self._session_lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session.close()

    def _reap_sessions(self) -> None:
        cutoff = time.time() - self.settings.token_ttl_s
        for sid in [k for k, v in self.sessions.items() if v.created < cutoff]:
            self.sessions.pop(sid).close()

    def principal(self, token: str) -> Principal:
        try:
            claims = verify(self.secret, token)
        except TokenError as e:
            raise AuthError(str(e))
        return Principal(user=claims["sub"], session=claims.get("jti"))

    def operator_principal(self) -> Principal:
        return Principal(user=(self.source.user if self.source else None) or "local")

    def engine_for(self, principal: Principal) -> DirectEngine | None:
        """The operator (no session) uses the main engine. A principal whose
        session is gone (logged out, expired, or never issued) must fail, not
        fall back to the main engine, which may be a more privileged account."""
        if principal.session is None:
            return self.engine
        session = self.sessions.get(principal.session)
        if session is None:
            raise AuthError("Session has ended; log in again.")
        return session.engine

    def schema_for(self, principal: Principal) -> list[TableMeta] | None:
        """The schema snapshot taken when this principal's engine connected."""
        engine = self.engine_for(principal)
        return self._schemas.get(id(engine)) if engine is not None else None

    # ------------------------------------------------------------- queries

    def sql(self, principal: Principal, sql: str, *, mode: str = "sql", question: str | None = None,
            attempts: int = 1, provider: str | None = None, allow_write: bool = True) -> QueryResult:
        """Run SQL as this principal. `allow_write` is true for a person's own
        SQL; ask() passes false so nothing the agent writes can change data."""
        started = time.time()
        engine = self.engine_for(principal)
        if engine is None:
            raise EngineError(NOT_CONNECTED)

        def audit(decision: str, reason: str | None, rows: int | None, safe_sql: str | None) -> int:
            return self.store.audit(user=principal.user, mode=mode, question=question, candidate_sql=sql,
                                    compiled_sql=safe_sql, decision=decision, reason=reason, row_count=rows,
                                    duration_ms=round((time.time() - started) * 1000, 1), attempts=attempts)

        try:
            safe_sql = check(sql, allow_write=allow_write, dialect=self.source.kind if self.source else None)
        except Denied as e:
            audit("denied", str(e), None, None)
            raise
        try:
            table = engine.execute(safe_sql, self.settings.query_timeout_s)
        except EngineError as e:
            audit("error", str(e), None, safe_sql)
            raise
        if self.settings.max_rows is not None and table.num_rows > self.settings.max_rows:
            table = table.slice(0, self.settings.max_rows)
        audit_id = audit("allowed", None, table.num_rows, safe_sql)
        return QueryResult(table, sql, (time.time() - started) * 1000, audit_id, attempts, provider)

    def ask(self, principal: Principal, question: str, on_status=None,
            feed_data: str | None = None, on_token=None) -> QueryResult | Answer:
        """Question -> agent -> SQL -> engine -> rows to the caller.

        The agent sees the schema, shape feedback and this session's history;
        it never sees the returned table. `on_status` is called with a short
        phrase as the phases change; `on_token` with reply text as it streams.
        With `feed_data`, the question is about a result already in hand
        (\\feed): no schema, no new query, and the fed text is remembered in
        this session's history from then on.
        """
        question = question.strip()
        session_id = principal.session or self._operator_session_id
        history = self._history.get(session_id, [])
        context = {"user": principal.user, "session": session_id}

        if feed_data is not None:
            return self._ask_about_fed_data(principal, question, feed_data, history, context, on_status, on_token)

        # An exact repeat of an earlier question replays its result: no agent
        # call, no new query. That is what re-running a question means.
        for turn in reversed(history):
            if turn.question == question and turn.result is not None:
                return turn.result

        tables = self.schema_for(principal)
        if tables is None:
            raise EngineError(NOT_CONNECTED)
        schema_text = render_schema(tables)
        attempts: list[Attempt] = []
        last_error: Exception | None = None
        for n in range(1, self.settings.agent_attempts + 1):
            if on_status:
                on_status("thinking")
            # A probe's reply is SQL meant for the agent, not the person, so
            # buffer this turn's tokens and only replay them if the turn is
            # the one actually returned.
            buffer: list[str] = []
            proposal = self.agent.propose(question, schema_text, attempts, context=context, history=history,
                                          on_token=buffer.append if on_token else None)
            if proposal.answer:
                for token in buffer if on_token else ():
                    on_token(token)
                audit_id = self.store.audit(user=principal.user, mode="ask", question=question,
                                            decision="answered", reason=proposal.answer[:500], attempts=n)
                answer = Answer(proposal.answer, question, audit_id, proposal.provider, n)
                self._remember(session_id, HistoryTurn(question, shape=f"answered: {proposal.answer[:300]}",
                                                       result=answer))
                return answer
            if not proposal.sql:
                self.store.audit(user=principal.user, mode="ask", question=question,
                                 decision="refused", reason=proposal.refusal, attempts=n)
                raise self._refusal(proposal.refusal or "The agent could not produce a query.", proposal.needs_login)
            if on_status:
                on_status("running the query")
            try:
                result = self.sql(principal, proposal.sql, mode="probe" if proposal.probe else "ask",
                                  question=question, attempts=n, provider=proposal.provider, allow_write=False)
            except (Denied, EngineError) as e:
                last_error = e
                attempts.append(Attempt(proposal.sql, feedback=str(e)))
                continue
            if proposal.probe and n < self.settings.agent_attempts:
                attempts.append(Attempt(proposal.sql, feedback=shape_feedback(result.table)))
                continue
            self._remember(session_id, HistoryTurn(question, sql=result.sql,
                                                   shape=shape_feedback(result.table), result=result))
            return result
        raise Denied(f"No acceptable query after {self.settings.agent_attempts} attempts. Last: {last_error}")

    def _ask_about_fed_data(self, principal, question, feed_data, history, context, on_status, on_token) -> Answer:
        if on_status:
            on_status("thinking")
        proposal = self.agent.analyze(question, feed_data, context=context, history=history, on_token=on_token)
        audit_id = self.store.audit(user=principal.user, mode="feed", question=question,
                                    decision="answered" if proposal.answer else "refused",
                                    reason=(proposal.answer or proposal.refusal or "")[:500], attempts=1)
        if not proposal.answer:
            raise self._refusal(proposal.refusal or "The agent could not answer from the fed data.",
                                proposal.needs_login)
        answer = Answer(proposal.answer, question, audit_id, proposal.provider, 1)
        self._remember(context["session"], HistoryTurn(question, fed=feed_data, result=answer))
        return answer

    @staticmethod
    def _refusal(reason: str, needs_login: bool) -> Denied:
        refusal = Denied(reason)
        refusal.needs_login = needs_login
        return refusal

    def _remember(self, session_id: str, turn: HistoryTurn) -> None:
        turns = self._history.setdefault(session_id, [])
        turns.append(turn)
        del turns[:-self.HISTORY_MAX_TURNS]
