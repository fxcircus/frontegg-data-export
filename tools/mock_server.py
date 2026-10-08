#!/usr/bin/env python3
"""Run the mock Frontegg API for manual checks. Test tooling only; it never
ships in the zip.

    python3 tools/mock_server.py                 # first state of the fake environment
    python3 tools/mock_server.py --variant 2     # same environment after a set of changes
    python3 tools/mock_server.py --fail-role-lookups 2 --audits forbidden

Point the app at the printed base URL with the printed Client ID and API key.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.mock_frontegg import CLIENT_ID, CLIENT_SECRET, Faults, MockFrontegg, make_dataset, mutate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--tenants", type=int, default=12)
    ap.add_argument("--big-tenant-users", type=int, default=230)
    ap.add_argument("--variant", type=int, choices=(1, 2), default=1,
                    help="2 applies a known set of changes, for trying the diff")
    ap.add_argument("--fail-role-lookups", type=int, default=0, metavar="N",
                    help="role lookups always fail for N accounts (gives a partial run)")
    ap.add_argument("--audits", choices=("ok", "forbidden", "not_found", "error"), default="ok")
    ap.add_argument("--flaky", type=int, default=0, metavar="N", help="every Nth GET returns 503")
    ap.add_argument("--rate-limit-every", type=int, default=0, metavar="N", help="every Nth GET returns 429")
    args = ap.parse_args()

    # Anchor generated timestamps to today's midnight (UTC) so restarting the
    # mock (for example to switch to --variant 2) serves the same data, and the
    # diff shows only the deliberate changes.
    anchor = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    ds = make_dataset(seed=args.seed, tenants=args.tenants, big_tenant_users=args.big_tenant_users, now=anchor)
    if args.variant == 2:
        mutate(ds, now=datetime.now(timezone.utc))
    faults = Faults(audits_mode=args.audits, error_5xx_every=args.flaky, rate_limit_every=args.rate_limit_every,
                    retry_after="1")
    small = [t["tenantId"] for t in ds.tenants if t["name"] != "Acme Big"]
    faults.failing_role_tenants = set(small[2:2 + args.fail_role_lookups])

    mock = MockFrontegg(ds, faults).start(port=args.port)
    print(f"Mock Frontegg API running (variant {args.variant})")
    print(f"  Base URL : {mock.url}")
    print(f"  Client ID: {CLIENT_ID}")
    print(f"  API key  : {CLIENT_SECRET}")
    print(f"  {len(ds.users)} users, {len(ds.tenants)} accounts, {len(ds.entitlements)} plan assignments")
    print("Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
