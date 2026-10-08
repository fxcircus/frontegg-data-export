"""Terminal styling (TTY only); every line is mirrored to the log."""

from __future__ import annotations

import sys

from .logs import _log

USE_COLOR = sys.stdout.isatty()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s
def bold(s: str) -> str: return _c("1", s)
def green(s: str) -> str: return _c("32", s)
def red(s: str) -> str: return _c("31", s)
def yellow(s: str) -> str: return _c("33", s)
def cyan(s: str) -> str: return _c("36", s)
def gray(s: str) -> str: return _c("90", s)


def banner(title: str, subtitle: str = "") -> None:
    print()
    print(bold("=" * 72))
    print(bold(f" {title}"))
    if subtitle:
        print(gray(f" {subtitle}"))
    print(bold("=" * 72))
    _log(f"=== {title} {subtitle} ===")

def step(n: int, total: int, title: str) -> None:
    print()
    print(bold(f"[{n}/{total}] {title}"))
    _log(f"STEP {n}/{total}: {title}")

def info(msg: str) -> None:
    print(f"      {msg}")
    _log(msg)

def progress(msg: str) -> None:
    print(f"      {gray(msg)}")
    _log(f"PROGRESS: {msg}")

def ok(msg: str) -> None:
    print(f"      {green('✓')} {msg}")
    _log(f"OK: {msg}")

def warn(msg: str) -> None:
    print(f"      {yellow('!')} {msg}")
    _log(msg, "WARN")

def err(msg: str) -> None:
    print(f"      {red('✗')} {msg}", file=sys.stderr)
    _log(msg, "ERROR")
