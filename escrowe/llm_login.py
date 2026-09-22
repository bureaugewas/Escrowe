"""Connecting Claude to escrowe.

Two ways, and they bill differently, which is the whole reason there are two:

  subscription  Claude Code signs in to your Anthropic account in a browser and
                escrowe asks it for queries. This uses your Claude subscription,
                the same one you use interactively. No API bill.
  api_key       An Anthropic API key, billed per token. Stored in escrowe's
                catalog, which is created readable only by you.

Checking the subscription is free: `claude auth status` reads local state and
makes no API call, so escrowe can verify it at every start without spending
anything.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys

CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
API_KEY_SETTING = "anthropic_api_key"

INSTALL_HINT = """Claude Code is what signs in to your Anthropic account.

  npm:    npm install -g @anthropic-ai/claude-code
  macOS:  brew install --cask claude-code
  or see  https://claude.ai/download"""


def claude_installed() -> bool:
    return shutil.which(CLAUDE) is not None


def subscription_status() -> dict:
    """Is Claude Code signed in? Free to ask: it reads local state, not the API."""
    if not claude_installed():
        return {"installed": False, "logged_in": False, "method": None}
    try:
        proc = subprocess.run([CLAUDE, "auth", "status"], capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout)
    except Exception:
        return {"installed": True, "logged_in": False, "method": None}
    return {"installed": True, "logged_in": bool(data.get("loggedIn")),
            "method": data.get("authMethod"), "provider": data.get("apiProvider")}


def stored_api_key(store=None) -> str | None:
    if store is not None:
        value = store.setting(API_KEY_SETTING)
        if value:
            return value
    return os.environ.get("ANTHROPIC_API_KEY") or None


def status(store=None) -> dict:
    """What would answer a question right now, and what would it cost?"""
    sub = subscription_status()
    key = stored_api_key(store)
    from_env = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if sub["logged_in"]:
        source = "subscription"
    elif key:
        source = "api_key"
    else:
        source = "none"
    return {"source": source, "connected": source != "none",
            "claude_installed": sub["installed"], "logged_in": sub["logged_in"],
            "api_key": bool(key), "api_key_from_env": from_env,
            # Agent._resolve() checks the api key BEFORE the subscription, so an api
            # key set alongside a subscription login actually wins in practice even
            # though `source` above reports "subscription" - this is what's true.
            "shadowed": bool(key) and sub["logged_in"]}


def describe(st: dict | None = None, store=None) -> str:
    st = st or status(store)
    return {
        "subscription": "Claude is connected through your Claude subscription.",
        "api_key": "Claude is connected with an API key, billed per token.",
        "none": "Claude is not connected.",
    }[st["source"]]


# ------------------------------------------------------------------ subscription

def install_command() -> list[str] | None:
    if shutil.which("npm"):
        return ["npm", "install", "-g", "@anthropic-ai/claude-code"]
    if platform.system() == "Darwin" and shutil.which("brew"):
        return ["brew", "install", "--cask", "claude-code"]
    return None


def install_claude_code() -> None:
    cmd = install_command()
    if cmd is None:
        raise RuntimeError(INSTALL_HINT)
    proc = subprocess.run(cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed.")
    if not claude_installed():
        raise RuntimeError("Claude Code installed but is not on your PATH yet. "
                           "Open a new terminal and try again.")


def ensure_claude_code(ask) -> bool:
    """`ask(cmd)` returns True to run the install."""
    if claude_installed():
        return True
    cmd = install_command()
    if cmd is None:
        raise RuntimeError(INSTALL_HINT)
    if not ask(cmd):
        return False
    install_claude_code()
    return True


def subscription_login() -> dict:
    """`claude auth login`, with the terminal inherited so the browser opens."""
    if not claude_installed():
        raise RuntimeError(INSTALL_HINT)
    try:
        proc = subprocess.run([CLAUDE, "auth", "login"], stdin=sys.stdin,
                              stdout=sys.stdout, stderr=sys.stderr)
    except OSError as e:
        raise RuntimeError(f"Could not run `{CLAUDE} auth login`: {e}")
    if proc.returncode != 0:
        raise RuntimeError("The browser login did not complete.")
    st = subscription_status()
    if not st["logged_in"]:
        raise RuntimeError("The login finished but Claude Code is still signed out.")
    return st


def subscription_logout() -> None:
    if claude_installed():
        subprocess.run([CLAUDE, "auth", "logout"], capture_output=True)


# ---------------------------------------------------------------------- api key

def check_api_key(key: str) -> tuple[bool, str]:
    """Validate a key with a request that returns no tokens and costs nothing."""
    try:
        import anthropic
    except ImportError:
        return True, "stored (the anthropic package is not installed, so it was not checked)"
    try:
        anthropic.Anthropic(api_key=key).models.list()
    except Exception as e:
        return False, str(e).splitlines()[0][:160]
    return True, "ok"


def save_api_key(store, key: str) -> None:
    store.set_setting(API_KEY_SETTING, key.strip())


def forget_api_key(store) -> None:
    store.set_setting(API_KEY_SETTING, "")
