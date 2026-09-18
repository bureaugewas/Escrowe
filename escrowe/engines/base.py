"""The shape every direct engine implements.

Escrowe's only job is: connect to a real database as a given account, hand
the agent its schema (never its data), run the SQL the agent or the person
wrote, and return the rows. No rewrite, no escrowe-side grants - the
database's own account is the entire access decision. Adding a new database
system means adding one of these, nothing else in the request path changes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa


@dataclass(frozen=True)
class Column:
    fqn: str          # whatever the connected account can put straight into a FROM
                      # clause: the bare table name once connected to one database,
                      # "db.table" for an engine connected to a whole server
    name: str
    type: str
    comment: str | None = None        # from the database's own catalog, never invented
    table_comment: str | None = None


class EngineError(Exception):
    pass


class DirectEngine:
    """Base class documenting the interface; kind-specific engines subclass it.
    See engines/mysql.py and engines/ducklake.py for the implementations so far."""

    kind: str = ""
    default_port: int | None = None
    # False for a kind with no per-account identity of its own (DuckLake): no
    # username/password to check, so escrowe connects straight from the
    # configured params with no login step.
    requires_credentials: bool = True

    def close(self) -> None:
        raise NotImplementedError

    def catalog(self) -> list[Column]:
        """Table/column names, types and comments. Must never issue a query
        against a user table - only the database's own system catalog."""
        raise NotImplementedError

    def table_sizes(self) -> dict[str, int]:
        raise NotImplementedError

    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        raise NotImplementedError

    @classmethod
    def test_login(cls, **params) -> None:
        """Open and immediately close a connection with these credentials.
        Raises EngineError with a clean message on failure."""
        raise NotImplementedError
