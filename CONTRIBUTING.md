# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

The suite runs against an in-memory fake engine (`tests/fake_engine.py`) and
needs no database. The per-engine tests (`tests/test_*_direct.py`) connect to
real servers from `tests/docker/` and skip themselves when those are not
running; see `tests/docker/README.md`.

## Where things live

| Path | What it is |
|---|---|
| `escrowe/service.py` | The core: connect, login, `sql()`, `ask()`. Start here. |
| `escrowe/guard.py` | The only checks made on a query before it runs. |
| `escrowe/agent.py` | Prompt construction, reply parsing, the LLM providers. |
| `escrowe/engines/` | One module per database kind. |
| `escrowe/cli/` | The `escrowe` command: commands, the prompt, the setup wizard. |
| `escrowe/server.py` | The HTTP API and the browser UI (`escrowe/static/`). |
| `docs/` | Architecture diagrams, from the big picture down to one request. |

## Adding a database kind

1. Add `escrowe/engines/<kind>.py` with a class that subclasses `DBAPIEngine`
   (for a PEP 249 driver) or `DirectEngine`, implementing `catalog()` and
   `table_sizes()` from the database's own system catalog. Never read a user
   table there.
2. Register it in `escrowe/engines/__init__.py`.
3. If sqlglot has a dialect for it, add the mapping in `escrowe/guard.py`.
4. Add a `tests/test_<kind>_direct.py` and a service to `tests/docker/`.

## The one rule

Nothing from a query result may reach the agent unless a person explicitly
fed it (`\feed`). `tests/test_blind.py` and `tests/test_no_data_leaks.py`
check this; a change that makes them fail is wrong, not the tests.

## Dependencies

Versions are pinned exactly in `pyproject.toml`. Bump one deliberately: change
the pin, run the full suite, commit. `ruff` is the linter; CI runs it.
