"""An append-only record of every prompt escrowe sends to an LLM.

Escrowe claims the agent never sees your data. This file is how you check
that claim: each call is written at the moment it is sent, with the exact
system prompt and user message, not a reconstruction. If a value from your
data ever reached a model, it would be in here.

One JSONL file, one object per line. The reader functions below extract
narrower views from it (just the questions, or a line-oriented context
log) so nothing needs to be written twice.

The file is created readable only by you: the schema, the column comments
and the questions people ask are worth protecting on their own.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
_lock = threading.Lock()


class Transcript:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, provider: str, model: str, system: str, user: str,
               response: str | None, duration_ms: float, error: str | None = None,
               context: dict | None = None, thinking: str | None = None) -> None:
        context = context or {}
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": provider,
            "model": model,
            "user": context.get("user"),
            "session": context.get("session"),
            "question": context.get("question"),
            "attempt": context.get("attempt"),
            "duration_ms": round(duration_ms, 1),
            "sent": {"system": system, "user": user},
            "received": response,
            "thinking": thinking,
            "error": error,
            "sent_chars": len(system) + len(user),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _lock:
            self._rotate_if_large()
            is_new = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if is_new:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass

    def _rotate_if_large(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
        except OSError:
            pass

    def read(self, limit: int = 20) -> list[dict]:
        """The most recent entries, oldest first. limit=0 means all."""
        if not self.path.exists():
            return []
        entries = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries[-limit:] if limit else entries


# ------------------------------------------------------------------ views

def _cell(text: str | None) -> str:
    return (text or "").replace("\t", "    ")


def questions(entries: list[dict]) -> list[str]:
    """Just what people asked, once per question (retries share the question).
    Tab-separated: timestamp, session, question."""
    return [f"{e['ts']}\t{e.get('session') or '-'}\t{_cell(e['question'])}"
            for e in entries if e.get("question") and (e.get("attempt") or 1) == 1]


def context_lines(entries: list[dict]) -> list[str]:
    """The conversation as tab-separated rows: timestamp, session, who, text.
    `who` is user (the question), thought (extended thinking, where the
    provider exposes it) or agent (the reply). One row per line of text, so
    the output can be grepped. The system prompt is left out; it repeats the
    same schema on every call and is in the JSONL in full."""
    out = []
    for e in entries:
        prefix = f"{e['ts']}\t{e.get('session') or '-'}"
        parts = [("user", e.get("question")), ("thought", e.get("thinking")),
                 ("agent", f"ERROR: {e['error']}" if e.get("error") else e.get("received") or "(none)")]
        for who, text in parts:
            if text:
                out += [f"{prefix}\t{who}\t{_cell(line)}" for line in text.splitlines() or [""]]
    return out
