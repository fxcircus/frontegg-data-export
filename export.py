#!/usr/bin/env python3
"""
Frontegg Account Backup — full read-only export of a Frontegg environment.

Produces a single JSON file containing the complete state of your Frontegg
environment:
    - tenants (accounts)
    - hierarchy trees (reseller-rooted)
    - users (with tenant memberships, metadata, vendorMetadata)
    - role catalog (each role's permissions[])
    - permission catalog
    - plans (enriched — includes assignedTenantsCount, assignedUsersCount, featuresCount)
    - features
    - feature flags
    - per-(user, tenant) role assignments
    - per-(tenant or user, plan) entitlement assignments

Quick start:
    1. cp .env.example .env
    2. fill in FRONTEGG_CLIENT_ID, FRONTEGG_CLIENT_SECRET, FRONTEGG_BASE_URL
    3. python3 export.py

Output (written next to this script):
    - frontegg_account_backup_<YYYYMMDDTHHMMSSZ>.json   (single self-contained file)
    - export.log                                          (per-call audit trail)

Dependencies: Python 3.10+ stdlib only (urllib, json, os). No `pip install` step.

Safety: this script only issues `GET` requests (and a single `POST /auth/vendor/`
for authentication). It does NOT create, update, or delete any Frontegg resource.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Tuning constants — values reflect live-verified Frontegg API behaviour.
# Most you should NOT need to change. Each comment notes why the value is what
# it is so you can adjust safely if your environment differs.
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
DOTENV_PATH = SCRIPT_DIR / ".env"
LOG_PATH = SCRIPT_DIR / "export.log"

THROTTLE_SEC = 0.08           # 80 ms between calls = ~12 req/sec ceiling. Frontegg's documented default is 1000 req/min/IP for Scale/Enterprise tiers — we run at ~25% of that headroom.
HTTP_TIMEOUT = 30
PAGE_SIZE_TENANTS = 200       # max accepted by /tenants/v2. NB: `_offset` on this endpoint is a PAGE INDEX (0..totalPages-1), not an item offset.
PAGE_SIZE_USERS = 200         # max accepted by /users/v3. Same page-index `_offset` quirk.
ENTITLEMENTS_LIMIT = 10       # /entitlements/v2 silently caps `limit` at 10. Values above this return 0 items. We paginate until empty.
ROLES_BATCH_SIZE = 100        # docs allow up to 250 user IDs per /v3/roles call, but the resulting URL (~9 KB) trips HTTP 414 "URI Too Large" on big tenants. 100 keeps the URL under ~4 KB.
SAFETY_OFFSET_CAP = 200_000   # ceiling for the entitlements page-until-empty loop. Raise this if your environment has more than 200K entitlement records.


# --------------------------------------------------------------------------- #
# Terminal styling (TTY only) + log file
# --------------------------------------------------------------------------- #
USE_COLOR = sys.stdout.isatty()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s
def bold(s: str) -> str: return _c("1", s)
def green(s: str) -> str: return _c("32", s)
def red(s: str) -> str: return _c("31", s)
def yellow(s: str) -> str: return _c("33", s)
def cyan(s: str) -> str: return _c("36", s)
def gray(s: str) -> str: return _c("90", s)

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


# --------------------------------------------------------------------------- #
# Configuration loader: .env file in script dir + process env-var fallback
# --------------------------------------------------------------------------- #
REQUIRED_VARS = ("FRONTEGG_CLIENT_ID", "FRONTEGG_CLIENT_SECRET", "FRONTEGG_BASE_URL")

def load_config(path: Path) -> dict[str, str]:
    """Reads credentials from `.env` next to the script, falling back to
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


# --------------------------------------------------------------------------- #
# HTTP client with throttling, retries, re-auth, rate-limit accounting
# --------------------------------------------------------------------------- #
class FronteggClient:
    def __init__(self, base_url: str, client_id: str, secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.secret = secret
        self.token: str | None = None
        self.token_expires_at: float = 0.0
        # running stats
        self.calls = 0
        self.errors = 0
        self.h429 = 0
        self.rate_limit_headers_seen = 0
        self.last_trace_id = ""

    def authenticate(self) -> None:
        url = f"{self.base_url}/auth/vendor/"
        body = json.dumps({"clientId": self.client_id, "secret": self.secret}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Vendor authentication failed: HTTP {e.code} — {e.read()[:300]!r}")
        self.token = payload["token"]
        expires_in = int(payload.get("expiresIn", 3600))
        self.token_expires_at = time.time() + expires_in - 60  # 60s safety margin
        _log(f"AUTH OK expiresIn={expires_in}s elapsed={time.time()-t0:.2f}s")

    def _maybe_reauth(self) -> None:
        if not self.token or time.time() >= self.token_expires_at:
            _log("Re-authenticating (token expiring)…", "WARN")
            self.authenticate()

    def get(self, path: str, params: dict | None = None, tenant_id: str | None = None) -> Any:
        self._maybe_reauth()
        return self._request("GET", path, params=params, tenant_id=tenant_id)

    def _request(self, method: str, path: str, params: dict | None = None,
                 tenant_id: str | None = None, attempt: int = 0) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if tenant_id is not None:
            headers["frontegg-tenant-id"] = tenant_id
        req = urllib.request.Request(url, headers=headers, method=method)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
                self.calls += 1
                elapsed = time.time() - t0
                self.last_trace_id = resp.headers.get("frontegg-trace-id", "")
                rl_limit = resp.headers.get("x-rate-limit-limit", "")
                if rl_limit:
                    self.rate_limit_headers_seen += 1
                _log(f"{method} {url} -> {resp.status} {elapsed:.2f}s trace={self.last_trace_id} rl={rl_limit}")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            elapsed = time.time() - t0
            preview = (e.read() or b"")[:300].decode("utf-8", errors="replace")
            if e.code == 401 and attempt == 0:
                _log(f"{method} {url} -> 401, re-auth then retry", "WARN")
                self.authenticate()
                return self._request(method, path, params=params, tenant_id=tenant_id, attempt=attempt + 1)
            if e.code == 429:
                self.h429 += 1
                retry_after = int(e.headers.get("retry-after") or 0) or 5 * (attempt + 1)
                _log(f"{method} {url} -> 429 retry_after={retry_after}s attempt={attempt}", "WARN")
                if attempt < 4:
                    time.sleep(retry_after)
                    return self._request(method, path, params=params, tenant_id=tenant_id, attempt=attempt + 1)
            self.errors += 1
            _log(f"{method} {url} -> {e.code} {preview}", "ERROR")
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 4:
                wait_s = min(2 ** attempt, 30)
                _log(f"{method} {url} network error: {e}; sleeping {wait_s}s and retrying", "WARN")
                time.sleep(wait_s)
                return self._request(method, path, params=params, tenant_id=tenant_id, attempt=attempt + 1)
            self.errors += 1
            _log(f"{method} {url} gave up after retries: {e}", "ERROR")
            raise


# --------------------------------------------------------------------------- #
# Pull helpers — each one encodes a specific Frontegg pagination quirk.
# --------------------------------------------------------------------------- #
def pull_pages_by_pageindex(
    client: FronteggClient,
    path: str,
    page_size: int,
    label: str,
) -> list[dict]:
    """For /tenants/v2 and /users/v3 where `_offset` is a PAGE INDEX
    (0..totalPages-1) and NOT an item offset."""
    page = 0
    all_items: list[dict] = []
    total = None
    while True:
        resp = client.get(path, {"_limit": page_size, "_offset": page})
        items = resp.get("items", [])
        all_items.extend(items)
        md = (resp or {}).get("_metadata") or {}
        if total is None:
            total = md.get("totalItems")
            total_pages = md.get("totalPages")
            if total_pages is not None:
                info(f"{label} total reported: {total} across {total_pages} pages")
        if not items:
            break
        page += 1
        if total and len(all_items) >= total:
            break
        if page % 5 == 0:
            progress(f"page {page} done; {label} so far: {len(all_items)}/{total or '?'}")
        time.sleep(THROTTLE_SEC)
    return all_items


def pull_paginated_limit_offset(
    client: FronteggClient,
    path: str,
    limit: int,
    label: str,
    extra_params: dict | None = None,
) -> list[dict]:
    """For endpoints that paginate with `limit/offset` (no underscore):
    /plans/v1, /plans/v1/enriched, /features/v1. Caller passes a `limit`
    known to be accepted by the specific endpoint."""
    offset = 0
    all_items: list[dict] = []
    while True:
        params = {"limit": limit, "offset": offset}
        if extra_params:
            params.update(extra_params)
        resp = client.get(path, params)
        items = resp.get("items", []) if isinstance(resp, dict) else (resp or [])
        if not items:
            break
        all_items.extend(items)
        offset += limit
        progress(f"{label}: {len(all_items)} so far")
        time.sleep(THROTTLE_SEC)
        if offset > SAFETY_OFFSET_CAP:
            warn(f"safety cap hit at offset={offset} on {path}")
            break
    return all_items


def pull_entitlements(client: FronteggClient) -> list[dict]:
    """`/entitlements/v2` silently caps `limit` at 10. We page until empty."""
    offset = 0
    all_items: list[dict] = []
    while True:
        resp = client.get("/entitlements/resources/entitlements/v2",
                          {"limit": ENTITLEMENTS_LIMIT, "offset": offset})
        items = resp.get("items", []) if isinstance(resp, dict) else []
        if not items:
            break
        all_items.extend(items)
        offset += ENTITLEMENTS_LIMIT
        if offset % 500 == 0:
            progress(f"entitlements offset={offset}, {len(all_items)} pulled")
        time.sleep(THROTTLE_SEC)
        if offset > SAFETY_OFFSET_CAP:
            warn(f"safety cap hit at offset={offset} on /entitlements/v2")
            break
    return all_items


def pull_hierarchy(client: FronteggClient, tenants: list[dict]) -> list[dict]:
    """`/tenants/hierarchy/v1*` endpoints require a `frontegg-tenant-id` header.
    Only tenants with `isReseller: true` have non-trivial trees; every other
    tenant is flat. So we only call once per reseller — O(reseller count)."""
    resellers = [t for t in tenants if t.get("isReseller")]
    info(f"resellers (hierarchy roots): {len(resellers)}")
    trees: list[dict] = []
    for tid in (r["tenantId"] for r in resellers):
        tree = client.get("/tenants/resources/hierarchy/v1/tree", tenant_id=tid)
        if tree:
            trees.append(tree)
        time.sleep(THROTTLE_SEC)
    return trees


def pull_user_role_assignments(client: FronteggClient, users: list[dict]) -> list[dict]:
    """For each `(tenant, [user-ids])`, one batched call to /v3/roles?ids=…
    (Up to ROLES_BATCH_SIZE ids per call, tenant scoped via header.)"""
    by_tenant: dict[str, list[str]] = {}
    for u in users:
        tenant_ids = u.get("tenantIds") or ([u["tenantId"]] if u.get("tenantId") else [])
        for tid in tenant_ids:
            if tid:
                by_tenant.setdefault(tid, []).append(u["id"])
    total_tenants = len(by_tenant)
    info(f"tenants with users to query: {total_tenants}")
    all_assignments: list[dict] = []
    t0 = time.time()
    for i, (tid, user_ids) in enumerate(by_tenant.items(), 1):
        for chunk_start in range(0, len(user_ids), ROLES_BATCH_SIZE):
            chunk = user_ids[chunk_start:chunk_start + ROLES_BATCH_SIZE]
            try:
                resp = client.get(
                    "/identity/resources/users/v3/roles",
                    {"ids": ",".join(chunk)},
                    tenant_id=tid,
                )
            except Exception as e:
                warn(f"role pull failed for tenant {tid}: {e}")
                continue
            if isinstance(resp, list):
                all_assignments.extend(resp)
            time.sleep(THROTTLE_SEC)
        if i % 100 == 0 or i == total_tenants:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta_s = int((total_tenants - i) / rate) if rate else 0
            progress(f"tenants {i}/{total_tenants}  assignments={len(all_assignments)}  "
                     f"elapsed={int(elapsed)}s  eta≈{eta_s}s")
    return all_assignments


# --------------------------------------------------------------------------- #
# Tree walker (used for hierarchy stats in the final summary)
# --------------------------------------------------------------------------- #
def _walk_tree(node: dict) -> Iterable[dict]:
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children", []) or []:
        yield from _walk_tree(child)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    started_at = datetime.now(timezone.utc)

    env = load_config(DOTENV_PATH)
    base_url = env["FRONTEGG_BASE_URL"].rstrip("/")

    banner(
        "Frontegg Account Backup",
        f"started {started_at.isoformat()}  base={base_url}",
    )
    info(f"Output dir: {SCRIPT_DIR}")
    info(f"Log file  : {LOG_PATH.name}")
    info("Read-only — GET requests only (plus one POST /auth/vendor/ for the token).")

    NSTEPS = 8

    # ---- 1. Authenticate ---------------------------------------------------
    step(1, NSTEPS, "Authenticate with vendor endpoint")
    client = FronteggClient(base_url, env["FRONTEGG_CLIENT_ID"], env["FRONTEGG_CLIENT_SECRET"])
    client.authenticate()
    ok("Authenticated. Token valid ~24h.")

    # ---- 2. Catalogs (small) -----------------------------------------------
    step(2, NSTEPS, "Pull catalogs (roles, permissions, plans, features, feature_flags)")
    roles = client.get("/identity/resources/roles/v1")
    ok(f"Roles: {len(roles)}  (each role includes its permissions[])")
    permissions = client.get("/identity/resources/permissions/v1")
    ok(f"Permissions: {len(permissions)}  (each includes the reverse roleIds[])")

    # /plans/v1/enriched paginates with limit/offset (no underscore).
    # Includes per-plan assignment counts.
    plans = pull_paginated_limit_offset(
        client,
        "/entitlements/resources/plans/v1/enriched",
        limit=200,
        label="plans",
        extra_params={"orderBy": "createdAt", "sortType": "DESC",
                      "filter": "", "featureIds": "", "tenantIds": "",
                      "userIds": "", "planIds": ""},
    )
    ok(f"Plans (enriched): {len(plans)}")

    # /features/v1 paginates with limit/offset, max limit=100.
    features = pull_paginated_limit_offset(
        client, "/entitlements/resources/features/v1", limit=100, label="features",
    )
    ok(f"Features: {len(features)}")

    # /feature-flags/v1 is the *opposite* — it paginates with _limit/_offset (underscored).
    # Wrapped response shape: {items, _metadata, _links}.
    flags_resp = client.get("/entitlements/resources/feature-flags/v1",
                             {"_limit": 200, "_offset": 0})
    feature_flags = flags_resp.get("items", []) if isinstance(flags_resp, dict) else []
    ok(f"Feature flags: {len(feature_flags)}")

    # ---- 3. Tenants --------------------------------------------------------
    step(3, NSTEPS, "Pull tenants (200/page, page-index `_offset`)")
    tenants = pull_pages_by_pageindex(
        client, "/tenants/resources/tenants/v2",
        page_size=PAGE_SIZE_TENANTS, label="tenants",
    )
    ok(f"Tenants: {len(tenants)}")

    # ---- 4. Hierarchy ------------------------------------------------------
    step(4, NSTEPS, "Pull hierarchy trees (one per `isReseller: true` tenant)")
    hierarchy_trees = pull_hierarchy(client, tenants)
    unique_in_hierarchy = {
        n.get("tenantId")
        for tree in hierarchy_trees
        for n in _walk_tree(tree)
        if n.get("tenantId")
    }
    ok(f"Trees: {len(hierarchy_trees)}  |  unique tenants in any hierarchy: {len(unique_in_hierarchy)}")

    # ---- 5. Users ----------------------------------------------------------
    step(5, NSTEPS, "Pull users (200/page, vendor-wide, no tenant header)")
    users = pull_pages_by_pageindex(
        client, "/identity/resources/users/v3",
        page_size=PAGE_SIZE_USERS, label="users",
    )
    ok(f"Users: {len(users)}  (each user includes tenantIds[], tenants[], metadata, vendorMetadata)")

    # ---- 6. Per-tenant user-role assignments -------------------------------
    step(6, NSTEPS, "Pull user-role assignments (batched per tenant)")
    user_role_assignments = pull_user_role_assignments(client, users)
    ok(f"Role assignment records: {len(user_role_assignments)}")

    # ---- 7. Entitlements ---------------------------------------------------
    step(7, NSTEPS, "Pull entitlements (limit=10/page until empty — Frontegg silently caps)")
    entitlements = pull_entitlements(client)
    ok(f"Entitlements: {len(entitlements)}")

    # ---- 8. Assemble + write ----------------------------------------------
    step(8, NSTEPS, "Assemble + write single backup JSON")
    ended_at = datetime.now(timezone.utc)
    duration_s = int((ended_at - started_at).total_seconds())

    vendor_id = (roles[0].get("vendorId") if roles else None) or \
                (tenants[0].get("vendorId") if tenants else None)

    output = {
        "schemaVersion": "1.0",
        "exportRun": {
            "startedAt": started_at.isoformat(),
            "endedAt": ended_at.isoformat(),
            "durationSeconds": duration_s,
            "baseUrl": base_url,
            "vendorId": vendor_id,
            "apiCalls": client.calls,
            "errors": client.errors,
            "rateLimit429s": client.h429,
            "rateLimitHeadersSeen": client.rate_limit_headers_seen,
            "lastTraceId": client.last_trace_id,
        },
        "counts": {
            "tenants": len(tenants),
            "resellersTenants": sum(1 for t in tenants if t.get("isReseller")),
            "uniqueTenantsInHierarchy": len(unique_in_hierarchy),
            "users": len(users),
            "roles": len(roles),
            "permissions": len(permissions),
            "plans": len(plans),
            "features": len(features),
            "featureFlags": len(feature_flags),
            "hierarchyTrees": len(hierarchy_trees),
            "userRoleAssignments": len(user_role_assignments),
            "entitlements": len(entitlements),
        },
        "roles": roles,
        "permissions": permissions,
        "plans": plans,
        "features": features,
        "featureFlags": feature_flags,
        "tenants": tenants,
        "hierarchyTrees": hierarchy_trees,
        "users": users,
        "userRoleAssignments": user_role_assignments,
        "entitlements": entitlements,
    }

    out_name = f"frontegg_account_backup_{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path = SCRIPT_DIR / out_name
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    ok(f"Wrote {out_path.name} ({size_mb:.1f} MB)")

    banner(
        "Done",
        f"duration={duration_s}s  api_calls={client.calls}  "
        f"errors={client.errors}  429s={client.h429}  "
        f"rate_limit_headers_seen={client.rate_limit_headers_seen}",
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("Aborted by user (KeyboardInterrupt)")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        err(f"Fatal: {exc!r}")
        _log(f"Fatal: {exc!r}", "ERROR")
        sys.exit(1)
