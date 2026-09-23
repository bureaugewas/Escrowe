"""Runtime settings, read from environment variables (and a `.env` file in
the working directory or in the escrowe home directory)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"


@dataclass
class Settings:
    home: Path
    jwt_secret: str | None = None
    llm_provider: str = "auto"            # auto, or one of agent.PROVIDERS
    llm_model: str | None = None          # None: the chosen vendor's own default
    llm_thinking_budget: int = 0          # >0 requests extended thinking (API-key providers only)
    max_rows: int = 1000
    query_timeout_s: float = 30.0
    agent_attempts: int = 3
    token_ttl_s: int = 12 * 3600
    server_url: str = DEFAULT_SERVER_URL
    api_enabled: bool = False             # `escrowe serve` refuses to start unless set
    feed_enabled: bool = True             # allow \feed (query results back to the LLM)

    @property
    def store_path(self) -> Path:
        return self.home / "escrowe.sqlite"

    @property
    def transcript_path(self) -> Path:
        return self.home / "llm-transcript.jsonl"

    @property
    def notebooks_dir(self) -> Path:
        return self.home / "notebooks"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def home_dir() -> Path:
    return Path(os.environ.get("ESCROWE_HOME", Path.home() / ".escrowe")).expanduser()


def load_settings() -> Settings:
    _load_dotenv(Path.cwd() / ".env")
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)   # holds the audit log and, possibly, an API key
    except OSError:
        pass
    _load_dotenv(home / ".env")
    env = os.environ.get
    return Settings(
        home=home,
        jwt_secret=env("ESCROWE_JWT_SECRET"),
        llm_provider=env("ESCROWE_LLM", "auto"),
        llm_model=env("ESCROWE_MODEL"),
        llm_thinking_budget=int(env("ESCROWE_LLM_THINKING", "0")),
        max_rows=int(env("ESCROWE_MAX_ROWS", "1000")),
        query_timeout_s=float(env("ESCROWE_QUERY_TIMEOUT", "30")),
        agent_attempts=int(env("ESCROWE_AGENT_ATTEMPTS", "3")),
        server_url=env("ESCROWE_SERVER", DEFAULT_SERVER_URL),
        api_enabled=_truthy(env("ESCROWE_API_ENABLED")),
        feed_enabled=_truthy(env("ESCROWE_FEED_ENABLED", "1")),
    )
