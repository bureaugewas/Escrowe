"""Escrowe: connect an LLM agent to a database, directly, without it ever
seeing the data.

escrowe adds no access control of its own: it connects with a real account
on the database, hands the agent that account's schema (names, types,
comments - never values), and runs the SQL it writes as that same account.
Whatever the account can't do, neither can the agent.
"""

__version__ = "0.1.0"


def build() -> str:
    """The commit this code came from, so you can tell which build you are running."""
    import subprocess
    from pathlib import Path
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent),
                              "log", "-1", "--format=%h %cd", "--date=short"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"

from .client import Connection, EscroweAuthError, EscroweDenied, EscroweError, LocalConnection, Result, connect, embedded  # noqa: E402,F401
