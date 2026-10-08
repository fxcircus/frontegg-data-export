"""Paths, regions, rate presets, settings, and where credentials come from.

Credentials are resolved field by field, first match wins:
  1. environment variables FRONTEGG_BASE_URL / FRONTEGG_CLIENT_ID / FRONTEGG_CLIENT_SECRET
  2. a `.env` file next to the app, or the file $FDE_DOTENV names (for
     engineers; the app never creates one)
  3. the stored setup: settings.json for the region and Client ID, and the
     OS credential store (or a private file) for the API key.

settings.json holds nothing secret.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

APP_DIR = Path(__file__).resolve().parent.parent


def dotenv_path() -> Path:
    """`.env` next to the app, or the file named by $FDE_DOTENV."""
    return Path(os.environ.get("FDE_DOTENV") or APP_DIR / ".env").expanduser()

ENV_BASE_URL, ENV_CLIENT_ID, ENV_SECRET = "FRONTEGG_BASE_URL", "FRONTEGG_CLIENT_ID", "FRONTEGG_CLIENT_SECRET"

# Where to find the credentials, in Frontegg's current portal.
CREDENTIALS_LOCATION = ("Frontegg Portal → your environment → Keys & domains, which shows the "
                        "Client ID and the API key")

REGIONS = {
    "eu": {"label": "EU", "baseUrl": "https://api.frontegg.com"},
    "us": {"label": "US", "baseUrl": "https://api.us.frontegg.com"},
    "ca": {"label": "CA", "baseUrl": "https://api.ca.frontegg.com"},
    "au": {"label": "AU", "baseUrl": "https://api.au.frontegg.com"},
}

# Requests per second. Frontegg's general limit is 100/min per IP on the Launch
# plan and 1,000/min per IP on Scale and Enterprise, shared with any other
# traffic from the same IP. gentle (90/min) fits under Launch; normal (240/min)
# is about a quarter of the Scale/Enterprise budget; fast (720/min) leaves
# little room for anything else on that IP.
RATE_PRESETS = {"gentle": 1.5, "normal": 4.0, "fast": 12.0}
DEFAULT_RATE = "normal"
MIN_RATE, MAX_RATE = 0.5, 16.0

SETTINGS_DEFAULTS: dict = {
    "baseUrl": "",
    "clientId": "",
    "preset": "standard",
    "roles": True,
    "loginEvents": False,
    "loginEventsMaxDays": 30,
    "rate": DEFAULT_RATE,
    "outputDir": "",
    "keepRuns": 30,
}


class ConfigError(Exception):
    """Something the person needs to set up. The message says how."""


def data_dir() -> Path:
    """Settings, logs and (where no OS store exists) the stored key live here:
    `<app>/data`, or $FDE_HOME."""
    return Path(os.environ.get("FDE_HOME") or APP_DIR / "data").expanduser()


def default_output_dir() -> Path:
    return APP_DIR / "exports"


def parse_rate(value: str | float | None) -> float:
    """A preset name or a number of requests per second."""
    if value is None or value == "":
        return RATE_PRESETS[DEFAULT_RATE]
    if isinstance(value, str) and value.strip().lower() in RATE_PRESETS:
        return RATE_PRESETS[value.strip().lower()]
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Rate must be gentle, normal, fast or a number of requests per second, not {value!r}")
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError(f"Rate must be between {MIN_RATE} and {MAX_RATE} requests per second")
    return rate


def rate_label(value: str | float | None) -> str:
    rate = parse_rate(value)
    for name, r in RATE_PRESETS.items():
        if r == rate:
            return f"{name} ({r:g}/s)"
    return f"{rate:g}/s"


# --------------------------------------------------------------------------- #
# settings.json
# --------------------------------------------------------------------------- #
def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict:
    try:
        stored = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        stored = {}
    except ValueError:
        raise ConfigError(f"{settings_path()} isn't valid JSON. Fix it or delete it and run Setup again.")
    return {**SETTINGS_DEFAULTS, **stored}


def save_settings(updates: dict) -> dict:
    from .store import atomic_write_json

    current = load_settings()
    current.update(updates)
    current.setdefault("installId", "")
    if not current["installId"]:
        current["installId"] = _secrets.token_hex(4)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, current)
    return current


def install_id() -> str:
    s = load_settings()
    return s.get("installId") or save_settings({})["installId"]


def output_dir(override: str | Path | None = None, settings: dict | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    s = settings if settings is not None else load_settings()
    return Path(s["outputDir"]).expanduser() if s.get("outputDir") else default_output_dir()


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def read_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return env
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@dataclass
class Credentials:
    base_url: str
    client_id: str
    secret: str = field(repr=False)
    sources: dict[str, str] = field(default_factory=dict)


def load_credentials(*, dotenv: Path | None = None, settings: dict | None = None,
                     secret_lookup: Callable[[], str | None] | None = None,
                     require_secret: bool = True) -> Credentials:
    from .client import validate_base_url

    s = settings if settings is not None else load_settings()
    dot = read_dotenv(dotenv if dotenv is not None else dotenv_path())
    sources: dict[str, str] = {}

    def pick(env_key: str, stored: Callable[[], str | None], name: str) -> str:
        if os.environ.get(env_key):
            sources[name] = "environment"
            return os.environ[env_key]
        if dot.get(env_key):
            sources[name] = ".env"
            return dot[env_key]
        value = stored()
        if value:
            sources[name] = "setup"
        return value or ""

    base_url = pick(ENV_BASE_URL, lambda: s.get("baseUrl"), "baseUrl")
    client_id = pick(ENV_CLIENT_ID, lambda: s.get("clientId"), "clientId")
    secret = pick(ENV_SECRET, secret_lookup or (lambda: None), "secret")

    missing = [label for label, v in (("the region (API base URL)", base_url), ("the Client ID", client_id),
                                      ("the API key", secret if require_secret else "x")) if not v]
    if missing:
        raise ConfigError(
            f"Missing {', '.join(missing)}.\n\n"
            "Set it up in the app's Setup tab, or for headless use set environment variables:\n"
            f"  {ENV_BASE_URL}=https://api.frontegg.com   (or .us / .ca / .au)\n"
            f"  {ENV_CLIENT_ID}=...\n"
            f"  {ENV_SECRET}=...\n"
            f"Find the Client ID and API key in {CREDENTIALS_LOCATION}.")
    try:
        base_url = validate_base_url(base_url)
    except ValueError as e:
        raise ConfigError(f"{e}. Choose a region in Setup, or fix {ENV_BASE_URL}.") from None
    return Credentials(base_url=base_url, client_id=client_id, secret=secret, sources=sources)
