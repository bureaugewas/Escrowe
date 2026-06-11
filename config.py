"""Runtime configuration: which DuckDB file to govern and which LLM to use.

Stored in config.yaml next to this file (gitignored — it contains local paths).
"""

import os
import threading

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

DEFAULTS = {
    "database_path": None,   # None → built-in sample data.duckdb
    "llm_provider": "auto",  # auto / claude / ollama / mock
}

_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(yaml.safe_load(f) or {})
    return cfg


def save_config(updates: dict) -> dict:
    with _lock:
        cfg = load_config()
        cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        return cfg
