"""Command-line entry point."""

from __future__ import annotations

import sys

from . import runner
from .logs import _log
from .progress import err


def main() -> int:
    try:
        return runner.main()
    except KeyboardInterrupt:
        err("Aborted by user (KeyboardInterrupt)")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        err(f"Fatal: {exc!r}")
        _log(f"Fatal: {exc!r}", "ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
