"""Runtime settings store for API keys and provider config.

Lets the owner enter keys from the dashboard instead of editing environment
variables. Values persist to a JSON file in the data volume (NOT committed,
chmod 600) and are applied to ``os.environ`` so the providers, key checks, and
integrations pick them up. Secrets are never logged and are masked when read
back to the UI.

This is for a personal paper-trading container. Keys live in the container's
mounted volume only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DATA_DIR = os.environ.get("DAYTRADER_DATA_DIR") or os.path.dirname(
    os.environ.get("DAYTRADER_DB_PATH", "")) or "/home/user/algotrader/cache"
SETTINGS_PATH = Path(_DATA_DIR) / "settings.json"

# Secret keys (masked in the UI) and plain config (shown as-is).
SECRET_KEYS = [
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "DASHSCOPE_API_KEY",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TASTYTRADE_PASSWORD",
    "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL",
]
PLAIN_KEYS = [
    "CLAUDE_MODEL", "OPENAI_MODEL", "XAI_MODEL", "QWEN_MODEL",
    "OPENAI_BASE_URL", "XAI_BASE_URL", "QWEN_BASE_URL",
    "TASTYTRADE_USERNAME",
    "GITHUB_REPO", "ALPACA_PAPER", "ALPACA_DATA_PLAN",
]
MANAGED_KEYS = SECRET_KEYS + PLAIN_KEYS


def load() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:  # noqa: BLE001 - missing/corrupt file
        return {}


def apply_to_env() -> None:
    """Push stored settings into the process environment (does not overwrite
    a value already set in the real environment, so explicit env vars win)."""
    data = load()
    for k, v in data.items():
        if k in MANAGED_KEYS and v not in (None, "") and not os.environ.get(k):
            os.environ[k] = str(v)


def save(updates: dict) -> dict:
    """Merge non-empty updates into the store and apply them. Empty string for a
    secret means 'leave unchanged'; the literal '__CLEAR__' deletes a key."""
    data = load()
    for k, v in updates.items():
        if k not in MANAGED_KEYS:
            continue
        if v == "__CLEAR__":
            data.pop(k, None)
            os.environ.pop(k, None)
        elif v not in (None, ""):
            data[k] = str(v)
            os.environ[k] = str(v)
    Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass
    return masked_status()


def _mask(val: str) -> str:
    if not val:
        return ""
    return ("•" * max(0, len(val) - 4)) + val[-4:] if len(val) > 4 else "••••"


def masked_status() -> dict:
    """UI-safe view: secrets masked (set flag + hint), plain config shown."""
    # Effective values = stored file merged under real env (env wins).
    data = load()
    out = {}
    for k in SECRET_KEYS:
        val = os.environ.get(k) or data.get(k) or ""
        out[k] = {"set": bool(val), "hint": _mask(val), "secret": True}
    for k in PLAIN_KEYS:
        out[k] = {"value": os.environ.get(k) or data.get(k) or "", "secret": False}
    return out


# Apply stored settings as soon as this module is imported anywhere.
apply_to_env()
