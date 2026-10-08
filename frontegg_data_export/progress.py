"""Progress reporting for a run.

One `Reporter` per run, in one of three modes:
- "console": readable progress for a terminal (colored when it's a TTY);
- "jsonl":   one JSON object per line on stdout, for the local app to stream;
- "quiet":   nothing but the final result line (for schedulers).

Every event is also written to the run log.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, TextIO

MODES = ("console", "jsonl", "quiet")


def _styler(stream: TextIO) -> Callable[[str, str], str]:
    color = hasattr(stream, "isatty") and stream.isatty()
    return lambda code, s: f"\033[{code}m{s}\033[0m" if color else s


class Reporter:
    def __init__(self, mode: str = "console", stream: TextIO | None = None,
                 err_stream: TextIO | None = None, log: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if mode not in MODES:
            raise ValueError(f"progress mode must be one of {MODES}")
        self.mode = mode
        self.out = stream or sys.stdout
        self.err = err_stream or sys.stderr
        self._log = log or (lambda msg, level="INFO": None)
        self._clock = clock
        self._t0 = clock()
        self._style = _styler(self.out)
        self.api_calls: Callable[[], int] = lambda: 0
        self.warnings: list[dict] = []
        self._last_progress = 0.0

    def set_log(self, log: Callable[[str, str], None]) -> None:
        self._log = log

    # ---- low level --------------------------------------------------------
    def _emit(self, event: str, **fields: Any) -> None:
        if self.mode != "jsonl":
            return
        payload = {"event": event, "elapsed": round(self._clock() - self._t0, 2),
                   "apiCalls": self.api_calls(), **fields}
        self.out.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self.out.flush()

    def _print(self, line: str = "", err: bool = False) -> None:
        if self.mode == "console":
            print(line, file=self.err if err else self.out, flush=True)

    # ---- events -----------------------------------------------------------
    def run_started(self, title: str, subtitle: str, **fields: Any) -> None:
        self._log(f"=== {title} {subtitle} ===", "INFO")
        self._emit("run_started", **fields)
        b = lambda s: self._style("1", s)  # noqa: E731
        self._print()
        self._print(b("=" * 72))
        self._print(b(f" {title}"))
        self._print(self._style("90", f" {subtitle}"))
        self._print(b("=" * 72))

    def info(self, message: str) -> None:
        self._log(message, "INFO")
        self._emit("info", message=message)
        self._print(f"      {message}")

    def step(self, key: str, label: str, index: int, total: int) -> None:
        self._log(f"STEP {index}/{total}: {label}", "INFO")
        self._emit("step_started", step=key, label=label, index=index, total=total)
        self._print()
        self._print(self._style("1", f"[{index}/{total}] {label}"))

    def progress(self, key: str, done: int, total: int | None = None, unit: str = "",
                 note: str = "", force: bool = False) -> None:
        """Throttled to about one line per second in console/jsonl output."""
        now = self._clock()
        if not force and now - self._last_progress < 1.0:
            return
        self._last_progress = now
        text = f"{unit} {done}/{total}" if total is not None else f"{unit} {done}"
        if note:
            text += f"  {note}"
        self._log(f"PROGRESS {key}: {text.strip()}", "INFO")
        self._emit("progress", step=key, done=done, total=total, unit=unit, note=note)
        self._print(f"      {self._style('90', text.strip())}")

    def step_done(self, key: str, message: str, **fields: Any) -> None:
        self._log(f"OK: {message}", "INFO")
        self._emit("step_finished", step=key, message=message, **fields)
        self._print(f"      {self._style('32', '✓')} {message}")

    def warn(self, message: str, **context: Any) -> None:
        self._log(message + "".join(f" {k}={v}" for k, v in context.items() if v), "WARN")
        self.warnings.append({"message": message, **context})
        self._emit("warning", message=message, **context)
        self._print(f"      {self._style('33', '!')} {message}")

    def error(self, message: str, **context: Any) -> None:
        self._log(message + "".join(f" {k}={v}" for k, v in context.items() if v), "ERROR")
        self._emit("error", message=message, **context)
        if self.mode == "console":
            self._print(f"      {self._style('31', '✗')} {message}", err=True)
        elif self.mode == "quiet":
            print(message, file=self.err, flush=True)

    def run_finished(self, status: str, headline: str, details: str, **fields: Any) -> None:
        self._log(f"=== {headline} {details} ===", "INFO")
        self._emit("run_finished", status=status, headline=headline, **fields)
        if self.mode == "quiet":
            print(f"{status}: {headline}", file=self.out, flush=True)
            return
        b = lambda s: self._style("1", s)  # noqa: E731
        self._print()
        self._print(b("=" * 72))
        self._print(b(f" {headline}"))
        self._print(self._style("90", f" {details}"))
        self._print(b("=" * 72))
