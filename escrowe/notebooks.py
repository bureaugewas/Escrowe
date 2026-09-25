"""Notebooks for the browser UI: named JSON files under the escrowe home.

A notebook is its cells (SQL or question text, chart settings, and the
last result so reopening it needs no query and no agent call) plus,
optionally, which database it was written against and its chart palette. Only the keys listed
here survive a save, and a source's secrets are always stripped.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .sources import SECRET_KEYS

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
CELL_KEYS = ("id", "kind", "title", "text", "chart", "result")
CELL_KINDS = ("sql", "ask", "md")
MAX_CELLS = 200
MAX_TEXT = 20_000
MAX_RESULT_BYTES = 2_000_000       # per cell, so a huge cached result cannot balloon the file


class NotebookError(ValueError):
    pass


def clean_cells(cells: list) -> list[dict]:
    if len(cells) > MAX_CELLS:
        raise NotebookError(f"At most {MAX_CELLS} cells per notebook.")
    out = []
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("kind") not in CELL_KINDS:
            raise NotebookError("Each cell needs a kind: sql, ask or md.")
        kept = {k: cell[k] for k in CELL_KEYS if k in cell}
        for key in ("id", "title", "text"):
            if key in kept and not isinstance(kept[key], str):
                raise NotebookError(f"Cell {key} must be text.")
        if len(kept.get("text", "")) > MAX_TEXT:
            raise NotebookError(f"A cell's text is limited to {MAX_TEXT} characters.")
        if "chart" in kept and not isinstance(kept["chart"], dict):
            raise NotebookError("Cell chart config must be an object.")
        if "result" in kept:
            if kept["result"] is not None and not isinstance(kept["result"], dict):
                raise NotebookError("Cell result must be an object.")
            if len(json.dumps(kept["result"])) > MAX_RESULT_BYTES:
                raise NotebookError(f"A cell's cached result is too large to save "
                                    f"(limit {MAX_RESULT_BYTES // 1_000_000} MB).")
        out.append(kept)
    return out


def clean_source(source: dict | None) -> dict | None:
    """Name, kind and non-secret params only. None means the notebook follows
    whatever database is currently connected."""
    if not source:
        return None
    if not isinstance(source, dict) or not source.get("kind"):
        raise NotebookError("A notebook's source needs at least a kind.")
    params = source.get("params") or {}
    if not isinstance(params, dict):
        raise NotebookError("A notebook's source params must be an object.")
    return {"name": source.get("name") or source["kind"], "kind": source["kind"],
            "params": {k: v for k, v in params.items() if k not in SECRET_KEYS}}


class Notebooks:
    def __init__(self, directory: Path):
        self.dir = directory

    def _path(self, name: str) -> Path:
        if not NAME.match(name):
            raise NotebookError("Notebook names: letters, digits, spaces, . _ - (max 64).")
        return self.dir / f"{name}.json"

    def list(self) -> list[dict]:
        items = []
        if self.dir.is_dir():
            for f in sorted(self.dir.glob("*.json")):
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                items.append({"name": f.stem, "cells": len(d.get("cells", [])), "saved_at": d.get("saved_at"),
                              "saved_by": d.get("saved_by"), "source": d.get("source")})
        return items

    def get(self, name: str) -> dict | None:
        path = self._path(name)
        if not path.is_file():
            return None
        return json.loads(path.read_text())

    def save(self, name: str, cells: list, source: dict | None, saved_by: str,
             palette: str | None = None) -> dict:
        path = self._path(name)
        if palette is not None and (not isinstance(palette, str) or len(palette) > 40):
            raise NotebookError("A notebook's palette is a short name.")
        doc = {"name": name, "cells": clean_cells(cells), "source": clean_source(source), "palette": palette,
               "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "saved_by": saved_by}
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(path)
        return doc

    def delete(self, name: str) -> None:
        path = self._path(name)
        if path.is_file():
            path.unlink()
