"""Escrowe: connect an LLM agent to a database, directly, without it ever
seeing the data.

Escrowe adds no access control of its own: it connects with a real account
on the database, hands the agent that account's schema (names, types,
comments, never values), and runs the SQL it writes as that same account.
Whatever the account cannot do, neither can the agent.
"""

__version__ = "0.1.0"

from .client import (  # noqa: E402
    Connection,
    EscroweAuthError,
    EscroweDenied,
    EscroweError,
    LocalConnection,
    Result,
    connect,
    embedded,
)

__all__ = ["Connection", "EscroweAuthError", "EscroweDenied", "EscroweError", "LocalConnection",
           "Result", "connect", "embedded", "__version__"]
