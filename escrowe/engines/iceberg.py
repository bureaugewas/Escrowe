"""An Iceberg REST catalog, attached through DuckDB's own `iceberg` extension.

Any catalog that speaks the Iceberg REST spec works: Apache Polaris,
Lakekeeper, Nessie, Gravitino, Unity Catalog, ... It is reached at
`endpoint`, and `warehouse` names the catalog on that server.

The catalog authenticates the connection one of two ways, both plain OAuth2
and indifferent to which identity provider issued them:

  token                                   a bearer token, e.g. a signed-in
                                          user's access token
  client_id + client_secret               client credentials; DuckDB fetches
    (+ oauth2_server_uri, scope)          the token itself

A login (`escrowe login`, or a `--local` command asking for a user and
password) is the client credentials: the user is the client ID and the
password its secret, so each person connects as their own catalog principal.

The catalog's own grants decide what the connection may read. Table data is
fetched with the storage credentials the catalog vends for that table, so
escrowe never needs its own S3 keys.
"""

from __future__ import annotations

import threading

from .base import Column, EngineError, first_line
from .ducklake import DuckDBEngine, _lit


class IcebergEngine(DuckDBEngine):
    kind = "iceberg"

    def __init__(self, endpoint: str, warehouse: str, token: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None,
                 oauth2_server_uri: str | None = None, scope: str | None = None,
                 alias: str = "lake", user: str | None = None, password: str | None = None, **_):
        client_id, client_secret = user or client_id, password or client_secret
        self._lock = threading.Lock()
        self.alias = alias
        self.conn = self._open(":memory:")
        if token:
            auth = {"TOKEN": token}
        elif client_id and client_secret:
            # Without a token URL DuckDB asks the catalog's own, as the REST spec defines.
            auth = {"CLIENT_ID": client_id, "CLIENT_SECRET": client_secret, "ENDPOINT": endpoint,
                    "OAUTH2_SERVER_URI": oauth2_server_uri, "OAUTH2_SCOPE": scope}
        else:
            self.close()
            raise EngineError("An Iceberg catalog needs a token, or a client_id and client_secret.")
        secret = ", ".join(f"{k} {_lit(v)}" for k, v in auth.items() if v)
        try:
            self.conn.execute("INSTALL iceberg; LOAD iceberg;")
            self.conn.execute(f"CREATE SECRET escrowe_iceberg (TYPE iceberg, {secret})")
            self.conn.execute(
                f"ATTACH {_lit(warehouse)} AS {alias} (TYPE iceberg, ENDPOINT {_lit(endpoint)}, "
                "SECRET escrowe_iceberg, ACCESS_DELEGATION_MODE 'vended_credentials')")
            # There is no `main` namespace to USE; any one makes this the default
            # catalog, so `namespace.table` resolves here.
            first = self.conn.execute("SELECT schema_name FROM duckdb_schemas() WHERE database_name = ? "
                                      "ORDER BY schema_name LIMIT 1", [alias]).fetchone()
            if first:
                self.conn.execute(f"USE {alias}.\"{first[0]}\"")
        except Exception as e:
            self.close()
            raise EngineError(first_line(e)) from e

    def catalog(self) -> list[Column]:
        # DuckDB lists an Iceberg table's columns as a placeholder until the
        # table's metadata is loaded. DESCRIBE loads it (the metadata file,
        # never a data file), after which the columns and their docs are real.
        with self._lock:
            tables = self.conn.execute("SELECT schema_name, table_name FROM duckdb_tables() "
                                       "WHERE database_name = ?", [self.alias]).fetchall()
            for schema, table in tables:
                self.conn.execute(f'DESCRIBE {self.alias}."{schema}"."{table}"')
        return super().catalog()
