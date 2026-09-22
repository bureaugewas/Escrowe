"""The `escrowe` command.

    escrowe                         guided: connect a database, connect Claude, ask questions
    escrowe -ui                     the same, in the browser
    escrowe connect [dsn]           connect a database (asks when no dsn is given)
    escrowe disconnect              forget the connected database
    escrowe ask "question"          one question, JSON out
    escrowe sql "SELECT ..."        one query, JSON out
    escrowe meta                    the schema the agent sees
    escrowe claude login|status|logout
    escrowe llm-log                 everything sent to the LLM
    escrowe serve / login / shell   the HTTP server, and sessions against one

`ask`, `sql`, `meta` and `shell` run in this process against the connected
database (--local) or against a server (the saved `escrowe login`, or --dsn).
"""

from __future__ import annotations

import json
import os
import sys

import typer

from .. import __version__, llm_login
from ..client import Connection, EscroweAuthError, EscroweDenied, EscroweError, LocalConnection, connect, embedded
from ..config import home_dir, load_settings
from . import repl, wizard
from .console import BLUE, NAME, console

app = typer.Typer(add_completion=False, invoke_without_command=True, no_args_is_help=False,
                  help="Escrowe: a blind-agent query gateway for your database.")
claude_app = typer.Typer(help="Connect Claude: login, status, logout.")
app.add_typer(claude_app, name="claude")

SESSION_FILE = home_dir() / "session.json"


@app.callback()
def main(ctx: typer.Context,
         ui: bool = typer.Option(False, "-ui", "--ui", help="Open the browser interface instead of the prompt"),
         port: int = typer.Option(8765, help="Port for --ui"),
         version: bool = typer.Option(False, "--version")):
    """`escrowe` opens the prompt; `escrowe -ui` opens the browser interface."""
    if version:
        console.print(f"escrowe {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    browser_ui(port) if ui else interactive()


# ------------------------------------------------------------ guided flows

def interactive() -> None:
    """Set up whatever is missing, then drop into the prompt as the operator."""
    conn = embedded(load_settings(), operator=True)
    svc = conn.svc
    console.print(f"{NAME} {__version__}   a blind-agent query gateway")

    claude_ok = wizard.ensure_claude(svc, interactive=wizard.is_tty(), quiet=True)
    if svc.source is not None and wizard.is_tty() and not wizard.confirm_saved_connection(conn):
        svc = conn.svc
    if svc.source is None:
        wizard.connect_database(conn)
    elif svc.engine is None and not wizard.login_saved_connection(conn):
        return

    tables = conn.metadata()["tables"]
    console.print(f"[dim]{svc.source.name} ({svc.source.kind}) · {len(tables)} tables · "
                  f"claude: {'ready' if claude_ok else 'not connected'}[/]")
    repl.table_overview(tables)
    repl.run(conn)


def browser_ui(port: int = 8765) -> None:
    """`escrowe -ui`: serve the interface on loopback and open it."""
    import threading
    import webbrowser

    import uvicorn

    from ..server import create_app
    from ..service import Escrowe

    svc = Escrowe(load_settings())
    if svc.source is None:
        console.print(f"{NAME}   no database connected yet, let's fix that first\n")
        wizard.connect_database(LocalConnection(svc, operator=True))
    url = f"http://127.0.0.1:{port}"
    console.print(f"{NAME} interface  {url}   (ctrl-c to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Loopback only; requests without a token run as the operator, like the prompt.
    uvicorn.run(create_app(svc, local_operator=True), host="127.0.0.1", port=port, log_level="warning")


# --------------------------------------------------------------- database

@app.command(name="connect")
def connect_cmd(dsn: str = typer.Argument(None, help="e.g. mysql://user:pw@host:3306/shop"),
                name: str = typer.Option(None, help="A name for this source")):
    """Connect a database: non-interactive with a connection string, guided without one.

    Host, user and database are saved; the password never is. A later
    `escrowe ask --local` asks for it again, or reads ESCROWE_PASSWORD.
    """
    from ..sources import parse_dsn
    conn = embedded(load_settings(), operator=True)
    if dsn is None:
        wizard.connect_database(conn, persist=True)
        return
    try:
        src = parse_dsn(dsn, name=name)
        res = conn.set_source(src.name, src.kind, src.params, persist=True)
    except (ValueError, EscroweError) as e:
        print(json.dumps({"connected": False, "reason": str(e)}))
        raise typer.Exit(1)
    print(json.dumps({"connected": True, "source": res["name"], "kind": res["kind"], "tables": res["tables"],
                      "claude": llm_login.status(conn.svc.store)["source"]}))


@app.command()
def disconnect():
    """Forget the connected database."""
    conn = embedded(load_settings(), operator=True)
    src = conn.svc.source
    conn.remove_source()
    console.print(f"disconnected {src.name}" if src else "[dim]nothing was connected[/]")


# ---------------------------------------------------------------- queries

_DSN_HELP = "escrowe://user:pass@host:port (else the saved login)"
_LOCAL_HELP = "Run in this process against the connected database"
_USER_HELP = "Database account (default: the one saved by `escrowe connect`, or ESCROWE_USER)"
_PASSWORD_HELP = "Its password (default: ESCROWE_PASSWORD, else asked for)"


@app.command()
def ask(question: str, dsn: str = typer.Option(None, help=_DSN_HELP),
        local: bool = typer.Option(False, "--local", help=_LOCAL_HELP),
        user: str = typer.Option(None, "--user", "-u", help=_USER_HELP),
        password: str = typer.Option(None, "--password", "-p", help=_PASSWORD_HELP),
        as_json: bool = typer.Option(True, "--json/--table")):
    """Ask one question; print the query and the rows as JSON."""
    _one_shot(lambda c: c.ask(question), dsn, local, user, password, as_json)


@app.command()
def sql(query: str, dsn: str = typer.Option(None, help=_DSN_HELP),
        local: bool = typer.Option(False, "--local", help=_LOCAL_HELP),
        user: str = typer.Option(None, "--user", "-u", help=_USER_HELP),
        password: str = typer.Option(None, "--password", "-p", help=_PASSWORD_HELP),
        as_json: bool = typer.Option(True, "--json/--table")):
    """Run one SQL query as the connected account; print the rows as JSON."""
    _one_shot(lambda c: c.sql(query), dsn, local, user, password, as_json)


@app.command()
def meta(dsn: str = typer.Option(None, help=_DSN_HELP),
         local: bool = typer.Option(False, "--local", help=_LOCAL_HELP),
         user: str = typer.Option(None, "--user", "-u", help=_USER_HELP),
         password: str = typer.Option(None, "--password", "-p", help=_PASSWORD_HELP)):
    """Print the schema the agent sees."""
    console.print(_open(local, dsn, user, password).metadata()["text"])


def _one_shot(run, dsn, local, user, password, as_json) -> None:
    conn = None
    try:
        conn = _open(local, dsn, user, password)
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
    print(res.to_json()) if as_json else repl.show(res)


def _open(local: bool, dsn: str | None, user: str | None, password: str | None):
    """The right connection for a command: in-process, an explicit server, or the saved session."""
    user = user or os.environ.get("ESCROWE_USER")
    password = password or os.environ.get("ESCROWE_PASSWORD")
    if local:
        conn = embedded(load_settings(), operator=True)
        src = conn.svc.source
        if src is not None and conn.svc.engine is None:
            # Saved by `escrowe connect`, but the password was not kept: ask for what is missing.
            try:
                user = user or src.user or typer.prompt("user")
                password = password or typer.prompt("password", hide_input=True)
            except (EOFError, KeyboardInterrupt, typer.Abort):
                raise EscroweAuthError("No password on file; pass --password or set ESCROWE_PASSWORD.")
            conn.login(user, password)
        elif user and password:
            conn.login(user, password)
        return conn
    if dsn:
        conn = connect(dsn, user, password)
        if not conn.token:
            raise EscroweAuthError("--dsn needs credentials: escrowe://user:password@host:port")
        return conn
    if not SESSION_FILE.exists():
        console.print("[red]No server session.[/] Add [bold]--local[/] to query the connected database "
                      "in this process, or sign in to an Escrowe server: [bold]escrowe login --server URL[/]")
        raise typer.Exit(1)
    saved = json.loads(SESSION_FILE.read_text())
    conn = Connection(saved["server"])
    conn.user, conn.token = saved["user"], saved["token"]
    return conn


# ----------------------------------------------------------------- claude

@claude_app.command("login")
def claude_login():
    """Connect Claude: your subscription (browser login) or an API key."""
    conn = embedded(load_settings(), operator=True)
    if not wizard.ensure_claude(conn.svc, force=True):
        raise typer.Exit(1)


@claude_app.command("status")
def claude_status():
    """Show how Claude is connected, and how it is billed."""
    store = embedded(load_settings(), operator=True).svc.store
    st = llm_login.status(store)
    console.print(llm_login.describe(st))
    if st["source"] == "api_key" and st["api_key_from_env"]:
        console.print("[dim]the key comes from ANTHROPIC_API_KEY in your environment[/]")
    if st["shadowed"]:
        console.print("[yellow]ANTHROPIC_API_KEY is set and wins over the browser login. "
                      "Unset it to use the profile.[/]")
    if not st["connected"]:
        console.print("Run [bold]escrowe claude login[/] to connect it.")
        if not st["claude_installed"]:
            console.print(f"[dim]{llm_login.INSTALL_HINT}[/]")


@claude_app.command("logout")
def claude_logout():
    """Sign out of the subscription and forget any stored API key."""
    store = embedded(load_settings(), operator=True).svc.store
    llm_login.forget_api_key(store)
    llm_login.subscription_logout()
    console.print("Disconnected Claude.")


@app.command(name="llm-log")
def llm_log(limit: int = typer.Option(10, help="How many of the most recent calls to show"),
            full: bool = typer.Option(False, "--full", help="Complete prompts, not an extract"),
            questions: bool = typer.Option(False, "--questions", help="Only the questions people asked"),
            context: bool = typer.Option(False, "--context", help="Question / thinking / reply, one line each"),
            grep: str = typer.Option(None, help="Only calls whose prompt contains this text"),
            path_only: bool = typer.Option(False, "--path", help="Print where the log lives and exit")):
    """Show everything escrowe has sent to an LLM.

    Every call is written down as it is sent: the exact system prompt, the
    exact question, and what came back. This is how you verify that no value
    from your data reaches a model.
    """
    from .. import transcript as tr
    t = tr.Transcript(load_settings().transcript_path)
    if path_only:
        print(t.path)
        return
    entries = t.read(0 if grep else limit)
    if grep:
        entries = [e for e in entries if grep.lower() in (e["sent"]["system"] + e["sent"]["user"]).lower()][-limit:]
    if not entries:
        console.print(f"[dim]nothing sent yet · {t.path}[/]")
        return
    if questions:
        print("\n".join(tr.questions(entries)))
        return
    if context:
        print("\n".join(tr.context_lines(entries)))
        return
    for e in entries:
        console.print(f"[bold {BLUE}]{e['ts']}[/] {e.get('user') or '-'} · {e['provider']}/{e['model']} · "
                      f"{e['sent_chars']} chars sent · {e['duration_ms']} ms"
                      + (f" · [red]{e['error']}[/]" if e.get("error") else ""))
        if full:
            for label, text in (("system", e["sent"]["system"]), ("user", e["sent"]["user"]),
                                ("received", str(e.get("received")))):
                console.print(f"[dim]── {label} ──[/]")
                console.print(text)
        else:
            console.print(f"  [dim]asked:[/] {(e.get('question') or '')[:100]}")
            console.print(f"  [dim]sent:[/]  {e['sent']['system'][:140].replace(chr(10), ' ')}…")
            console.print(f"  [dim]got:[/]   {str(e.get('received'))[:140].replace(chr(10), ' ')}")
        console.print()
    console.print(f"[dim]{t.path}  ·  --full for complete prompts, --questions or --context for narrower views[/]")


# ----------------------------------------------------------------- server

@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Start the HTTP server. Needs ESCROWE_API_ENABLED=1."""
    if not load_settings().api_enabled:
        console.print("[yellow]The HTTP API is off by default.[/] Set [bold]ESCROWE_API_ENABLED=1[/] to turn it on.")
        raise typer.Exit(1)
    import uvicorn

    from ..server import create_app
    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def login(server: str = typer.Option(None, help="Escrowe server URL"),
          user: str = typer.Option(None, prompt=True),
          password: str = typer.Option(None, prompt=True, hide_input=True)):
    """Sign in to an Escrowe server with your database account; later commands reuse the session.

    Not needed for --local commands. For Claude, use `escrowe claude login`.
    """
    server = server or load_settings().server_url
    try:
        conn = Connection(server, user, password)
    except EscroweError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"server": server, "token": conn.token, "user": conn.user}))
    console.print(f"Logged in as [bold]{conn.user}[/]")


@app.command()
def shell(local: bool = typer.Option(False, "--local", help=_LOCAL_HELP),
          dsn: str = typer.Option(None, help=_DSN_HELP)):
    """The interactive prompt against a server, or --local against this machine."""
    with _open(local, dsn, None, None) as conn:
        console.print(f"{NAME} · {conn.user} · {getattr(conn, 'url', 'local')}")
        if repl.run(conn) == "idle":
            console.print(f"[yellow]Logged out after {repl.IDLE_MINUTES:g} minutes idle.[/] Run: escrowe login")


if __name__ == "__main__":
    sys.exit(app())
