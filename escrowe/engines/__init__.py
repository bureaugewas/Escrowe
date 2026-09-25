"""One engine per database kind. To add a kind: write a module here that
subclasses DirectEngine (or DBAPIEngine) and add it to REGISTRY."""

from __future__ import annotations

from .base import Column, DBAPIEngine, DirectEngine, EngineError
from .ducklake import DuckDBEngine, DuckLakeEngine
from .iceberg import IcebergEngine
from .mysql import MySQLEngine
from .postgres import PostgresEngine
from .sqlite import SQLiteEngine
from .sqlserver import SQLServerEngine

REGISTRY: dict[str, type[DirectEngine]] = {
    "mysql": MySQLEngine,
    "postgres": PostgresEngine,
    "sqlserver": SQLServerEngine,
    "sqlite": SQLiteEngine,
    "duckdb": DuckDBEngine,
    "ducklake": DuckLakeEngine,
    "iceberg": IcebergEngine,
}


def kinds() -> tuple[str, ...]:
    return tuple(REGISTRY)


def build(kind: str, **params) -> DirectEngine:
    try:
        cls = REGISTRY[kind]
    except KeyError:
        raise EngineError(f"Unknown database kind {kind!r}; choose from {', '.join(REGISTRY)}")
    return cls(**params)


__all__ = ["Column", "DBAPIEngine", "DirectEngine", "EngineError", "REGISTRY", "build", "kinds"]
