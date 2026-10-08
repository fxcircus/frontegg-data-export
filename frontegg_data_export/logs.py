"""Logs: one log per run (in that run's folder, so retention removes it with
the run) and a small rotating app log for the local app and the scheduler.

Nothing secret reaches a log. Every line passes through `REDACTOR`, which
replaces registered secret values (the API key, every access token the
client mints) and anything that looks like a bearer token with [redacted].
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

REDACTED = "[redacted]"
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
APP_LOG_MAX_BYTES = 1_000_000
APP_LOG_BACKUPS = 3


class Redactor:
    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._values.add(value)

    def __call__(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for v in values:
            if v in text:
                text = text.replace(v, REDACTED)
        return _BEARER.sub(r"\1" + REDACTED, text)


REDACTOR = Redactor()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RunLog:
    """A plain-text log for one run: `log(message, level)`."""

    def __init__(self, path: Path, redactor: Redactor = REDACTOR) -> None:
        self.path = Path(path)
        self._redact = redactor
        self._fp = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, msg: str, level: str = "INFO") -> None:
        line = self._redact(f"{_ts()} [{level}] {msg}")
        with self._lock:
            if self._fp:
                self._fp.write(line + "\n")
                self._fp.flush()

    def close(self) -> None:
        with self._lock:
            if self._fp:
                self._fp.close()
                self._fp = None


def null_log(msg: str, level: str = "INFO") -> None:
    pass


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = REDACTOR(record.getMessage())
        record.args = None
        return True


def app_logger(log_dir: Path, name: str = "frontegg_data_export") -> logging.Logger:
    """The rotating app log (1 MB x 3) in `log_dir/app.log`."""
    logger = logging.getLogger(name)
    target = str(Path(log_dir) / "app.log")
    if any(getattr(h, "baseFilename", None) == target for h in logger.handlers):
        return logger
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(target, maxBytes=APP_LOG_MAX_BYTES,
                                                   backupCount=APP_LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    handler.addFilter(_RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
