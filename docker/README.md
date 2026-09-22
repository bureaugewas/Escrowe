# Test databases

Docker Compose stack of real MySQL, PostgreSQL, and SQL Server instances for
exercising escrowe's direct engines end to end (`tests/test_mysql_direct.py`,
`test_postgres_direct.py`, `test_sqlserver_direct.py`). Each is pre-loaded
with the same sample schema: `customers` and `orders` tables, an FK between
them, mixed column types (int, varchar, date, timestamp, decimal, boolean),
and a comment on `customers` (and `customers.tier`) where the engine supports
column/table comments. SQLite needs no container, see below.

## Bring it up

```sh
cd docker
docker compose up -d
```

Wait for the containers to report healthy (`docker compose ps`) before
running the tests - first boot loads the init SQL, which takes a few seconds
per engine. The SQL Server init runs as a one-shot sidecar
(`escrowe-test-sqlserver-init`) that waits for the server's healthcheck, then
runs `docker/init/sqlserver.sql` with `sqlcmd`; check `docker compose logs
escrowe-test-sqlserver-init` if the SQL Server tests find no tables.

## What's inside

| Service | Image | Host port | User | Password | Database |
|---|---|---|---|---|---|
| MySQL | `mysql:8.0.36` | 23306 | root | root | clientdb |
| PostgreSQL | `postgres:16.4` | 25432 | root | root | clientdb |
| SQL Server | `mcr.microsoft.com/mssql/server:2022-CU14-ubuntu-22.04` | 21433 | sa | `Root_pw1` | clientdb |

Host ports are non-default (23306/25432/21433) to avoid colliding with any
MySQL/Postgres/SQL Server already running on the usual ports on your machine.
Point the test env vars at them, e.g.:

```sh
export ESCROWE_TEST_MYSQL_PORT=23306 ESCROWE_TEST_MYSQL_USER=root ESCROWE_TEST_MYSQL_PASSWORD=root
export ESCROWE_TEST_POSTGRES_PORT=25432 ESCROWE_TEST_POSTGRES_USER=root ESCROWE_TEST_POSTGRES_PASSWORD=root
export ESCROWE_TEST_SQLSERVER_PORT=21433 ESCROWE_TEST_SQLSERVER_USER=sa ESCROWE_TEST_SQLSERVER_PASSWORD=Root_pw1
```

### Deviation: SQL Server password

Every other service uses the project convention `root` / `root`. SQL Server
enforces a password complexity policy on `SA_PASSWORD`/`MSSQL_SA_PASSWORD`
(needs upper+lower+digit, 8+ chars) and refuses to start with `root` -
`Root_pw1` is the substitute used here, only for the SQL Server container.

## SQLite

No container: it is a plain file. `tests/test_sqlite_direct.py` generates its
own copy in a temp directory, so nothing needs to be run first. To get a
sample file to play with:

```sh
python docker/seed_sqlite.py            # writes docker/sample.sqlite (git-ignored)
escrowe connect sqlite:docker/sample.sqlite
```

SQLite has no table or column comments, so the schema the agent sees has none.

## Tearing down

```sh
docker compose down -v
```
