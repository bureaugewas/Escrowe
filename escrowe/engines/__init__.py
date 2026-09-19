"""One engine per database kind escrowe can connect to directly. Add a new
database system by adding one module here (subclassing DirectEngine) and
registering it below - nothing elsewhere needs to change."""

from .base import Column, DirectEngine, EngineError
from .ducklake import DuckDBEngine, DuckLakeEngine
from .mysql import MySQLEngine

REGISTRY: dict[str, type[DirectEngine]] = {
    "mysql": MySQLEngine,
    "ducklake": DuckLakeEngine,
    "duckdb": DuckDBEngine,
}


def __getattr__(name):
    # KINDS reads REGISTRY live (PEP 562), so a kind registered after import
    # (tests do this) is still recognized instead of a frozen import-time snapshot.
    if name == "KINDS":
        return tuple(REGISTRY)
    raise AttributeError(name)


def build(kind: str, **params) -> DirectEngine:
    try:
        cls = REGISTRY[kind]
    except KeyError:
        raise EngineError(f"Unknown database kind {kind!r}; choose from {', '.join(REGISTRY)}")
    return cls(**params)


__all__ = ["Column", "DirectEngine", "EngineError", "REGISTRY", "KINDS", "build"]
