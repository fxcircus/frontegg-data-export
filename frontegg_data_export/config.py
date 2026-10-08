"""Configuration loader: `.env` file in the app dir + process env-var fallback."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DOTENV_PATH = APP_DIR / ".env"


def data_dir() -> Path:
    """Settings, logs and (where no OS store exists) the stored key live here:
    `<app>/data`, or $FDE_HOME."""
    return Path(os.environ.get("FDE_HOME") or APP_DIR / "data").expanduser()


def default_output_dir() -> Path:
    return APP_DIR / "exports"

REQUIRED_VARS = ("FRONTEGG_CLIENT_ID", "FRONTEGG_CLIENT_SECRET", "FRONTEGG_BASE_URL")

# Requests per second. Frontegg's general limit is 100/min per IP on the Launch
# plan and 1,000/min per IP on Scale and Enterprise, shared with any other
# traffic from the same IP. gentle (90/min) fits under Launch; normal (240/min)
# is about a quarter of the Scale/Enterprise budget; fast (720/min) leaves
# little room for anything else on that IP.
RATE_PRESETS = {"gentle": 1.5, "normal": 4.0, "fast": 12.0}
DEFAULT_RATE = "normal"
MIN_RATE, MAX_RATE = 0.5, 16.0


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


def load_config(path: Path) -> dict[str, str]:
    """Reads credentials from `.env` next to the app, falling back to
    process environment variables for any value not present in the file.
    """
    env: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    for k in REQUIRED_VARS:
        if not env.get(k):
            env[k] = os.environ.get(k, "")
    missing = [k for k in REQUIRED_VARS if not env.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required configuration: {', '.join(missing)}\n\n"
            "Either:\n"
            "  (a) copy .env.example to .env and fill in the values:\n"
            "        cp .env.example .env\n"
            "  (b) export them as environment variables before running:\n"
            "        export FRONTEGG_CLIENT_ID=...\n"
            "        export FRONTEGG_CLIENT_SECRET=...\n"
            "        export FRONTEGG_BASE_URL=https://api.frontegg.com\n"
        )
    return env
