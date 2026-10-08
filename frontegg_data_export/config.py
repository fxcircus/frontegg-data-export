"""Configuration loader: `.env` file in the app dir + process env-var fallback."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DOTENV_PATH = APP_DIR / ".env"
LOG_PATH = APP_DIR / "export.log"

REQUIRED_VARS = ("FRONTEGG_CLIENT_ID", "FRONTEGG_CLIENT_SECRET", "FRONTEGG_BASE_URL")


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
