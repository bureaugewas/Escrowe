"""The interactive prompt: type a question, or a \\command."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

try:
    import readline  # noqa: F401  (line editing and history in input())
except ImportError:
    pass

import typer
from rich.markup import escape
from rich.table import Table

from ..client import EscroweAuthError, EscroweDenied, EscroweError, Result
from . import wizard
from .console import NAME, StreamPrinter, console, print_llm_text, status, style_command

IDLE_MINUTES = float(os.environ.get("ESCROWE_IDLE_MINUTES", "30"))
FEED_MAX_ROWS = 50
FEED_MAX_CHARS = 4000
OVERVIEW_MAX_TABLES = 20

HELP = [
    ("question text", "the agent writes SQL from the schema; you get the rows"),
    ("\\sql <query>", "run SQL yourself, as your own connected account"),
    ("\\meta", "the schema the agent sees"),
    ("\\audit", "recent decisions"),
    ("\\export <file>", "last result → .csv / .parquet / .json"),
    ("\\json", "last result as JSON"),
    ("\\feed <question>", "[experimental] ask about the last result's own data (no schema, no new query)"),
    ("\\q", "quit"),
]

_ANSI = re.compile(r"(\x1b\[[0-9;]*m)")


def _readline_prompt(markup: str) -> str:
    """Render Rich markup to a plain input() prompt, with escape codes wrapped
    in \\001/\\002 so readline knows they take no width and redraws correctly."""
    with console.capture() as cap:
        console.print(markup, end="")
    return _ANSI.sub(r"\001\1\002", cap.get())


def run(conn, idle_minutes: float = IDLE_MINUTES, prompt: str | None = None) -> str:
    """Loop until quit. Returns 'quit' or 'idle'."""
    last: Result | None = None
    last_activity = time.time()
    label = prompt or NAME
    console.print("type a question, or \\sql …   (\\help for more, \\q to quit)")
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
            last = _dispatch(conn, line, last) or last
        except EscroweDenied as e:
            console.print(f"[red]DENIED[/] {e}")
            if e.needs_login and wizard.is_tty() and hasattr(conn, "svc"):
                if wizard.ensure_llm(conn.svc, force=True):
                    console.print("[dim]ask again[/]")
        except EscroweAuthError as e:
            console.print(f"[red]AUTH[/] {e}")
            return "idle"
        except EscroweError as e:
            console.print(f"[red]ERROR[/] {e}")


def _dispatch(conn, line: str, last: Result | None) -> Result | None:
    if line == "\\help":
        # highlight=False: Rich would otherwise tint \sql/<query> like code.
        console.print("\n".join(f"{style_command(f'{cmd:<18}')}{escape(desc)}" for cmd, desc in HELP),
                      highlight=False)
    elif line == "\\meta":
        console.print(conn.metadata()["text"])
    elif line == "\\audit":
        for r in conn.audit(15):
            console.print(f"[dim]#{r['id']} {r['ts']}[/] {r['user']} {r['mode']} [bold]{r['decision']}[/] "
                          f"{r.get('reason') or ''} {r.get('candidate_sql') or ''}"[:200])
    elif line == "\\json":
        console.print(last.to_json() if last else "nothing yet")
    elif line.startswith("\\export "):
        if last:
            export(last, line.split(None, 1)[1].strip())
        else:
            console.print("nothing to export yet")
    elif line.startswith("\\sql "):
        return show(conn.sql(line[5:]))
    elif line == "\\feed" or line.startswith("\\feed "):
        return _feed(conn, line[len("\\feed"):].strip(), last)
    else:
        return _ask(conn, line)
    return None


def _ask(conn, question: str, feed_data: str | None = None) -> Result:
    console.print()
    stream = StreamPrinter()
    with status("thinking…") as st:
        res = conn.ask(question, on_status=lambda t: st and st.update(t + "…"),
                       feed_data=feed_data, on_token=stream)
    stream.close()
    return show(res, streamed=stream.printed)


def _feed(conn, question: str, last: Result | None) -> Result | None:
    if not last:
        console.print("[yellow]nothing to feed yet - run a question first.[/]")
    elif not question:
        console.print("[yellow]usage: \\feed <question about the last result>[/]")
    elif typer.confirm("Are you sure you want to feed query results back to the LLM?", default=False):
        return _ask(conn, question, feed_data=feed_text(last))
    return None


def feed_text(res: Result) -> str | None:
    """The last result for \\feed: question, SQL and rows, capped so a big
    result cannot blow up the prompt."""
    if res.answer and not res.columns:
        return None
    parts = []
    if res.question:
        parts.append(f"original question: {res.question}")
    if res.sql:
        parts.append(f"sql: {res.sql}")
    lines = [", ".join("" if v is None else str(v) for v in r) for r in res.rows[:FEED_MAX_ROWS]]
    parts.append(f"columns: {', '.join(res.columns)}\n" + "\n".join(lines))
    text = "\n".join(parts)
    if res.row_count > FEED_MAX_ROWS:
        text += f"\n... ({res.row_count - FEED_MAX_ROWS} more rows not shown)"
    return text[:FEED_MAX_CHARS]


def show(res: Result, max_rows: int = 200, streamed: bool = False) -> Result:
    """Print a result. `streamed` means the prose already printed live."""
    if res.answer:
        if not streamed:
            print_llm_text(res.answer)
        console.print(f"[dim]from the schema · audit #{res.audit_id}[/]")
        return res
    console.print(f"[dim]sql:[/] {res.sql}")
    table = Table(show_lines=False, header_style="bold cyan")
    for c in res.columns:
        table.add_column(c)
    for r in res.rows[:max_rows]:
        table.add_row(*["" if v is None else str(v) for v in r])
    console.print(table)
    tail = f"{res.row_count} row(s) · {res.duration_ms} ms · audit #{res.audit_id}"
    if res.attempts > 1:
        tail += f" · {res.attempts} attempts"
    console.print(f"[dim]{tail}[/]")
    return res


def export(res: Result, path: str) -> None:
    if res.answer and not res.columns:
        console.print("the last reply was an answer, not a table")
        return
    table = res.to_arrow()
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq
        pq.write_table(table, path)
    elif path.endswith(".json"):
        Path(path).write_text(json.dumps(res.records(), default=str, indent=1))
    else:
        import pyarrow.csv as pc
        pc.write_csv(table, path)
    console.print(f"wrote {table.num_rows} rows to {path}")


def table_overview(tables: list[dict], limit: int = OVERVIEW_MAX_TABLES) -> None:
    """A quick orientation after connecting: documented tables first, then the
    biggest undocumented ones, capped so a large warehouse does not scroll by."""
    if not tables:
        return
    documented = sorted((t for t in tables if t.get("comment")), key=lambda t: t.get("approx_rows") or 0, reverse=True)
    rest = sorted((t for t in tables if not t.get("comment")), key=lambda t: t.get("approx_rows") or 0, reverse=True)
    shown = (documented + rest)[:limit]
    console.print(f"\n[dim]a quick look ({len(shown)} of {len(tables)} tables):[/]")
    for t in shown:
        rows = f" [dim](~{t['approx_rows']:,} rows)[/]" if t.get("approx_rows") else ""
        console.print(f"  [bold]{t['table']}[/]{rows}")
        if t.get("comment"):
            console.print(f"    [dim]{t['comment'][:100]}[/]")
    if len(tables) > limit:
        console.print(f"  [dim]… {len(tables) - limit} more - \\meta for the full schema[/]")
    console.print()
