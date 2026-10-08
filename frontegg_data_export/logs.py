"""Append-only per-call audit log."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import LOG_PATH

_log_fp = None


def _log(msg: str, level: str = "INFO") -> None:
    global _log_fp
    if _log_fp is None:
        _log_fp = open(LOG_PATH, "a", encoding="utf-8")
        _log_fp.write(f"\n{'=' * 72}\n=== run start {datetime.now(timezone.utc).isoformat()} ===\n{'=' * 72}\n")
        _log_fp.flush()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _log_fp.write(f"{ts} [{level}] {msg}\n")
    _log_fp.flush()
