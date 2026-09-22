# Escrowe

![Escrowe overview](docs/01-overview.svg)

Escrowe lets an LLM agent write SQL against your database without ever
seeing the data. It connects with a real database account, shows the agent
that account's schema (table and column names, types, comments, never
values), runs the SQL the agent writes as that account, and hands the rows
to you. The rows do not go back to the agent.

Escrowe adds no access control of its own: it does not grant the agent
anything the connected account cannot already do. What it does add is a
non-transformation policy - by default the agent may only run a single
`SELECT` (or `UNION`/`EXCEPT`/`INTERSECT`), never a write, a stored
procedure, or a file/environment read disguised as one - and the structural
guarantee that query results never re-enter the agent's context unless a
person explicitly feeds them back.

## How a question is answered

1. You connect a database account: `escrowe connect mysql://user:pw@host/shop`.
2. Escrowe reads the schema once, from the database's own system catalog.
   No `SELECT` is ever run against your tables to do this.
3. You ask a question. The agent proposes SQL from the schema alone.
4. A guard checks the SQL: one statement, read-only for the agent, no file
   writes, no stored procedures, no built-ins that read server files or
   stall the connection. Nothing is rewritten.
5. The query runs as your account. The rows come back to you. The agent may
   first "probe": run a query and get back only its shape (row count, which
   columns were all NULL) before finalizing.

Every attempt is written to a local audit log, and every prompt sent to the
LLM is written to a transcript you can inspect with `escrowe llm-log`.

## Install

With [pipx](https://pipx.pypa.io) (recommended: an isolated environment, and `escrowe` on your PATH):

```bash
pipx install "git+https://github.com/bureaugewas/Escrowe.git"
```

Or with pip, into your current environment:

```bash
pip install "git+https://github.com/bureaugewas/Escrowe.git"
```

Either way this clones and installs escrowe for you; you don't need a local
checkout first. Python 3.10 or newer.

To work on escrowe itself, clone it and install in editable mode instead —
see [CONTRIBUTING.md](CONTRIBUTING.md).

## Use

Guided: connect a database, connect Claude, start asking.

```bash
escrowe
```

The same in the browser, with notebooks and charts:

```bash
escrowe -ui
```

Scripted:

```bash
escrowe connect mysql://user:pw@host:3306/shop
escrowe ask "how many orders shipped last week?" --local
escrowe sql "SELECT count(*) FROM orders" --local
escrowe meta --local
```

The password is never saved. A later `--local` command asks for it once, or
reads `ESCROWE_PASSWORD`.

Inside the prompt: type a question, or `\sql <query>`, `\meta`, `\audit`,
`\export <file>`, `\json`, `\feed <question>` (experimental: ask about the
last result's own data), `\help`, `\q`.

### As a server

```bash
ESCROWE_API_ENABLED=1 escrowe serve
```

Then from another machine: `escrowe login`, `escrowe shell`, or Python:

```python
import escrowe
with escrowe.connect("escrowe://user:pw@host:8765") as conn:
    result = conn.ask("revenue per region this quarter")
    result.sql, result.rows, result.to_arrow()
```

Each login opens its own database connection as that person; sessions are
bearer tokens and the password is dropped after the connection opens.

## Databases

| Kind | Connection string | Driver |
|---|---|---|
| MySQL / MariaDB | `mysql://user:pw@host:3306/db` | PyMySQL |
| PostgreSQL | `postgres://user:pw@host:5432/db` | psycopg |
| SQL Server | `sqlserver://user:pw@host:1433/db` | pymssql |
| SQLite | `sqlite:/path/to/file.sqlite` | stdlib |
| DuckDB | `duckdb:/path/to/file.duckdb` | duckdb |
| DuckLake | `ducklake:/catalog.duckdb`, `ducklake:quack:host:port?token=…` | duckdb |

Adding a kind is one module in `escrowe/engines/` plus a registry entry; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Configuration

Read from the environment, or a `.env` file in the working directory or in
`~/.escrowe`.

| Variable | Purpose |
|---|---|
| `ESCROWE_HOME` | Where state lives (default `~/.escrowe`) |
| `ESCROWE_LLM` | `auto` \| `anthropic` \| `claude-cli` \| `mock` |
| `ESCROWE_MODEL` | Model name (default `claude-opus-5`) |
| `ESCROWE_LLM_THINKING` | Extended-thinking token budget (API-key provider only) |
| `ESCROWE_MAX_ROWS`, `ESCROWE_QUERY_TIMEOUT`, `ESCROWE_AGENT_ATTEMPTS` | Query and agent limits |
| `ESCROWE_API_ENABLED` | Allow `escrowe serve` |
| `ESCROWE_JWT_SECRET` | Session signing secret (generated if unset) |
| `ESCROWE_SERVER` | Default server URL for client commands |
| `ESCROWE_PASSWORD`, `ESCROWE_USER` | Credentials for scripted `--local` commands |
| `ANTHROPIC_API_KEY` | Use the API instead of a Claude subscription |

## Architecture

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks from the big picture down
to what one question does, with diagrams at each level.

## Security model

Escrowe is not an authorization system. Connect an account scoped to exactly
what you are willing to let the agent read; the database's grants decide the
rest. What Escrowe adds is keeping data out of the agent's context: the agent
gets enough schema and shape information to write useful SQL and nothing
else. `tests/test_blind.py` and `tests/test_no_data_leaks.py` verify this.

## Development

```bash
pip install -e ".[dev]"
pytest          # fake in-memory engine; live engines via docker/ (see docker/README.md)
ruff check .
```

MIT licensed.
