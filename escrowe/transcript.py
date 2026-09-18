"""Three append-only records of everything escrowe sends to an LLM.

escrowe claims the agent never sees your data. These files are how you check
that claim rather than take it on faith: every call is written down at the
moment it is sent, with the exact system prompt and the exact user message,
not a reconstruction. If a value from your data ever reached a model, it
would be in here.

  llm-transcript.jsonl   One JSON object per line - the full record (system
                         prompt, schema, SQL written, reply), for scripts
                         and `escrowe llm-log`.
  llm-context.log        The same calls as tab-separated rows:
                         timestamp \t session_id \t who \t text
                         where `who` is one of user / thought / agent, one
                         row per physical line - meant to be tailed, grepped
                         or opened in a spreadsheet, not parsed as JSON.
                         `user` is the question itself, not the schema/system
                         prompt that goes with it on every call (that's in
                         llm-transcript.jsonl in full; repeating it here on
                         every line would bury the actual question). `thought`
                         rows are the model's extended-thinking text where
                         the provider exposes it (the anthropic/API-key
                         provider only - the Claude Code CLI subscription
                         path has no way to surface it, so those rows are
                         simply absent there). `agent` is the reply.
  llm-questions.log      Just the questions people asked: tab-separated
                         timestamp \t session_id \t question, nothing else -
                         no schema, no SQL, no reply. The other two logs
                         necessarily include table/column names (and, in the
                         reply, the SQL the agent wrote against them), which
                         some may consider sensitive on its own; this file
                         is for when only "what did people ask" is wanted.
                         One line per question, written once, not once per
                         retry attempt.

All three created readable only by you, because the schema, the column
comments and the questions people ask are themselves worth protecting.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

FILENAME = "llm-transcript.jsonl"
CONTEXT_LOG_FILENAME = "llm-context.log"
QUESTIONS_LOG_FILENAME = "llm-questions.log"
_lock = threading.Lock()


class Transcript:
    def __init__(self, path: Path | str, max_bytes: int = 64 * 1024 * 1024,
                 context_log_path: Path | str | None = None, questions_log_path: Path | str | None = None):
        self.path = Path(path)
        self.context_log_path = Path(context_log_path) if context_log_path else self.path.with_name(CONTEXT_LOG_FILENAME)
        self.questions_log_path = Path(questions_log_path) if questions_log_path else self.path.with_name(QUESTIONS_LOG_FILENAME)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, provider: str, model: str, system: str, user: str,
               response: str | None, duration_ms: float, error: str | None = None,
               context: dict | None = None, thinking: str | None = None) -> None:
        context = context or {}
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
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
        with _lock:
            self._write_jsonl(entry)
            self._write_context_log(entry)
            self._write_questions_log(entry)

    def _write_jsonl(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        self._rotate_if_large(self.path, ".jsonl.1")
        newfile = not self.path.exists()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if newfile:
            self._lock_down(self.path)

    def _write_context_log(self, entry: dict) -> None:
        """Tab-separated: timestamp, session id, who (user/thought/agent),
        text - one row per physical line, so the file stays line-oriented
        and greppable instead of one giant escaped blob per call.

        `user` is the question itself, not the system prompt: the schema is
        sent fresh on every call (see the catalog cache in service.py) and
        would otherwise dominate this file with the same block repeated for
        every question. It's still in llm-transcript.jsonl in full, for
        whoever actually needs to see exactly what was sent."""
        ts = entry["ts"]
        sid = entry["session"] or "-"

        def rows(who: str, text: str | None):
            for ln in (text or "").splitlines() or [""]:
                yield f"{ts}\t{sid}\t{who}\t{ln.replace(chr(9), '    ')}"   # keep columns intact

        lines = []
        if entry["question"]:
            lines += list(rows("user", entry["question"]))
        if entry["thinking"]:
            lines += list(rows("thought", entry["thinking"]))
        if entry["error"]:
            lines += list(rows("agent", f"ERROR: {entry['error']}"))
        else:
            lines += list(rows("agent", entry["received"] or "(none)"))

        self._rotate_if_large(self.context_log_path, ".log.1")
        newfile = not self.context_log_path.exists()
        with self.context_log_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if newfile:
            self._lock_down(self.context_log_path)

    def _write_questions_log(self, entry: dict) -> None:
        """Just the question, once - not on retry attempts (attempt > 1 is
        the same question again, with feedback from the failed try)."""
        question = entry["question"]
        if not question or (entry["attempt"] or 1) != 1:
            return
        line = f"{entry['ts']}\t{entry['session'] or '-'}\t{question.replace(chr(9), '    ')}"
        self._rotate_if_large(self.questions_log_path, ".log.1")
        newfile = not self.questions_log_path.exists()
        with self.questions_log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if newfile:
            self._lock_down(self.questions_log_path)

    @staticmethod
    def _lock_down(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _rotate_if_large(path: Path, rotated_suffix: str) -> None:
        try:
            if path.exists() and path.stat().st_size > 64 * 1024 * 1024:
                path.replace(path.with_suffix(rotated_suffix))
        except OSError:
            pass

    def read(self, limit: int = 20) -> list[dict]:
        """The most recent entries from the JSONL log, newest last."""
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out[-limit:] if limit else out


def default_path(home: Path | str) -> Path:
    return Path(home) / FILENAME


def default_context_log_path(home: Path | str) -> Path:
    return Path(home) / CONTEXT_LOG_FILENAME


def default_questions_log_path(home: Path | str) -> Path:
    return Path(home) / QUESTIONS_LOG_FILENAME
