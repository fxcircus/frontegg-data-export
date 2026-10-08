"""Command-line entry point."""

from __future__ import annotations

import sys

from . import runner
from .logs import _log
from .status import EXIT_INTERRUPTED


def main() -> int:
    try:
        return runner.main()
    except KeyboardInterrupt:
        print("Stopped before the export finished (Ctrl-C).", file=sys.stderr)
        return EXIT_INTERRUPTED
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Fatal: {exc!r}", file=sys.stderr)
        _log(f"Fatal: {exc!r}", "ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
