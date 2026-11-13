"""Settings stored on this computer only.

Everything personal (name, contact details, API keys) lives in
``data/settings.json``, which is git-ignored and never leaves the machine.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CFA_DATA_DIR", ROOT / "data"))
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "leads.db"

HOST = "127.0.0.1"
PORT = int(os.environ.get("CFA_PORT", "8765"))

DEFAULT_SERVICES = [
    "AI & automation",
    "Website development & redesign",
    "App development",
    "Data analysis",
]

# Fields the UI may read and write.
PUBLIC_DEFAULTS: dict[str, Any] = {
    "your_name": "",
    "business_name": "",
    "email": "",
    "whatsapp": "",
    "portfolio_url": "",
    "services": DEFAULT_SERVICES,
    "portfolio": [],
    "use_pagespeed": False,
}

# Fields the UI may write but never read back.
SECRET_KEYS = ("google_api_key", "claude_api_key")

_lock = threading.Lock()


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_raw() -> dict[str, Any]:
    try:
        # utf-8-sig tolerates a file saved by Notepad with a byte-order mark.
        with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_raw(data: dict[str, Any]) -> None:
    ensure_data_dir()
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS_PATH)


def _clean_str(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _clean_services(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_SERVICES)
    out = [_clean_str(v, 120) for v in value]
    return [v for v in out if v][:20]


def _clean_portfolio(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value[:30]:
        if not isinstance(item, dict):
            continue
        entry = {
            "name": _clean_str(item.get("name"), 120),
            "description": _clean_str(item.get("description"), 400),
            "url": _clean_str(item.get("url"), 400),
        }
        if entry["name"] or entry["url"]:
            out.append(entry)
    return out


def public_settings() -> dict[str, Any]:
    """Settings as the UI sees them: no keys, only whether a key is saved."""
    raw = _read_raw()
    out: dict[str, Any] = {}
    for key, default in PUBLIC_DEFAULTS.items():
        out[key] = raw.get(key, default)
    out["services"] = _clean_services(out["services"])
    out["portfolio"] = _clean_portfolio(out["portfolio"])
    out["use_pagespeed"] = bool(out["use_pagespeed"])
    out["has_google_key"] = bool(get_secret("google_api_key"))
    out["has_claude_key"] = bool(get_secret("claude_api_key"))
    return out


def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        raw = _read_raw()
        for key in ("your_name", "business_name", "email", "whatsapp", "portfolio_url"):
            if key in changes:
                raw[key] = _clean_str(changes[key])
        if "services" in changes:
            raw["services"] = _clean_services(changes["services"])
        if "portfolio" in changes:
            raw["portfolio"] = _clean_portfolio(changes["portfolio"])
        if "use_pagespeed" in changes:
            raw["use_pagespeed"] = bool(changes["use_pagespeed"])
        for key in SECRET_KEYS:
            value = changes.get(key)
            # Empty or missing means "keep what is saved".
            if isinstance(value, str) and value.strip():
                raw[key] = value.strip()
        _write_raw(raw)
    return public_settings()


def get_secret(name: str) -> str:
    """Read a key from settings.json, falling back to an environment variable."""
    value = _read_raw().get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    env_name = {"google_api_key": "GOOGLE_API_KEY", "claude_api_key": "ANTHROPIC_API_KEY"}.get(name, "")
    return os.environ.get(env_name, "").strip() if env_name else ""


def get_setting(name: str) -> Any:
    return public_settings().get(name)
