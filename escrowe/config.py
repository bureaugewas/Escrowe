"""Runtime configuration, read from environment variables (or a .env file).

Everything is env-driven so the same code runs on a laptop and on a VM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass
class Attachment:
    """The one database escrowe connects to.

    kind: mysql (see escrowe.engines for what's registered)
    spec: a key=value connection string, e.g. "host=127.0.0.1 user=ro password=x database=shop"
    """

    name: str
    kind: str
    spec: str


@dataclass
class Settings:
    home: Path
    jwt_secret: str | None
    attachments: list[Attachment] = field(default_factory=list)
    llm_provider: str = "auto"            # auto | anthropic | claude-cli | mock
    llm_model: str = "claude-opus-5"
    llm_thinking_budget: int = 0          # >0 requests extended thinking (anthropic provider only)
    max_rows: int = 1000
    query_timeout_s: float = 30.0
    agent_attempts: int = 3
    token_ttl_s: int = 12 * 3600
    server_url: str = "http://127.0.0.1:8765"

    @property
    def store_path(self) -> Path:
        return self.home / "escrowe.sqlite"


def parse_attachments(raw: str | None) -> list[Attachment]:
    """ESCROWE_ATTACH="shop=mysql:host=127.0.0.1 user=ro password=x port=3306 database=shop"
    Entries are separated by ';', each is name=kind:spec."""
    out: list[Attachment] = []
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, _, rest = entry.partition("=")
        kind, _, spec = rest.partition(":")
        if not (name and kind and spec):
            raise ValueError(f"Bad ESCROWE_ATTACH entry: {entry!r} (want name=kind:spec)")
        out.append(Attachment(name.strip(), kind.strip().lower(), spec.strip()))
    return out


def mysql_attachment_from_env(env: dict | None = None, name: str = "mysql") -> Attachment | None:
    """Standard MySQL client variables → an attachment, so `export MYSQL_HOST=...
    MYSQL_USER=... MYSQL_PWD=...` is all a MySQL user has to do."""
    env = os.environ if env is None else env
    host = env.get("MYSQL_HOST")
    if not host:
        return None
    parts = [f"host={host}", f"port={env.get('MYSQL_PORT', '3306')}"]
    if env.get("MYSQL_USER"):
        parts.append(f"user={env['MYSQL_USER']}")
    pwd = env.get("MYSQL_PWD") or env.get("MYSQL_PASSWORD")
    if pwd:
        parts.append(f"password={pwd}")
    if env.get("MYSQL_DATABASE"):
        parts.append(f"database={env['MYSQL_DATABASE']}")
    return Attachment(env.get("MYSQL_ATTACH_NAME", name), "mysql", " ".join(parts))


def load_settings(ephemeral: bool | None = None) -> Settings:
    _load_dotenv(Path.cwd() / ".env")
    if ephemeral is None:
        ephemeral = os.environ.get("ESCROWE_EPHEMERAL", "").lower() in ("1", "true", "yes")
    if ephemeral:
        # Nothing survives the session: a throwaway home, removed when the process
        # exits. No credential, no audit and no transcript are left on disk.
        import atexit, shutil, tempfile
        home = Path(tempfile.mkdtemp(prefix="escrowe-ephemeral-"))
        atexit.register(lambda: shutil.rmtree(home, ignore_errors=True))
    else:
        home = Path(os.environ.get("ESCROWE_HOME", Path.home() / ".escrowe")).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    _load_dotenv(home / ".env")
    attachments = parse_attachments(os.environ.get("ESCROWE_ATTACH"))
    mysql = mysql_attachment_from_env()
    if mysql and mysql.name not in {a.name for a in attachments}:
        attachments.append(mysql)
    return Settings(
        home=home,
        jwt_secret=os.environ.get("ESCROWE_JWT_SECRET"),
        attachments=attachments,
        llm_provider=os.environ.get("ESCROWE_LLM", "auto"),
        llm_model=os.environ.get("ESCROWE_MODEL", "claude-opus-5"),
        llm_thinking_budget=int(os.environ.get("ESCROWE_LLM_THINKING", "0")),
        max_rows=int(os.environ.get("ESCROWE_MAX_ROWS", "1000")),
        query_timeout_s=float(os.environ.get("ESCROWE_QUERY_TIMEOUT", "30")),
        agent_attempts=int(os.environ.get("ESCROWE_AGENT_ATTEMPTS", "3")),
        server_url=os.environ.get("ESCROWE_SERVER", "http://127.0.0.1:8765"),
    )
