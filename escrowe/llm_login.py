"""Connecting an LLM to escrowe.

Two vendors - Claude and ChatGPT - and for each the same two ways in, which
bill differently, the whole reason there are two:

  browser   The vendor's own CLI (Claude Code, Codex) signs in to your account
            in a browser, and escrowe asks it for queries. This uses the
            subscription you already pay for. No API bill.
  api_key   An API key, billed per token. Stored in escrowe's catalog, which
            is created readable only by you.

Checking a browser login is free: both CLIs answer `status` from local state
and make no API call, so escrowe can verify one at every start without
spending anything.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

VENDOR_SETTING = "llm_provider"      # store key: which vendor the person picked
METHOD_SETTING = "llm_method"        # store key: browser | api_key


def _claude_signed_in(proc: subprocess.CompletedProcess) -> bool:
    """`claude auth status` answers in JSON."""
    try:
        return bool(json.loads(proc.stdout).get("loggedIn"))
    except Exception:
        return False


def _codex_signed_in(proc: subprocess.CompletedProcess) -> bool:
    """`codex login status` says "Logged in using ChatGPT" (or "... an API
    key", if that is how codex itself was set up), and "Not logged in" when
    nobody is. It says it on stderr, not stdout."""
    said = (proc.stdout + proc.stderr).lower()
    return proc.returncode == 0 and "logged in" in said and "not logged in" not in said


@dataclass(frozen=True)
class Vendor:
    name: str                        # claude | chatgpt
    label: str                       # what to call it in a sentence
    cli: str                         # the CLI that owns the browser login
    cli_label: str
    cli_env: str                     # env var that overrides the binary
    status_args: tuple[str, ...]
    login_args: tuple[str, ...]
    logout_args: tuple[str, ...]
    signed_in: Callable[[subprocess.CompletedProcess], bool]
    npm: str
    brew: tuple[str, ...]
    download: str
    api_key_setting: str
    api_key_env: str
    cli_provider: str                # agent provider for the browser login
    api_provider: str                # agent provider for the API key
    model: str                       # default model for the API provider
    sdk: str                         # the package the API provider imports


VENDORS: dict[str, Vendor] = {
    "claude": Vendor(
        name="claude", label="Claude", cli="claude", cli_label="Claude Code", cli_env="CLAUDE_BIN",
        status_args=("auth", "status"), login_args=("auth", "login"), logout_args=("auth", "logout"),
        signed_in=_claude_signed_in,
        npm="@anthropic-ai/claude-code", brew=("--cask", "claude-code"),
        download="https://claude.ai/download",
        api_key_setting="anthropic_api_key", api_key_env="ANTHROPIC_API_KEY",
        cli_provider="claude-cli", api_provider="anthropic", model="claude-opus-5", sdk="anthropic"),
    "chatgpt": Vendor(
        name="chatgpt", label="ChatGPT", cli="codex", cli_label="Codex", cli_env="CODEX_BIN",
        status_args=("login", "status"), login_args=("login",), logout_args=("logout",),
        signed_in=_codex_signed_in,
        npm="@openai/codex", brew=("codex",),
        download="https://developers.openai.com/codex",
        api_key_setting="openai_api_key", api_key_env="OPENAI_API_KEY",
        cli_provider="codex-cli", api_provider="openai", model="gpt-6-astra", sdk="openai"),
}

DEFAULT_VENDOR = "claude"


def vendor(name: str | None = None) -> Vendor:
    return VENDORS.get(name or "", VENDORS[DEFAULT_VENDOR])


def for_provider(provider: str) -> Vendor | None:
    """Which vendor an agent provider name belongs to, if any."""
    return next((v for v in VENDORS.values() if provider in (v.cli_provider, v.api_provider)), None)


def install_hint(v: Vendor) -> str:
    return f"""{v.cli_label} is what signs in to your {v.label} account.

  npm:    npm install -g {v.npm}
  brew:   brew install {' '.join(v.brew)}
  or see  {v.download}"""


# ------------------------------------------------------------ browser login

def binary(v: Vendor) -> str:
    return os.environ.get(v.cli_env, v.cli)


def installed(v: Vendor) -> bool:
    return shutil.which(binary(v)) is not None


def browser_status(v: Vendor) -> dict:
    """Is the CLI signed in? Free to ask: it reads local state, not the API."""
    if not installed(v):
        return {"installed": False, "logged_in": False}
    try:
        proc = subprocess.run([binary(v), *v.status_args], capture_output=True, text=True, timeout=30)
    except Exception:
        return {"installed": True, "logged_in": False}
    return {"installed": True, "logged_in": v.signed_in(proc)}


def install_command(v: Vendor) -> list[str] | None:
    if shutil.which("npm"):
        return ["npm", "install", "-g", v.npm]
    if platform.system() == "Darwin" and shutil.which("brew"):
        return ["brew", "install", *v.brew]
    return None


def install_cli(v: Vendor) -> None:
    cmd = install_command(v)
    if cmd is None:
        raise RuntimeError(install_hint(v))
    proc = subprocess.run(cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed.")
    if not installed(v):
        raise RuntimeError(f"{v.cli_label} installed but is not on your PATH yet. "
                           "Open a new terminal and try again.")


def ensure_cli(v: Vendor, ask) -> bool:
    """`ask(cmd)` returns True to run the install."""
    if installed(v):
        return True
    cmd = install_command(v)
    if cmd is None:
        raise RuntimeError(install_hint(v))
    if not ask(cmd):
        return False
    install_cli(v)
    return True


def browser_login(v: Vendor) -> dict:
    """The CLI's own login, with the terminal inherited so the browser opens."""
    if not installed(v):
        raise RuntimeError(install_hint(v))
    cmd = [binary(v), *v.login_args]
    try:
        proc = subprocess.run(cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    except OSError as e:
        raise RuntimeError(f"Could not run `{' '.join(cmd)}`: {e}")
    if proc.returncode != 0:
        raise RuntimeError("The browser login did not complete.")
    st = browser_status(v)
    if not st["logged_in"]:
        raise RuntimeError(f"The login finished but {v.cli_label} is still signed out.")
    return st


def browser_logout(v: Vendor) -> None:
    if installed(v):
        subprocess.run([binary(v), *v.logout_args], capture_output=True)


# ---------------------------------------------------------------- api key

def stored_api_key(store=None, name: str | None = None) -> str | None:
    v = vendor(name)
    if store is not None:
        value = store.setting(v.api_key_setting)
        if value:
            return value
    return os.environ.get(v.api_key_env) or None


def check_api_key(v: Vendor, key: str) -> tuple[bool, str]:
    """Validate a key with a request that returns no tokens and costs nothing."""
    try:
        sdk = __import__(v.sdk)
    except ImportError:
        return True, f"stored (the {v.sdk} package is not installed, so it was not checked)"
    client = sdk.Anthropic(api_key=key) if v.sdk == "anthropic" else sdk.OpenAI(api_key=key)
    try:
        client.models.list()
    except Exception as e:
        return False, str(e).splitlines()[0][:160]
    return True, "ok"


def save_api_key(store, v: Vendor, key: str) -> None:
    store.set_setting(v.api_key_setting, key.strip())


def forget_api_key(store, v: Vendor | None = None) -> None:
    for each in [v] if v else VENDORS.values():
        store.set_setting(each.api_key_setting, "")


# ------------------------------------------------------------------ status

def chosen_vendor(store=None) -> str | None:
    return (store.setting(VENDOR_SETTING) or None) if store is not None else None


def _source(store, v: Vendor) -> str:
    """Which way in is used. The person's own choice wins when it still works;
    otherwise the browser login does, because it does not bill per token."""
    have = {"browser": browser_status(v)["logged_in"], "api_key": bool(stored_api_key(store, v.name))}
    picked = store.setting(METHOD_SETTING) if store is not None else None
    if have.get(picked):
        return picked
    return next((m for m, ok in have.items() if ok), "none")


def status(store=None, name: str | None = None) -> dict:
    """What would answer a question right now, and what would it cost?

    Without a vendor, the one the person picked; failing that, whichever is
    actually connected."""
    name = name or chosen_vendor(store)
    if name is None:
        name = next((n for n in VENDORS if _source(store, VENDORS[n]) != "none"), DEFAULT_VENDOR)
    v = vendor(name)
    source = _source(store, v)
    cli = browser_status(v)
    return {"vendor": v.name, "label": v.label, "source": source, "connected": source != "none",
            "cli": binary(v), "cli_label": v.cli_label, "cli_installed": cli["installed"],
            "logged_in": cli["logged_in"], "api_key": bool(stored_api_key(store, v.name)),
            "api_key_from_env": bool(os.environ.get(v.api_key_env)), "api_key_env": v.api_key_env,
            "provider": {"browser": v.cli_provider, "api_key": v.api_provider, "none": "mock"}[source],
            "model": v.model}


def describe(st: dict | None = None, store=None) -> str:
    st = st or status(store)
    return {
        "browser": f"{st['label']} is connected through your {st['label']} subscription.",
        "api_key": f"{st['label']} is connected with an API key, billed per token.",
        "none": "No LLM is connected.",
    }[st["source"]]
