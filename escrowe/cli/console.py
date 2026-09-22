"""Shared terminal output: one Rich console, the colours, and small helpers."""

from __future__ import annotations

import re
import sys
from contextlib import nullcontext

from rich.console import Console

console = Console()

# Named ANSI colours rather than truecolor hex: every terminal that does
# colour at all renders the 16 standard names exactly.
BLUE = "bright_blue"
NAME = f"[bold {BLUE}]Escrowe[/]"
CMD_STYLE = "blue"                 # a \command in \help
PLACEHOLDER_STYLE = "magenta"      # <query> / <file> inside a \command
LLM_STYLE = "#b7c6d9"              # the agent's own words, so they never read as escrowe's
LLM_INDENT = "  "

_PLACEHOLDER = re.compile(r"(<[^>]+>)")


def status(message: str):
    """A spinner on a terminal; nothing at all when piped, because the live
    display rewrites lines and would erase prompts printed around it."""
    return console.status(message) if sys.stdout.isatty() else nullcontext()


def style_command(cmd: str) -> str:
    """Markup for a \\command with its <placeholders> in their own colour."""
    parts, last = [], 0
    for m in _PLACEHOLDER.finditer(cmd):
        if m.start() > last:
            parts.append(f"[{CMD_STYLE}]{cmd[last:m.start()]}[/]")
        parts.append(f"[{PLACEHOLDER_STYLE}]{m.group(1)}[/]")
        last = m.end()
    if last < len(cmd):
        parts.append(f"[{CMD_STYLE}]{cmd[last:]}[/]")
    return "".join(parts)


def print_llm_text(text: str) -> None:
    """The agent's prose, indented and in its own colour."""
    console.print("\n".join(LLM_INDENT + line for line in text.split("\n")),
                  style=LLM_STYLE, markup=False, highlight=False)


class StreamPrinter:
    """Prints the agent's reply as it streams in, indented like print_llm_text.
    Remembers whether anything was printed so the caller does not repeat it."""

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
            console.print()
