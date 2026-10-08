"""Command-line interface. The local app and the scheduler run these same
commands, so there is one code path.

    python3 -m frontegg_data_export run [--preset users|standard|full] ...
    python3 -m frontegg_data_export estimate
    python3 -m frontegg_data_export diff --from previous --to latest
    python3 -m frontegg_data_export runs
    python3 -m frontegg_data_export test-connection

Exit codes: 0 succeeded, 2 partial, 1 failed, 64 usage error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import __version__, runner
from .client import ApiError
from .config import (
    ConfigError,
    RATE_PRESETS,
    load_credentials,
    load_settings,
    output_dir,
    parse_rate,
    rate_label,
    data_dir,
)
from .diff import CHANGES_HEADER, diff_models, summary_lines
from .logs import app_logger
from .sections import PRESETS, SECTIONS, format_duration, resolve
from .status import EXIT_CODES, EXIT_INTERRUPTED, EXIT_USAGE, FAILED
from .store import Store, StoreError, read_json


class Parser(argparse.ArgumentParser):
    """argparse exits with 2 on usage errors; 2 means "partial" here, so use 64."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _sections_arg(value: str) -> list[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in SECTIONS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown section(s) {', '.join(bad)}; choose from {', '.join(SECTIONS)}")
    return items


def _formats_arg(value: str) -> tuple[str, ...]:
    items = tuple(v.strip().lower() for v in value.split(",") if v.strip())
    if not items or any(v not in ("csv", "json") for v in items):
        raise argparse.ArgumentTypeError("choose csv, json or csv,json")
    return items


def _rate_arg(value: str) -> str:
    try:
        parse_rate(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return value


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--preset", choices=list(PRESETS),
                   help="users (Users only), standard (Users, accounts and plans), full (Full backup). "
                        "Default: from Setup, else standard.")
    p.add_argument("--sections", type=_sections_arg, metavar="A,B",
                   help=f"export exactly these sections instead of a preset: {', '.join(SECTIONS)}")
    p.add_argument("--roles", dest="roles", action="store_true", default=None,
                   help="look up each user's roles per account (the slowest step)")
    p.add_argument("--no-roles", dest="roles", action="store_false", help="skip role lookups")
    p.add_argument("--login-events", dest="login_events", action="store_true", default=None,
                   help="also export successful and failed logins from the audit log (one call per account)")
    p.add_argument("--no-login-events", dest="login_events", action="store_false", help="skip login events")
    p.add_argument("--rate", type=_rate_arg, metavar="RATE",
                   help=f"requests per second: {', '.join(f'{k} ({v:g}/s)' for k, v in RATE_PRESETS.items())}, "
                        "or a number from 0.5 to 16")
    p.add_argument("--out", metavar="DIR", help="output folder (default: from Setup, else ./exports)")


def build_parser() -> Parser:
    ap = Parser(prog="python3 -m frontegg_data_export",
                description="Read-only export of a Frontegg environment to CSV and JSON.",
                epilog="Exit codes: 0 succeeded, 2 partial, 1 failed, 64 usage error.")
    ap.add_argument("--version", action="version", version=f"Frontegg Data Export {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)
    sub.required = True

    run = sub.add_parser("run", help="run an export")
    _add_selection(run)
    run.add_argument("--format", dest="formats", type=_formats_arg, default=("csv", "json"), metavar="csv,json",
                     help="which outputs to write (default: csv,json)")
    run.add_argument("--since", metavar="DATE",
                     help="login events from this ISO 8601 date (default 'last': since the previous succeeded run)")
    run.add_argument("--login-events-max-days", type=int, metavar="N",
                     help="never read login events further back than N days (default: from Setup, else 30)")
    run.add_argument("--keep", type=int, metavar="N", help="keep the last N runs (default: from Setup, else 30)")
    run.add_argument("--quiet", action="store_true", help="print only the result line")
    run.add_argument("--progress", choices=("console", "jsonl", "quiet"),
                     help="progress output: console (default), jsonl (machine-readable), quiet")
    run.add_argument("--use-as-baseline", action="store_true",
                     help="if the run is partial, still use it as the baseline for the next comparison")
    run.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--trigger", choices=("manual", "scheduled", "app"), default=None, help=argparse.SUPPRESS)

    est = sub.add_parser("estimate", help="estimate API calls and duration before running")
    _add_selection(est)
    est.add_argument("--probe", action="store_true",
                     help="with no previous run, make 3 quick read calls to count users, accounts and plans")
    est.add_argument("--json", action="store_true", help="print JSON")

    d = sub.add_parser("diff", help="compare two runs")
    d.add_argument("--from", dest="from_run", default="previous", metavar="RUN",
                   help="run ID, folder, latest, previous or baseline (default: previous)")
    d.add_argument("--to", dest="to_run", default="latest", metavar="RUN", help="(default: latest)")
    d.add_argument("--csv", metavar="FILE", help="also write the changes to this CSV file")
    d.add_argument("--json", action="store_true", help="print JSON")
    d.add_argument("--out", metavar="DIR", help="output folder that holds the runs")

    r = sub.add_parser("runs", help="list previous runs")
    r.add_argument("--json", action="store_true", help="print JSON")
    r.add_argument("--out", metavar="DIR", help="output folder that holds the runs")

    t = sub.add_parser("test-connection", help="check the region, Client ID and API key")
    t.add_argument("--json", action="store_true", help="print JSON")
    return ap


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    trigger = args.trigger or ("scheduled" if args.scheduled else "manual")
    progress = args.progress or ("quiet" if (args.quiet or args.scheduled) else "console")
    return runner.main(preset=args.preset, sections=args.sections, roles=args.roles,
                       login_events=args.login_events, since=args.since,
                       login_events_max_days=args.login_events_max_days, rate=args.rate,
                       progress=progress, out_dir=args.out, keep=args.keep, trigger=trigger,
                       use_as_baseline=args.use_as_baseline, formats=args.formats)


def cmd_estimate(args: argparse.Namespace) -> int:
    settings = load_settings()
    selection = resolve(None if args.sections else (args.preset or settings["preset"]), args.sections,
                        roles=settings["roles"] if args.roles is None else args.roles,
                        login_events=(settings["loginEvents"] if (args.login_events is None and not args.sections)
                                      else args.login_events))
    rate = parse_rate(args.rate if args.rate is not None else settings["rate"])
    creds = load_credentials(settings=settings) if args.probe else None
    est = runner.estimate_for(selection, rate, Store(output_dir(args.out, settings)), probe=args.probe,
                              credentials=creds)
    if args.json:
        print(json.dumps({**est.to_dict(), "preset": selection.preset, "sections": list(selection.sections),
                          "rate": rate, "durationText": format_duration(est.seconds)}))
        return 0
    if est.basis == "unknown":
        print("No previous run to estimate from. Run with --probe to count records first (3 quick read calls).")
    print(f"{selection.label}: about {est.calls:,} API calls, {format_duration(est.seconds)} "
          f"at {rate_label(rate)}. Based on: {est.basis}.")
    for step, n in est.per_step.items():
        print(f"  {step:<14} {n:>8,}")
    for note in est.notes:
        print(f"  Note: {note}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    store = Store(output_dir(args.out))
    try:
        a, b = store.resolve_run(args.from_run), store.resolve_run(args.to_run)
    except StoreError as e:
        print(str(e), file=sys.stderr)
        return 1
    old, new = read_json(a / "normalized.json"), read_json(b / "normalized.json")
    if old is None or new is None:
        print("One of those runs has no data to compare (it may have failed).", file=sys.stderr)
        return 1
    changes, summary = diff_models(old, new)
    if args.csv:
        from .csvout import write_csv
        write_csv(Path(args.csv), CHANGES_HEADER, (c.row() for c in changes))
    if args.json:
        print(json.dumps({"from": a.name, "to": b.name, **summary, "changes": [c.to_dict() for c in changes]},
                         ensure_ascii=False))
        return 0
    print(f"Changes from {a.name} to {b.name}: {summary['total']}")
    for line in summary_lines(summary):
        print("  " + line)
    for note in summary["notCompared"]:
        print(f"  Not compared: {note}")
    if args.csv:
        print(f"Wrote {args.csv}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    store = Store(output_dir(args.out))
    h = store.load_history()
    if args.json:
        print(json.dumps(h, ensure_ascii=False))
        return 0
    if not h["runs"]:
        print(f"No runs yet in {store.root}.")
        return 0
    print(f"{'run':<22} {'status':<10} {'preset':<9} {'time':>7} {'users':>8} {'accounts':>9} {'calls':>7}")
    for r in h["runs"]:
        mark = " (baseline)" if r["runId"] == h.get("baselineRunId") else ""
        c = r.get("counts") or {}
        print(f"{r['runId']:<22} {r.get('status', ''):<10} {r.get('preset', ''):<9} "
              f"{r.get('durationSeconds', 0):>6}s {c.get('users', ''):>8} {c.get('accounts', ''):>9} "
              f"{r.get('apiCalls', ''):>7}{mark}")
    return 0


def cmd_test_connection(args: argparse.Namespace) -> int:
    result = runner.test_connection(load_credentials())
    if args.json:
        print(json.dumps(result))
    else:
        print(result["message"], file=sys.stdout if result["ok"] else sys.stderr)
        if result.get("traceId") and not result["ok"]:
            print(f"Trace ID: {result['traceId']}", file=sys.stderr)
    return 0 if result["ok"] else 1


COMMANDS = {"run": cmd_run, "estimate": cmd_estimate, "diff": cmd_diff, "runs": cmd_runs,
            "test-connection": cmd_test_connection}


def _sigterm_to_interrupt(signum, frame):  # the local app stops a run with SIGTERM
    raise KeyboardInterrupt


def _utf8_streams() -> None:
    """Names and the progress glyphs are Unicode. On Windows, output that's
    redirected to a file or pipe would otherwise use the ANSI code page and
    crash on the first character it can't encode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    args = build_parser().parse_args(argv)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CODES[FAILED]
    except ApiError as e:
        print(e.message + (f" (trace ID {e.trace_id})" if e.trace_id else ""), file=sys.stderr)
        return EXIT_CODES[FAILED]
    except KeyboardInterrupt:
        print("Stopped before finishing (interrupted).", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        print(f"Unexpected error: {exc!r}", file=sys.stderr)
        app_logger(data_dir() / "logs").exception("Unexpected error in the command line")
        return EXIT_CODES[FAILED]


if __name__ == "__main__":
    sys.exit(main())
