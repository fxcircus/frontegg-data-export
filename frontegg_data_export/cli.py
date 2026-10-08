"""Command-line entry point."""

from __future__ import annotations

import sys

from . import runner
from .client import ApiError
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
    except ApiError as exc:
        err(exc.message)
        _log(f"Fatal: {exc.message} (HTTP {exc.status}, path={exc.path}, trace={exc.trace_id})", "ERROR")
        return 1
    except Exception as exc:
        err(f"Fatal: {exc!r}")
        _log(f"Fatal: {exc!r}", "ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
