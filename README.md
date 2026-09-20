# Escrowe

![Escrowe architecture](docs/architecture.svg)

Escrowe is a blind-agent query gateway: it connects an LLM agent directly to a real database account, hands it only the schema (table/column names, types, comments — never row values), and lets it write SQL that runs as that account.

Escrowe adds no access control of its own. Whatever the connected database account can do, the agent can do — nothing more, nothing less. The one guarantee Escrowe provides structurally is that the agent never sees query result rows unless a human explicitly opts in.

## How it works

1. You connect a database account to Escrowe (`escrowe connect <dsn>`).
2. Escrowe reads the schema once, from the database's own system catalog (e.g. `information_schema`) — it never runs a `SELECT` against your tables to do this, so no data value can leak into what the agent sees.
3. You ask a question in natural language (`escrowe ask "..."`). The agent proposes SQL based on the schema alone.
4. The proposed SQL is checked by a guard (single statement, no writes, no `SELECT ... INTO OUTFILE`, no stored procedures, no dangerous built-ins like `SLEEP`/`LOAD_FILE`) and then executed.
5. Results go straight back to you — they do not re-enter the agent's context. The agent only gets to see shape information (row counts, null counts) via an optional "probe" query before finalizing its answer, or full data if you explicitly `\feed` a result back to it for follow-up analysis.

Every attempt — proposed SQL, compiled SQL, guard decision, row count, duration — is written to a local audit log.

## Install

```bash
pip install -e .
```

Requires Python ≥3.10.

## Usage

Guided setup (connect a database, connect Claude, start asking questions):

```bash
escrowe
```

Scripted / machine use:

```bash
escrowe connect mysql://user:pw@host:3306/shop --local
escrowe ask "how many orders shipped last week?" --local
escrowe sql "SELECT count(*) FROM orders" --local
```

Run as a server for remote/multi-user access:

```bash
escrowe serve
```

then, from a client, `escrowe login` / `escrowe shell` for an interactive REPL, or use the Python client library:

```python
import escrowe
conn = escrowe.connect("escrowe://user:pass@host:port")
result = conn.ask("...")
result.rows       # or result.to_arrow()
```

An HTTP API (FastAPI) is also available for programmatic access, but is disabled by default during the current beta — enable it with `ESCROWE_API_ENABLED=1`.

## Database engines

Engines are pluggable (`escrowe/engines/`). Built in:

- **MySQL** (`mysql://...`, via PyMySQL)
- **DuckLake** (via DuckDB as a DuckLake client, attaching to catalogs backed by SQLite, Postgres, MySQL, a local file, or a Quack server)

Adding a new database kind means adding one module and registering it.

## Configuration

Environment variables (see `escrowe/config.py`; also loaded from a `.env` file):

| Variable | Purpose |
|---|---|
| `ESCROWE_HOME` | Local data directory (default `~/.escrowe`) |
| `ESCROWE_ATTACH` | Database attachment spec (`name=kind:spec;...`) |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PWD` / `MYSQL_DATABASE` | Standard MySQL client vars, auto-detected as an attachment |
| `ESCROWE_JWT_SECRET` | JWT signing secret for server sessions |
| `ESCROWE_LLM` | LLM provider: `auto` \| `anthropic` \| `claude-cli` \| `mock` |
| `ESCROWE_MODEL` | Model name (default `claude-opus-5`) |
| `ESCROWE_LLM_THINKING` | Extended-thinking budget (Anthropic only) |
| `ESCROWE_MAX_ROWS` / `ESCROWE_QUERY_TIMEOUT` / `ESCROWE_AGENT_ATTEMPTS` | Query/agent limits |
| `ESCROWE_SERVER` | Default server URL for client commands |
| `ESCROWE_API_ENABLED` | Enable the HTTP API (off by default) |
| `ESCROWE_EPHEMERAL` | Throwaway home directory — nothing persisted to disk |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Security model

Escrowe is not an authorization system. Access control is entirely delegated to the database account's own grants — connect an account scoped to exactly what you're willing to let the agent read. The value Escrowe adds is keeping raw data out of the agent's context by default, giving the agent enough schema and shape information to write useful SQL without ever seeing the data itself unless a human chooses to share it.
