"""Command line.

    escrowe                            guided: connect a database, connect Claude, ask questions
    escrowe connect <dsn> --local      machine: connect with one connection string
    escrowe ask "question" --local     machine: one question, JSON out (query + rows)
    escrowe sql "SELECT …" --local     machine: one query, JSON out
    escrowe serve                      run the HTTP server (for remote users and machines)
    escrowe login / shell              session against a server
    escrowe sources / detach / doctor / meta

--local runs in this process against the database on this machine; without it
the commands talk to a server (the one you ran `escrowe login` against, or --dsn).

escrowe connects to exactly one database at a time, directly, with a real
account on it. That account's own grants decide what a person or the agent
can see and do - escrowe adds no access control of its own.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:
    import readline  # noqa: F401  (imported for its side effect: line editing in input())
except ImportError:
    pass  # not available on Windows without pyreadline3

import click
import typer
from rich.console import Console
from rich.table import Table

from . import llm_login
from .client import Connection, EscroweAuthError, EscroweDenied, EscroweError, LocalConnection, Result, connect, embedded
from .config import load_settings

app = typer.Typer(add_completion=False, invoke_without_command=True,
                  help="Escrowe: a blind-agent query gateway for your database.")
console = Console()
SESSION_FILE = Path(os.environ.get("ESCROWE_HOME", Path.home() / ".escrowe")).expanduser() / "session.json"
IDLE_MINUTES = float(os.environ.get("ESCROWE_IDLE_MINUTES", "30"))
BLUE = "#6cb6ff"                       # the escrowe light blue
NAME = f"[bold {BLUE}]Escrowe[/]"


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context,
         ui: bool = typer.Option(False, "-ui", "--ui", help="Open the browser interface instead of the prompt"),
         port: int = typer.Option(8765, help="Port for -ui"),
         ephemeral: bool = typer.Option(False, "--ephemeral", help="Keep nothing on disk; forget everything on exit"),
         version: bool = typer.Option(False, "--version")):
    """`escrowe` opens the prompt. `escrowe -ui` opens the browser interface."""
    if version:
        from . import __version__, build
        console.print(f"escrowe {__version__}  build {build()}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    if ephemeral:
        os.environ["ESCROWE_EPHEMERAL"] = "1"
    if ui:
        browser_ui(port)
    else:
        interactive()


# =========================================================== the guided flow

def interactive() -> None:
    """`escrowe`, with nothing else: sets itself up the first time and drops
    you at a prompt, querying as the account you connected with."""
    from . import __version__, build
    settings = load_settings()
    conn = embedded(settings, operator=True)
    svc = conn.svc
    console.print(f"{NAME} {__version__}   a blind-agent query gateway   "
                  f"[dim]build {build().split()[0]}[/]")
    if os.environ.get("ESCROWE_EPHEMERAL", "").lower() in ("1", "true", "yes"):
        console.print("[yellow]ephemeral: nothing is saved; the connection is forgotten on exit[/]")

    if svc.sources() and svc.engine is not None:
        if not any(True for _ in svc.engine.catalog()):
            console.print(f"[yellow]{svc.source().name} is connected but shows no tables to "
                          f"{svc.source().params.get('user', 'this account')}.[/]")
            if sys.stdin.isatty() and typer.confirm("Set up the database connection again?", default=True):
                conn.detach(svc.source().name)

    # The LLM first: it is the step that makes questions work at all.
    claude_ok = _ensure_claude(svc, interactive=sys.stdin.isatty(), quiet=True)
    if not svc.sources():
        _first_run_database(conn)
    elif svc.engine is None:
        # Connected before, but the password was never kept - ask once now.
        if not _login_saved_connection(conn):
            return

    engine = svc.engine_for(conn._p())
    tables = len({c.fqn for c in engine.catalog()}) if engine else 0
    console.print(f"[dim]{svc.source().name} ({svc.source().kind}) · {tables} tables · "
                  f"claude: {'ready' if claude_ok else 'not connected'}[/]")
    _repl(conn, idle_minutes=IDLE_MINUTES)


def _login_saved_connection(conn: LocalConnection) -> bool:
    """A source was connected in an earlier run; its password was never kept
    (by design), so ask for it once now."""
    src = conn.svc.source()
    stored = src.params.get("user")
    for _ in range(3):
        try:
            user = typer.prompt("Username", default=stored) if stored else typer.prompt("Username")
            conn.login(user, typer.prompt("Password", hide_input=True))
            return True
        except EscroweAuthError as e:
            console.print(f"[red]{e}[/]")
        except (EOFError, KeyboardInterrupt, typer.Abort):
            return False
    return False


def _first_run_database(conn: LocalConnection) -> None:
    from .engines import KINDS
    console.print()
    while True:
        # typer vendors its own click internals here and only auto-shows choices for its
        # private TyperChoice type, not a real click.Choice - so the options are spelled
        # out in the prompt text itself rather than relying on that (silently broken) display.
        kind = typer.prompt(f"Database system [{'/'.join(KINDS)}]",
                            type=click.Choice(list(KINDS)), default=KINDS[0], show_default=False)
        name, params = _ask_connection(kind)
        try:
            with _status("connecting"):
                res = conn.attach(name, kind, params, persist=False)   # credentials are session-only
        except EscroweError as e:
            console.print(f"[red]{e}[/]")
            if typer.confirm("Try again?", default=True):
                continue
            raise typer.Exit(1)
        if not res["tables"]:
            console.print(f"[yellow]connected {res['name']}, but no tables are visible to "
                          f"{params.get('user', 'this account')}.[/] "
                          "Pick a different database, or grant the account access.")
            conn.detach(res["name"])          # never leave a source that shows nothing
            if typer.confirm("Try again?", default=True):
                continue
            raise typer.Exit(1)
        console.print(f"[green]connected[/] {res['name']} · {len(res['tables'])} tables")
        return


def _ask_connection(kind: str) -> tuple[str, dict]:
    if kind == "ducklake":
        return "lake", _ducklake_prompts()
    host = typer.prompt("Host", default="127.0.0.1")
    port = int(typer.prompt("Port", default=3306))
    database = typer.prompt("Database", default="", show_default=False).strip()
    # The username and password are the database's own; its grants decide everything.
    user = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True, default="", show_default=False)
    params = {"host": host, "port": port, "user": user, "password": password}
    if not database:
        database = _choose_database(kind, params) or ""
    if database:
        params["database"] = database
    return kind, params


def _ducklake_prompts() -> dict:
    """DuckLake's catalog always goes through the hosted Quack server: a local
    catalog file can't be shared safely once the server already holds its lock,
    so escrowe never offers to attach one directly."""
    host = typer.prompt("Host:port", default="127.0.0.1:443")
    token = typer.prompt("Quack token", hide_input=True, default="", show_default=False).strip()
    if not token:
        console.print("[yellow]No token entered (input is hidden while typing) - most Quack "
                      "servers reject the attach without one.[/]")
        if typer.confirm("Continue without a token?", default=False):
            pass
        else:
            token = typer.prompt("Quack token", hide_input=True).strip()
    metadata = f"quack:{host}"
    params = {"metadata": metadata}
    if token:
        params["token"] = token
    data_path = typer.prompt("Data path (s3://…, r2://…) "
                             " - only needed the first time this catalog is used",
                             default="", show_default=False).strip()
    if data_path:
        params["data_path"] = data_path
    return params


def _choose_database(kind: str, params: dict) -> str | None:
    """Offer what this account can actually see, rather than asking for a name blind."""
    from .sources import list_databases
    try:
        with _status("looking at what you can see"):
            names = list_databases(kind, params)
    except Exception as e:
        console.print(f"[yellow]Could not list databases ({str(e).splitlines()[0][:120]})[/]")
        return typer.prompt("Database", default="", show_default=False) or None
    if not names:
        console.print(f"[yellow]{params['user']} can see no databases on this server.[/] "
                      "Grant it access, or connect as another account.")
        return typer.prompt("Database", default="", show_default=False) or None
    console.print("Databases you can see:")
    for i, n in enumerate(names, 1):
        console.print(f"  {i}. {n}")
    choice = typer.prompt("Which one", default="1")
    try:
        idx = int(choice)
    except ValueError:
        return choice.strip() or None
    if 1 <= idx <= len(names):
        return names[idx - 1]
    return None


LLM_CHOSEN = "llm_provider"          # what the person picked, not what was detected
LLM_METHOD = "llm_method"


def _ensure_claude(svc, interactive: bool = True, force: bool = False, quiet: bool = False) -> bool:
    """Settle the LLM. Asks unless the person has already chosen and it still works.

    Detecting a credential is not the same as being told to use it: a Claude Code
    login that happens to exist may be the wrong account, or out of credit. So the
    first run always asks, and the choice is remembered.
    """
    chosen = svc.store.setting(LLM_CHOSEN)
    st = llm_login.status(svc.store)
    if chosen and st["connected"] and not force:
        if not quiet:
            console.print(f"[dim]{llm_login.describe(st, svc.store)}[/]")
        return True
    if not interactive:
        console.print("[yellow]No LLM is connected, so questions will not work.[/] "
                      "Start [bold]escrowe[/] on a terminal to connect one.")
        return False
    return _choose_llm(svc)


def _choose_llm(svc) -> bool:
    console.print("\n[bold]Which LLM should escrowe use?[/]")
    console.print("  1. Claude")
    console.print("  2. Not now - only \\sql will work")
    if typer.prompt("Which", default="1").strip() == "2":
        console.print("[yellow]Skipped.[/] Run [bold]escrowe llm[/] when you want to connect one.")
        return False
    svc.store.set_setting(LLM_CHOSEN, "claude")

    console.print("\n[bold]How should escrowe connect to Claude?[/]")
    console.print("  1. Browser login - signs in to your Anthropic account")
    console.print("  2. API key - billed per token")
    method = "api_key" if typer.prompt("Which", default="1").strip() == "2" else "browser"
    svc.store.set_setting(LLM_METHOD, method)
    return _connect_api_key(svc) if method == "api_key" else _connect_subscription(svc)


def _connect_subscription(svc) -> bool:
    """Always performs the login. Being signed in already is not a reason to skip:
    it may be the wrong account, which is exactly what the person is fixing."""
    def ask(cmd) -> bool:
        console.print(f"  Claude Code signs you in. It is not installed yet:\n  [dim]$ {' '.join(cmd)}[/]")
        return typer.confirm("  Install it now?", default=True)

    try:
        if not llm_login.ensure_claude_code(ask):
            console.print(f"[yellow]Skipped.[/] {llm_login.INSTALL_HINT}")
            return False
        console.print("  opening your browser to sign in…")
        llm_login.subscription_login()
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        return False
    svc.reload_agent()
    console.print("[green]Signed in.[/] escrowe will ask Claude through that account.")
    return True


def _connect_api_key(svc) -> bool:
    key = typer.prompt("  API key", hide_input=True, default="", show_default=False).strip()
    if not key:
        return False
    with _status("checking the key"):
        ok, why = llm_login.check_api_key(key)
    if not ok:
        console.print(f"[red]That key was not accepted:[/] {why}")
        return False
    llm_login.save_api_key(svc.store, key)
    svc.reload_agent()
    console.print(f"[green]Claude connected with an API key.[/] [dim]stored in {svc.store.path}, "
                  "readable only by you[/]")
    return True


def browser_ui(port: int = 8765) -> None:
    """`escrowe -ui`: serve the interface and open it, the way `duckdb -ui` does."""
    import threading
    import webbrowser
    import uvicorn
    from .server import create_app
    from .service import Escrowe

    settings = load_settings()
    svc = Escrowe(settings)
    if not svc.sources():
        console.print(f"{NAME}   no data connected yet, let's fix that first\n")
        _first_run_database(LocalConnection(svc, operator=True))
    src = svc.source()
    console.print(f"[dim]log in with your {src.kind} username and password[/]")

    url = f"http://127.0.0.1:{port}"
    console.print(f"{NAME} interface  {url}   (ctrl-c to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(svc), host="127.0.0.1", port=port, log_level="warning")


# ================================================================== the REPL

def _repl(conn, idle_minutes: float = IDLE_MINUTES, prompt: str = None) -> str:
    """DuckDB-style prompt. Returns 'quit' or 'idle'."""
    last: Result | None = None
    pending_feed = False
    last_activity = time.time()
    label = prompt or NAME
    console.print(f"type a question, or \\sql …   (\\help for more, \\q to quit)")
    while True:
        try:
            line = console.input(f"[bold]{label}[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if idle_minutes and time.time() - last_activity > idle_minutes * 60:
            return "idle"
        last_activity = time.time()
        if not line:
            continue
        if line in ("\\q", "\\quit", "exit", "quit"):
            return "quit"
        try:
            if line == "\\help":
                console.print("question text     the agent writes SQL from the schema; you get the rows\n"
                              "\\sql <query>      run SQL yourself, as your own connected account\n"
                              "\\meta             the schema the agent sees\n"
                              "\\audit            recent decisions\n"
                              "\\export <file>    last result → .csv / .parquet / .json\n"
                              "\\json             last result as JSON\n"
                              "\\feed             [experimental] feed the last result to the agent, once, for your next question\n"
                              "\\llmsetup         restart the LLM setup (browser login or API key), from scratch\n"
                              "\\database         disconnect and choose a different database, from scratch\n"
                              "\\q                quit")
            elif line == "\\feed":
                if not last:
                    console.print("[yellow]nothing to feed yet - run a question first.[/]")
                elif typer.confirm(
                        "[experimental] \\feed sends the last result's actual rows to the "
                        "LLM as context for your very next question only. Escrowe normally "
                        "never lets the agent see row data, only the schema - you're about "
                        "to expose data. Are you sure?", default=False):
                    pending_feed = True
                    console.print("[yellow]armed[/] - your next question is answered from the "
                                  "previous result's rows alone (no schema, no new query). "
                                  "Type \\feed again before any later question.")
            elif line == "\\llmsetup":
                if not hasattr(conn, "svc"):
                    console.print("[yellow]Only available with --local; this session is against a server.[/]")
                else:
                    _ensure_claude(conn.svc, force=True)
            elif line == "\\database":
                if not hasattr(conn, "svc"):
                    console.print("[yellow]Only available with --local; this session is against a server.[/]")
                else:
                    existing = conn.svc.source()
                    if existing:
                        conn.detach(existing.name)
                    _first_run_database(conn)
            elif line == "\\meta":
                console.print(conn.metadata()["text"])
            elif line == "\\audit":
                for r in conn.audit(15):
                    console.print(f"[dim]#{r['id']} {r['ts']}[/] {r['user']} {r['mode']} "
                                  f"[bold]{r['decision']}[/] {r.get('reason') or ''} {r.get('candidate_sql') or ''}"[:200])
            elif line == "\\json":
                console.print(last.to_json() if last else "nothing yet")
            elif line.startswith("\\export "):
                if last:
                    _export(last, line.split(None, 1)[1].strip())
                else:
                    console.print("nothing to export yet")
            elif line.startswith("\\sql "):
                last = _show(conn.sql(line[5:])) or last
            else:
                feed_data = _feed_text(last) if pending_feed and last else None
                pending_feed = False              # one-shot: used or not, it doesn't carry forward
                with _status("thinking…") as st:
                    res = conn.ask(line, on_status=lambda t: st and st.update(t + "…"),
                                   feed_data=feed_data)
                last = _show(res) or last
        except EscroweDenied as e:
            console.print(f"[red]DENIED[/] {e}")
            if getattr(e, "needs_login", False) and sys.stdin.isatty():
                if hasattr(conn, "svc") and _ensure_claude(conn.svc, force=True):
                    console.print("[dim]ask again[/]")
        except EscroweAuthError as e:
            console.print(f"[red]AUTH[/] {e}")
            return "idle"
        except EscroweError as e:
            console.print(f"[red]ERROR[/] {e}")


FEED_MAX_ROWS = 50
FEED_MAX_CHARS = 4000


def _feed_text(res: Result) -> str | None:
    """Render the last result compactly for \\feed - capped so a big result
    can't silently blow up the prompt (or the bill). Agent.analyze() adds the
    "question is about these results" lead-in; this is just the data itself."""
    if res.answer and not res.columns:
        return None                      # nothing but prose from the schema; nothing to feed
    header = ", ".join(res.columns)
    lines = [", ".join("" if v is None else str(v) for v in r) for r in res.rows[:FEED_MAX_ROWS]]
    text = f"columns: {header}\n" + "\n".join(lines)
    if res.row_count > FEED_MAX_ROWS:
        text += f"\n... ({res.row_count - FEED_MAX_ROWS} more rows not shown)"
    return text[:FEED_MAX_CHARS]


def _show(res: Result, max_rows: int = 200) -> Result:
    if res.answer:
        # The agent replied from the schema. Nothing was queried, so there is nothing to table.
        console.print(res.answer)
        console.print(f"[dim]from the schema · audit #{res.audit_id}[/]")
        return res
    console.print(f"[dim]sql:[/] {res.sql}")
    t = Table(show_lines=False, header_style="bold cyan")
    for c in res.columns:
        t.add_column(c)
    for r in res.rows[:max_rows]:
        t.add_row(*["" if v is None else str(v) for v in r])
    console.print(t)
    tail = f"{res.row_count} row(s) · {res.duration_ms} ms · audit #{res.audit_id}"
    if res.attempts > 1:
        tail += f" · {res.attempts} attempts"
    console.print(f"[dim]{tail}[/]")
    return res


def _export(res: Result, path: str) -> None:
    if res.answer and not res.columns:
        console.print("the last reply was an answer, not a table")
        return
    t = res.to_arrow()
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq
        pq.write_table(t, path)
    elif path.endswith(".json"):
        Path(path).write_text(json.dumps(res.records(), default=str, indent=1))
    else:
        import pyarrow.csv as pc
        pc.write_csv(t, path)
    console.print(f"wrote {t.num_rows} rows to {path}")


# ========================================================== remote commands

def _open(local: bool = False, dsn: str | None = None, user: str | None = None,
          password: str | None = None, need_login: bool = True):
    """Open the right connection for a command: in-process, an explicit server, or the saved session."""
    user = user or os.environ.get("ESCROWE_USER")
    password = password or os.environ.get("ESCROWE_PASSWORD")
    if local:
        conn = embedded(load_settings(), operator=True)
        src = conn.svc.source()
        if user and password:
            conn.login(user, password)
        elif need_login and src is not None and conn.svc.engine is None:
            # Connected (by a previous `escrowe connect`) but no password was
            # kept, by design - supply it now, once, for this process.
            try:
                u = user or src.params.get("user") or typer.prompt("user")
                p = password or typer.prompt("password", hide_input=True)
            except (EOFError, KeyboardInterrupt, typer.Abort):
                raise EscroweAuthError(
                    "No password on file for this connection; pass --user/--password "
                    "or set ESCROWE_PASSWORD.")
            conn.login(u, p)
        return conn
    if dsn:
        conn = connect(dsn, user, password)
        if need_login and not conn.token:
            raise EscroweAuthError("--dsn needs credentials: escrowe://user:password@host:port")
        return conn
    return _remote()


def _status(message: str):
    """A spinner on a terminal, nothing at all when piped: the live display
    rewrites lines, which erases the prompts printed around it."""
    from contextlib import nullcontext
    return console.status(message) if sys.stdout.isatty() else nullcontext()


def _session() -> dict:
    if not SESSION_FILE.exists():
        console.print("[red]Not logged in.[/] Run: escrowe login")
        raise typer.Exit(1)
    return json.loads(SESSION_FILE.read_text())


def _remote() -> Connection:
    s = _session()
    c = Connection(s["server"])
    c.user, c.token = s["user"], s["token"]
    return c


@app.command()
def llm():
    """Restart the LLM setup: pick Claude again, then browser login or an API
    key again, from scratch. `escrowe` also asks this the first time."""
    conn = embedded(load_settings(), operator=True)
    if not _ensure_claude(conn.svc, force=True):
        raise typer.Exit(1)


@app.command()
def database():
    """Reset the connected database and choose a new one, from scratch.

    Disconnects whatever is connected now (the account's credentials, if any
    were kept, are simply forgotten - see `escrowe connect`) and walks
    through the database-system/host/port/database/username/password
    questions again, the same as the first time you ran `escrowe`.
    """
    conn = embedded(load_settings(), operator=True)
    svc = conn.svc
    existing = svc.source()
    if existing:
        conn.detach(existing.name)
        console.print(f"[dim]disconnected {existing.name}[/]")
    _first_run_database(conn)


@app.command()
def setup(database: bool = typer.Option(True, "--database/--no-database", help="Connect a database"),
          claude: bool = typer.Option(True, "--claude/--no-claude", help="Connect Claude")):
    """Run setup again: connect a database, connect Claude.

    `escrowe` does this automatically the first time. Use this to replace the
    connected database later, or to finish a step you skipped.
    """
    conn = embedded(load_settings(), operator=True)
    svc = conn.svc
    if database:
        existing = svc.source()
        if existing:
            console.print(f"Already connected: [bold]{existing.name}[/] ({existing.kind})")
            if typer.confirm("Replace it?", default=False):
                conn.detach(existing.name)
                _first_run_database(conn)
        else:
            _first_run_database(conn)
    if claude:
        _ensure_claude(svc, force=True)
    console.print("[dim]run [bold]escrowe[/bold] to start asking[/]")


claude_app = typer.Typer(help="Claude specifically: login, status, logout. `escrowe llm` is the general one.")
app.add_typer(claude_app, name="claude")


@claude_app.command("login")
def claude_login():
    """Connect Claude: your subscription, or an API key. `escrowe` also asks this."""
    conn = embedded(load_settings(), operator=True)
    if not _ensure_claude(conn.svc, force=True):
        raise typer.Exit(1)


@claude_app.command("status")
def claude_status():
    """Show how Claude is connected, and how it is billed."""
    store = embedded(load_settings(), operator=True).svc.store
    st = llm_login.status(store)
    console.print(llm_login.describe(st, store))
    if st["source"] == "api_key" and st["api_key_from_env"]:
        console.print("[dim]the key comes from ANTHROPIC_API_KEY in your environment[/]")
    if not st["connected"]:
        console.print("Run [bold]escrowe[/] or [bold]escrowe claude login[/] to connect it.")
    if st["shadowed"]:
        console.print("[yellow]ANTHROPIC_API_KEY is set and wins over the browser login. Unset it to use the profile.[/]")
    if not st["connected"] and not st["ant_installed"]:
        console.print(f"[dim]{llm_login.INSTALL_HINT}[/]")


@claude_app.command("logout")
def claude_logout():
    """Sign out of the subscription and forget any stored API key."""
    store = embedded(load_settings(), operator=True).svc.store
    llm_login.forget_api_key(store)
    llm_login.subscription_logout()
    console.print("Disconnected Claude.")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Start the Escrowe server (reads .env / ESCROWE_* variables)."""
    import uvicorn
    from .server import create_app
    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def login(server: str = typer.Option(None, help="Escrowe server URL"),
          user: str = typer.Option(None, prompt=True), password: str = typer.Option(None, prompt=True, hide_input=True)):
    """Log in to a server with your database account."""
    server = server or load_settings().server_url
    try:
        c = Connection(server, user, password)
    except EscroweError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"server": server, "token": c.token, "user": c.user}))
    console.print(f"Logged in as [bold]{c.user}[/]")


@app.command()
def shell(local: bool = typer.Option(False, "--local", help="Run in this process against the local database"),
          dsn: str = typer.Option(None, help="escrowe://user:pass@host:port")):
    """Interactive session against a server, or --local against this machine."""
    with _open(local=local, dsn=dsn) as conn:
        console.print(f"{NAME} · {conn.user} · {getattr(conn, 'url', 'local')}")
        out = _repl(conn)
        if out == "idle":
            console.print(f"[yellow]Logged out after {IDLE_MINUTES:g} minutes idle.[/] Run: escrowe login")


@app.command()
def ask(question: str, dsn: str = typer.Option(None, help="escrowe://user:pass@host:port (else the saved login)"),
        local: bool = typer.Option(False, "--local", help="Run in this process against the local database"),
        user: str = typer.Option(None), password: str = typer.Option(None),
        as_json: bool = typer.Option(True, "--json/--table")):
    """Ask one question; print the query and the rows as JSON (for scripts and agents)."""
    _one_shot(lambda c: c.ask(question), dsn, local, user, password, as_json)


@app.command()
def sql(query: str, dsn: str = typer.Option(None, help="escrowe://user:pass@host:port (else the saved login)"),
        local: bool = typer.Option(False, "--local", help="Run in this process against the local database"),
        user: str = typer.Option(None), password: str = typer.Option(None),
        as_json: bool = typer.Option(True, "--json/--table")):
    """Run one SQL query as your connected account; print the rows as JSON."""
    _one_shot(lambda c: c.sql(query), dsn, local, user, password, as_json)


def _one_shot(run, dsn, local, user, password, as_json) -> None:
    conn = None
    try:
        conn = _open(local=local, dsn=dsn, user=user, password=password)
        res = run(conn)
    except EscroweDenied as e:
        print(json.dumps({"decision": "denied", "reason": str(e)}))
        raise typer.Exit(2)
    except EscroweError as e:
        print(json.dumps({"decision": "error", "reason": str(e)}))
        raise typer.Exit(1)
    finally:
        if conn is not None:
            conn.close()
    print(res.to_json()) if as_json else _show(res)


@app.command(name="connect")
def connect_cmd(dsn: str = typer.Argument(..., help="Connection string, e.g. mysql://user:pw@host:3306/shop"),
               name: str = typer.Option(None, help="Name for this source in SQL")):
    """Machine setup, string one: open a connection and keep it.

    Then ask with string two:  escrowe ask "how many orders last week"
    """
    from .sources import parse_source_dsn
    conn = embedded(load_settings(), operator=True)
    try:
        src = parse_source_dsn(dsn, name=name)
        # Host/user/database persist so the next process knows what to connect
        # to; the password never does (Source.persisted_json strips it) - a
        # later `escrowe ask --local` supplies it again, or ESCROWE_PASSWORD.
        res = conn.attach(src.name, src.kind, src.params, persist=True)
    except (ValueError, EscroweError) as e:
        print(json.dumps({"connected": False, "reason": str(e)}))
        raise typer.Exit(1)
    print(json.dumps({"connected": True, "source": res["name"], "kind": res["kind"],
                      "tables": res["tables"], "claude": llm_login.status(conn.svc.store)["source"]}))


@app.command()
def attach(dsn: str = typer.Argument(None, help="Connection string, e.g. mysql://user:pw@host:3306/shop"),
           name: str = typer.Option(None, help="Name for this source in SQL"),
           local: bool = typer.Option(False, "--local", help="Attach in this process instead of on a server"),
           server_dsn: str = typer.Option(None, "--dsn", help="escrowe://user:pass@host:port")):
    """Connect a database. With a connection string it is non-interactive (machines);
    without one it asks for the connection details (people)."""
    from .engines import KINDS
    from .sources import parse_source_dsn
    conn = _open(local=local, dsn=server_dsn, need_login=not local)
    if dsn:
        try:
            src = parse_source_dsn(dsn, name=name)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)
        name, kind, params = src.name, src.kind, src.params
    else:
        kind = typer.prompt(f"Database system [{'/'.join(KINDS)}]",
                            type=click.Choice(list(KINDS)), default=KINDS[0], show_default=False)
        name = name or typer.prompt("Name for this source (used as the database name in SQL)", default=kind)
        params = _db_prompts()
    try:
        data = conn.attach(name, kind, params, persist=False)
    except EscroweError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"[green]connected[/] {data['name']} ({data['kind']}) · {len(data['tables'])} tables")
    for t in data["tables"][:40]:
        console.print(f"  {t}")


def _db_prompts() -> dict:
    host = typer.prompt("Host", default="127.0.0.1")
    port = int(typer.prompt("Port", default=3306))
    database = typer.prompt("Database", default="", show_default=False).strip()
    user = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True, default="", show_default=False)
    p = {"host": host, "port": port, "user": user, "password": password}
    if database:
        p["database"] = database
    return p


@app.command()
def sources(local: bool = typer.Option(False, "--local"), dsn: str = typer.Option(None)):
    """Show the connected database (credentials redacted)."""
    data = _open(local=local, dsn=dsn, need_login=not local).sources()
    for s_ in data["sources"]:
        console.print(f"[bold]{s_['name']}[/] ({s_['kind']}) {json.dumps(s_['params'])}")
    if not data["sources"]:
        console.print("[dim]nothing connected; run: escrowe connect <dsn>[/]")


@app.command()
def detach(name: str, local: bool = typer.Option(False, "--local"), dsn: str = typer.Option(None)):
    """Disconnect the database."""
    _open(local=local, dsn=dsn, need_login=not local).detach(name)
    console.print(f"detached {name}")


@app.command()
def doctor():
    """Show exactly what escrowe sees on the connected database.

    Run this when a source connects but reports no tables: it prints the
    database's own catalog probes directly, so the cause is visible rather
    than guessed at.
    """
    conn = embedded(load_settings(), operator=True)
    svc = conn.svc
    if not svc.sources():
        console.print("[dim]nothing connected[/]")
        return
    if svc.engine is None:
        if not _login_saved_connection(conn):
            return
    engine = svc.engine_for(conn._p())
    report = engine.diagnose()
    console.print(f"[bold]sources[/]  {json.dumps(report['sources'])}")
    if report.get("error"):
        console.print(f"[red]{report['error']}[/]")
    for key in ("databases", "tables"):
        value = report.get(key)
        console.print(f"\n[bold {BLUE}]{key}[/]")
        if isinstance(value, str):
            console.print(f"  [red]{value}[/]")
        elif not value:
            console.print("  [dim](nothing)[/]")
        else:
            for row in value[:20]:
                console.print(f"  {' · '.join(str(x) for x in row)}" if isinstance(row, (list, tuple)) else f"  {row}")
            if len(value) > 20:
                console.print(f"  [dim]… {len(value) - 20} more[/]")
    console.print(f"\n[bold]escrowe's catalog[/]  "
                  f"{sorted({c.fqn for c in engine.catalog()})[:20]}")


@app.command(name="llm-log")
def llm_log(limit: int = typer.Option(10, help="How many of the most recent calls to show"),
            full: bool = typer.Option(False, "--full", help="Print the complete prompts, not an extract"),
            path_only: bool = typer.Option(False, "--path", help="Print where the logs live and exit"),
            grep: str = typer.Option(None, help="Only calls whose prompt contains this text")):
    """Show everything escrowe has sent to an LLM.

    Every call is written down as it is sent: the exact system prompt, the exact
    question, and what came back. This is how you verify that no value from your
    data reaches a model. Three copies are kept on disk: the JSONL log this
    command reads (full detail); llm-context.log, the same calls in plain text,
    line by line; and llm-questions.log, just the questions people asked, with
    no schema and no reply, for when even the SQL the agent wrote is more than
    you want kept.
    """
    from .transcript import Transcript, default_context_log_path, default_path, default_questions_log_path
    home = load_settings().home
    t = Transcript(default_path(home))
    if path_only:
        print(t.path)
        print(default_context_log_path(home))
        print(default_questions_log_path(home))
        return
    entries = t.read(0 if grep else limit)
    if grep:
        entries = [e for e in entries
                   if grep.lower() in (e["sent"]["system"] + e["sent"]["user"]).lower()][-limit:]
    if not entries:
        console.print(f"[dim]nothing sent yet · {t.path}[/]")
        return
    for e in entries:
        console.print(f"[bold {BLUE}]{e['ts']}[/] {e.get('user') or '-'} · {e['provider']}/{e['model']} · "
                      f"{e['sent_chars']} chars sent · {e['duration_ms']} ms"
                      + (f" · [red]{e['error']}[/]" if e.get("error") else ""))
        if full:
            console.print("[dim]── system ──[/]"); console.print(e["sent"]["system"])
            console.print("[dim]── user ──[/]"); console.print(e["sent"]["user"])
            console.print("[dim]── received ──[/]"); console.print(str(e.get("received")))
        else:
            console.print(f"  [dim]asked:[/] {(e.get('question') or '')[:100]}")
            console.print(f"  [dim]sent:[/]  {e['sent']['system'][:140].replace(chr(10), ' ')}…")
            console.print(f"  [dim]got:[/]   {str(e.get('received'))[:140].replace(chr(10), ' ')}")
        console.print()
    console.print(f"[dim]{t.path}  ·  --full for complete prompts[/]")


@app.command()
def meta(local: bool = typer.Option(False, "--local"), dsn: str = typer.Option(None),
         user: str = typer.Option(None), password: str = typer.Option(None)):
    """Print the schema the agent sees."""
    console.print(_open(local=local, dsn=dsn, user=user, password=password).metadata()["text"])


if __name__ == "__main__":
    app()
