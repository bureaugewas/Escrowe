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
import re
import sys
import time
from pathlib import Path

try:
    import readline  # noqa: F401  (imported for its side effect: line editing in input())
except ImportError:
    pass  # not available on Windows without pyreadline3

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import llm_login
from .client import Connection, EscroweAuthError, EscroweDenied, EscroweError, LocalConnection, Result, connect, embedded
from .config import load_settings

app = typer.Typer(add_completion=False, invoke_without_command=True,
                  help="Escrowe: a blind-agent query gateway for your database.")
console = Console()
SESSION_FILE = Path(os.environ.get("ESCROWE_HOME", Path.home() / ".escrowe")).expanduser() / "session.json"
IDLE_MINUTES = float(os.environ.get("ESCROWE_IDLE_MINUTES", "30"))
BLUE = "bright_blue"                   # the escrowe blue - a named ANSI color, not a custom
                                       # truecolor hex: some terminals don't advertise
                                       # truecolor support and downsample an arbitrary RGB
                                       # value unpredictably, but every terminal that does
                                       # color at all recognizes the 16 standard names exactly.
NAME = f"[bold {BLUE}]Escrowe[/]"
CMD_STYLE = "blue"                     # the plain (non-bright) variant - inherently darker
                                       # than BLUE above - the \command column in \help
PLACEHOLDER_STYLE = "magenta"          # <query>/<file>/<question> within a \command - a
                                       # named ANSI color, not a truecolor hex, same
                                       # reasoning as BLUE/CMD_STYLE above
_PLACEHOLDER = re.compile(r"(<[^>]+>)")


def _style_cmd(cmd: str) -> str:
    """A \\command string with any <placeholder> tokens picked out in their
    own color - built as separate, non-overlapping markup spans rather than
    one nested inside the other, so there's no ambiguity about which [/]
    closes which."""
    parts, last = [], 0
    for m in _PLACEHOLDER.finditer(cmd):
        if m.start() > last:
            parts.append(f"[{CMD_STYLE}]{cmd[last:m.start()]}[/]")
        parts.append(f"[{PLACEHOLDER_STYLE}]{m.group(1)}[/]")
        last = m.end()
    if last < len(cmd):
        parts.append(f"[{CMD_STYLE}]{cmd[last:]}[/]")
    return "".join(parts)
LLM_STYLE = "#b7c6d9"                  # light grey-blue - readable body text, not a heading
LLM_INDENT = "  "


def _indent_lines(text: str) -> str:
    """Prefix every line with LLM_INDENT - used for a non-streamed fallback
    print, so it looks the same as if it had streamed in line by line."""
    return "\n".join(LLM_INDENT + line for line in text.split("\n"))


class _StreamPrinter:
    """Prints the agent's reply as it streams in - own color, indented, so it's
    never confused with escrowe's own output. Tracks whether anything was
    printed at all, so the caller can skip reprinting the same text afterward."""

    def __init__(self):
        self.printed = False
        self._at_line_start = True

    def __call__(self, chunk: str) -> None:
        if not chunk:
            return
        self.printed = True
        out = []
        for ch in chunk:
            if self._at_line_start:
                out.append(LLM_INDENT)
                self._at_line_start = False
            out.append(ch)
            if ch == "\n":
                self._at_line_start = True
        console.print("".join(out), end="", style=LLM_STYLE, markup=False, highlight=False)

    def close(self) -> None:
        if self.printed:
            console.print()          # end the streamed line before whatever prints next


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
    _table_overview(conn.metadata()["tables"])
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


def _prompt_choice(text: str, choices: list[str], default: str) -> str:
    """A choice prompt that validates itself instead of passing a real
    click.Choice as `type=` to typer.prompt(): this typer version vendors its
    own copy of click's internals, and an invalid entry raises a *real*
    click.exceptions.BadParameter that its vendored retry loop doesn't
    recognize (it only catches its own vendored UsageError) - so instead of
    re-prompting, it crashes. Validating here avoids that entirely."""
    while True:
        value = typer.prompt(text, default=default).strip()
        if value in choices:
            return value
        console.print(f"[red]{value!r} is not one of: {', '.join(choices)}[/]")


def _first_run_database(conn: LocalConnection) -> None:
    from .engines import KINDS
    console.print()
    while True:
        kind = _prompt_choice(f"Database system [{'/'.join(KINDS)}]", list(KINDS), KINDS[0])
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


DEFAULT_PORTS = {"mysql": 3306, "postgres": 5432, "sqlserver": 1433}


def _ask_connection(kind: str) -> tuple[str, dict]:
    if kind == "ducklake":
        return "lake", _ducklake_prompts()
    if kind == "duckdb":
        path = typer.prompt("File path", default=str(Path.cwd() / "database.duckdb"))
        return "duckdb", {"path": str(Path(path).expanduser())}
    if kind == "sqlite":
        path = typer.prompt("File path", default=str(Path.cwd() / "database.sqlite"))
        return "sqlite", {"path": str(Path(path).expanduser())}
    host = typer.prompt("Host", default="127.0.0.1")
    port = int(typer.prompt("Port", default=DEFAULT_PORTS.get(kind, 3306)))
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
    # Loopback only, and requests without a token run as the operator - the same
    # no-separate-login mode the REPL is in. Someone can still log in as another
    # account from the UI; that's the /login flow, kept for when it's wanted.
    uvicorn.run(create_app(svc, local_operator=True), host="127.0.0.1", port=port, log_level="warning")


# ================================================================== the REPL

_ANSI = re.compile(r"(\x1b\[[0-9;]*m)")


def _readline_prompt(markup: str) -> str:
    """Render rich markup to a plain `input()` prompt, with escape codes wrapped
    in \\001/\\002 (readline's own "this is zero-width" markers).

    console.input() prints the prompt itself and then calls bare input() with
    none - so once readline is loaded (for history/arrow-key editing), it has
    no idea the prompt exists: a history redraw returns to column 0 and reprints
    only its own buffer, wiping out whatever was printed beside it. Passing the
    prompt to input() directly, correctly marked up, is what readline needs to
    redraw around it instead of over it.
    """
    with console.capture() as cap:
        console.print(markup, end="")
    return _ANSI.sub(r"\001\1\002", cap.get())


def _repl(conn, idle_minutes: float = IDLE_MINUTES, prompt: str = None) -> str:
    """DuckDB-style prompt. Returns 'quit' or 'idle'."""
    last: Result | None = None
    last_activity = time.time()
    label = prompt or NAME
    console.print(f"type a question, or \\sql …   (\\help for more, \\q to quit)")
    while True:
        try:
            line = input(_readline_prompt(f"[bold]{label}[/] ")).strip()
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
                # highlight=False: Rich's automatic highlighter otherwise tints things
                # like \sql/<query> as if they were code tokens, in a color nobody
                # chose on purpose. Explicit markup here instead - and the description
                # text is escaped since it can contain a literal "[...]" (\feed's
                # "[experimental]") that markup would otherwise try to parse as a tag.
                HELP_ITEMS = [
                    ("question text", "the agent writes SQL from the schema; you get the rows"),
                    ("\\sql <query>", "run SQL yourself, as your own connected account"),
                    ("\\meta", "the schema the agent sees"),
                    ("\\audit", "recent decisions"),
                    ("\\export <file>", "last result → .csv / .parquet / .json"),
                    ("\\json", "last result as JSON"),
                    ("\\feed <question>", "[experimental] ask about the last result's own data (no schema, no new query)"),
                    ("\\llmsetup", "restart the LLM setup (browser login or API key), from scratch"),
                    ("\\database", "disconnect and choose a different database, from scratch"),
                    ("\\q", "quit"),
                ]
                console.print("\n".join(f"{_style_cmd(f'{cmd:<18}')}{escape(desc)}" for cmd, desc in HELP_ITEMS),
                             highlight=False)
            elif line == "\\feed" or line.startswith("\\feed "):
                feed_question = line[len("\\feed"):].strip()
                if not last:
                    console.print("[yellow]nothing to feed yet - run a question first.[/]")
                elif not feed_question:
                    console.print("[yellow]usage: \\feed <question about the last result>[/]")
                elif typer.confirm(
                        "Are you sure you want to feed query results back to the LLM?", default=False):
                    feed_data = _feed_text(last)
                    console.print()
                    stream = _StreamPrinter()
                    with _status("thinking…") as st:
                        res = conn.ask(feed_question, on_status=lambda t: st and st.update(t + "…"),
                                       feed_data=feed_data, on_token=stream)
                    stream.close()
                    last = _show(res, streamed=stream.printed) or last
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
                    _table_overview(conn.metadata()["tables"])
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
                console.print()
                stream = _StreamPrinter()
                with _status("thinking…") as st:
                    res = conn.ask(line, on_status=lambda t: st and st.update(t + "…"), on_token=stream)
                stream.close()
                last = _show(res, streamed=stream.printed) or last
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
    """Render the last result for \\feed: the original question, the SQL that
    answered it, and the rows - capped so a big result can't silently blow up
    the prompt (or the bill). Agent.analyze() adds the "question is about
    these results" lead-in around this."""
    if res.answer and not res.columns:
        return None                      # nothing but prose from the schema; nothing to feed
    parts = []
    if res.question:
        parts.append(f"original question: {res.question}")
    if res.sql:
        parts.append(f"sql: {res.sql}")
    header = ", ".join(res.columns)
    lines = [", ".join("" if v is None else str(v) for v in r) for r in res.rows[:FEED_MAX_ROWS]]
    parts.append(f"columns: {header}\n" + "\n".join(lines))
    text = "\n".join(parts)
    if res.row_count > FEED_MAX_ROWS:
        text += f"\n... ({res.row_count - FEED_MAX_ROWS} more rows not shown)"
    return text[:FEED_MAX_CHARS]


OVERVIEW_MAX_TABLES = 20
OVERVIEW_COMMENT_CHARS = 100


def _table_overview(tables: list[dict], limit: int = OVERVIEW_MAX_TABLES) -> None:
    """A quick orientation right after connecting - what's actually in here -
    without scrolling a 445-table warehouse past the prompt. Documented tables
    first (someone bothered to describe them, so they're probably the ones
    that matter), then the biggest undocumented ones, capped at `limit`."""
    if not tables:
        return
    documented = [t for t in tables if t.get("comment")]
    rest = [t for t in tables if not t.get("comment")]
    documented.sort(key=lambda t: t.get("approx_rows") or 0, reverse=True)
    rest.sort(key=lambda t: t.get("approx_rows") or 0, reverse=True)
    shown = (documented + rest)[:limit]
    if not shown:
        return
    console.print(f"\n[dim]a quick look ({len(shown)} of {len(tables)} tables):[/]")
    for t in shown:
        rows = f" [dim](~{t['approx_rows']:,} rows)[/]" if t.get("approx_rows") else ""
        console.print(f"  [bold]{t['table']}[/]{rows}")
        if t.get("comment"):
            comment = t["comment"][:OVERVIEW_COMMENT_CHARS]
            console.print(f"    [dim]{comment}[/]")
    if len(tables) > limit:
        console.print(f"  [dim]… {len(tables) - limit} more - \\meta for the full schema[/]")
    console.print()


def _show(res: Result, max_rows: int = 200, streamed: bool = False) -> Result:
    """`streamed` means the text (answer or note) already printed live via
    _StreamPrinter as it arrived - so it isn't repeated here verbatim."""
    if res.answer:
        # The agent replied from the schema. Nothing was queried, so there is nothing to table.
        if not streamed:
            console.print(_indent_lines(res.answer), style=LLM_STYLE, markup=False, highlight=False)
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
    # res.note (a caveat the agent could attach after a query) is left out of the
    # display for now - plumbing stays in place, just not shown, on request.
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
    if not st["connected"] and not st["claude_installed"]:
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
    if not load_settings().api_enabled:
        console.print("[yellow]The HTTP API is off for this beta.[/] Set "
                      "[bold]ESCROWE_API_ENABLED=1[/] to turn it on.")
        raise typer.Exit(1)
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
        kind = _prompt_choice(f"Database system [{'/'.join(KINDS)}]", list(KINDS), KINDS[0])
        name = name or typer.prompt("Name for this source (used as the database name in SQL)", default=kind)
        _, params = _ask_connection(kind)
    try:
        data = conn.attach(name, kind, params, persist=False)
    except EscroweError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    console.print(f"[green]connected[/] {data['name']} ({data['kind']}) · {len(data['tables'])} tables")
    for t in data["tables"][:40]:
        console.print(f"  {t}")




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
