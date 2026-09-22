"""Interactive setup: the questions that connect a database and connect
Claude. Used by the guided `escrowe` command and by `escrowe connect` when
no connection string is given."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from .. import llm_login
from ..client import EscroweAuthError, EscroweError, LocalConnection
from ..engines import kinds
from .console import console, status

DEFAULT_PORTS = {"mysql": 3306, "postgres": 5432, "sqlserver": 1433}
LLM_CHOSEN = "llm_provider"          # store key: what the person picked
LLM_METHOD = "llm_method"            # store key: browser | api_key


def prompt_choice(text: str, choices: list[str], default: str) -> str:
    while True:
        value = typer.prompt(text, default=default).strip()
        if value in choices:
            return value
        console.print(f"[red]{value!r} is not one of: {', '.join(choices)}[/]")


# ------------------------------------------------------------- database

def connect_database(conn: LocalConnection, persist: bool = False) -> None:
    """Ask for connection details until a database with visible tables connects."""
    console.print()
    while True:
        kind = prompt_choice(f"Database system [{'/'.join(kinds())}]", list(kinds()), kinds()[0])
        name, params = ask_connection(kind)
        try:
            with status("connecting"):
                res = conn.set_source(name, kind, params, persist=persist)
        except EscroweError as e:
            console.print(f"[red]{e}[/]")
            if typer.confirm("Try again?", default=True):
                continue
            raise typer.Exit(1)
        if not res["tables"]:
            console.print(f"[yellow]connected {res['name']}, but no tables are visible to "
                          f"{params.get('user', 'this account')}.[/] "
                          "Pick a different database, or grant the account access.")
            conn.remove_source()
            if typer.confirm("Try again?", default=True):
                continue
            raise typer.Exit(1)
        console.print(f"[green]connected[/] {res['name']} · {len(res['tables'])} tables")
        return


def ask_connection(kind: str) -> tuple[str, dict]:
    if kind == "ducklake":
        return "lake", _ducklake_params()
    if kind in ("duckdb", "sqlite"):
        path = typer.prompt("File path", default=str(Path.cwd() / f"database.{kind}"))
        return kind, {"path": str(Path(path).expanduser())}
    host = typer.prompt("Host", default="127.0.0.1")
    port = int(typer.prompt("Port", default=DEFAULT_PORTS.get(kind, 3306)))
    database = typer.prompt("Database", default="", show_default=False).strip()
    user = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True, default="", show_default=False)
    params = {"host": host, "port": port, "user": user, "password": password}
    if not database and kind == "mysql":
        database = _choose_mysql_database(params) or ""
    if database:
        params["database"] = database
    return kind, params


def _ducklake_params() -> dict:
    """A DuckLake catalog is reached through a hosted Quack server: a local
    catalog file cannot be shared once a server holds its lock."""
    host = typer.prompt("Host:port", default="127.0.0.1:443")
    token = typer.prompt("Quack token", hide_input=True, default="", show_default=False).strip()
    if not token:
        console.print("[yellow]No token entered (input is hidden while typing); most Quack "
                      "servers reject the attach without one.[/]")
        if not typer.confirm("Continue without a token?", default=False):
            token = typer.prompt("Quack token", hide_input=True).strip()
    params = {"metadata": f"quack:{host}"}
    if token:
        params["token"] = token
    data_path = typer.prompt("Data path (s3://…, r2://…), only needed the first time this catalog is used",
                             default="", show_default=False).strip()
    if data_path:
        params["data_path"] = data_path
    return params


def _choose_mysql_database(params: dict) -> str | None:
    """Offer what this account can actually see instead of asking for a name blind."""
    from ..sources import list_mysql_databases
    try:
        with status("looking at what you can see"):
            names = list_mysql_databases(params)
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
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return names[int(choice) - 1]
    return choice.strip() or None


def confirm_saved_connection(conn: LocalConnection) -> bool:
    """Ask before reusing the last connection, and forget it if declined."""
    src = conn.svc.source
    label = f"{src.name} ({src.kind}" + (f" as {src.user}" if src.user else "") + ")"
    if typer.confirm(f"Connect to the last connection, {label}?", default=False):
        return True
    conn.remove_source()
    return False


def login_saved_connection(conn: LocalConnection) -> bool:
    """A source was connected in an earlier run and its password was not kept
    (by design): ask for it once now."""
    for _ in range(3):
        try:
            conn.login(typer.prompt("Username"), typer.prompt("Password", hide_input=True))
            return True
        except EscroweAuthError as e:
            console.print(f"[red]{e}[/]")
        except (EOFError, KeyboardInterrupt, typer.Abort):
            return False
    return False


# --------------------------------------------------------------- claude

def ensure_claude(svc, interactive: bool = True, force: bool = False, quiet: bool = False) -> bool:
    """Settle the LLM. Asks unless the person already chose and it still works.
    A credential that happens to exist is not the same as being told to use
    it (it may be the wrong account), so the first run always asks."""
    chosen = svc.store.setting(LLM_CHOSEN)
    st = llm_login.status(svc.store)
    if chosen and st["connected"] and not force:
        if not interactive:
            if not quiet:
                console.print(f"[dim]{llm_login.describe(st)}[/]")
            return True
        if typer.confirm(f"Keep using {llm_login.describe(st)}?", default=True):
            return True
        llm_login.forget_api_key(svc.store)
        svc.store.set_setting(LLM_CHOSEN, "")
        svc.reload_agent()
        return _choose_llm(svc)
    if not interactive:
        console.print("[yellow]No LLM is connected, so questions will not work.[/] "
                      "Run [bold]escrowe claude login[/] on a terminal to connect one.")
        return False
    return _choose_llm(svc)


def _choose_llm(svc) -> bool:
    console.print("\n[bold]Which LLM should escrowe use?[/]")
    console.print("  1. Claude")
    console.print("  2. Not now - only \\sql will work")
    if typer.prompt("Which", default="1").strip() == "2":
        console.print("[yellow]Skipped.[/] Run [bold]escrowe claude login[/] when you want to connect one.")
        return False
    svc.store.set_setting(LLM_CHOSEN, "claude")
    console.print("\n[bold]How should escrowe connect to Claude?[/]")
    console.print("  1. Browser login - signs in to your Anthropic account")
    console.print("  2. API key - billed per token")
    method = "api_key" if typer.prompt("Which", default="1").strip() == "2" else "browser"
    svc.store.set_setting(LLM_METHOD, method)
    return _connect_api_key(svc) if method == "api_key" else _connect_subscription(svc)


def _connect_subscription(svc) -> bool:
    """Always performs the login: being signed in already may be the wrong account."""
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
    with status("checking the key"):
        ok, why = llm_login.check_api_key(key)
    if not ok:
        console.print(f"[red]That key was not accepted:[/] {why}")
        return False
    llm_login.save_api_key(svc.store, key)
    svc.reload_agent()
    console.print(f"[green]Claude connected with an API key.[/] [dim]stored in {svc.store.path}, "
                  "readable only by you[/]")
    return True


def is_tty() -> bool:
    return sys.stdin.isatty()
