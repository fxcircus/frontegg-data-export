"""Pull helpers — each one encodes a specific Frontegg API quirk, commented
where it's handled. Request pacing lives in the client's throttle, not here.

Tuning constants reflect live-verified Frontegg API behaviour. Most you should
NOT need to change. Each comment notes why the value is what it is so you can
adjust safely if your environment differs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable

from .client import ApiError, FronteggClient
from .model import iso_z
from .progress import Reporter
from .status import Failure

PAGE_SIZE_TENANTS = 200       # max accepted by /tenants/v2. NB: `_offset` on this endpoint is a PAGE INDEX (0..totalPages-1), not an item offset.
PAGE_SIZE_USERS = 200         # max accepted by /users/v3. Same page-index `_offset` quirk.
ENTITLEMENTS_LIMIT = 10       # /entitlements/v2 silently caps `limit` at 10. Values above this return 0 items. We paginate until empty.
FEATURES_LIMIT = 100          # /features/v1 caps `limit` at 100 and returns 400 above that.
PLANS_LIMIT = 200
ROLES_BATCH_SIZE = 100        # docs allow up to 250 user IDs per /v3/roles call, but the resulting URL (~9 KB) trips HTTP 414 "URI Too Large" on big tenants. 100 keeps the URL under ~4 KB.
SAFETY_OFFSET_CAP = 200_000   # ceiling for the page-until-empty loops. Raise this if your environment has more than 200K entitlement records.

USERS_PATH = "/identity/resources/users/v3"
TENANTS_PATH = "/tenants/resources/tenants/v2"
ROLES_PATH = "/identity/resources/roles/v1"
PERMISSIONS_PATH = "/identity/resources/permissions/v1"
USER_ROLES_PATH = "/identity/resources/users/v3/roles"
TREE_PATH = "/tenants/resources/hierarchy/v1/tree"
# Quirk: the non-enriched /plans/v1 returns 10 records at most, with no sign
# that more exist. The enriched listing is complete and adds per-plan
# assignedTenantsCount / assignedUsersCount / featuresCount.
PLANS_PATH = "/entitlements/resources/plans/v1/enriched"
FEATURES_PATH = "/entitlements/resources/features/v1"
FLAGS_PATH = "/entitlements/resources/feature-flags/v1"
ENTITLEMENTS_PATH = "/entitlements/resources/entitlements/v2"


def pull_pages_by_pageindex(client: FronteggClient, path: str, page_size: int,
                            step: str, report: Reporter, unit: str) -> list[dict]:
    """For /tenants/v2 and /users/v3 where `_offset` is a PAGE INDEX
    (0..totalPages-1) and NOT an item offset.

    Quirk: these endpoints use the underscored `_limit`/`_offset`. The plain
    `limit`/`offset` return a stale, cached small page with no error."""
    page = 0
    all_items: list[dict] = []
    total = None
    while True:
        resp = client.get(path, {"_limit": page_size, "_offset": page})
        items = (resp or {}).get("items", [])
        all_items.extend(items)
        md = (resp or {}).get("_metadata") or {}
        if total is None:
            total = md.get("totalItems")
        if not items:
            break
        page += 1
        report.progress(step, len(all_items), total, unit)
        if total is not None and len(all_items) >= total:
            break
    return all_items


def pull_paginated_limit_offset(client: FronteggClient, path: str, limit: int,
                                extra_params: dict | None = None) -> list[dict]:
    """For endpoints that paginate with `limit/offset` (no underscore, item
    offsets): /plans/v1/enriched, /features/v1. Caller passes a `limit` known
    to be accepted by the specific endpoint."""
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
        if offset > SAFETY_OFFSET_CAP:
            raise ApiError(f"Stopped reading {path} after {SAFETY_OFFSET_CAP} records (safety cap).", path=path)
    return all_items


def pull_plans(client: FronteggClient) -> list[dict]:
    return pull_paginated_limit_offset(
        client, PLANS_PATH, PLANS_LIMIT,
        extra_params={"orderBy": "createdAt", "sortType": "DESC", "filter": "", "featureIds": "",
                      "tenantIds": "", "userIds": "", "planIds": ""})


def pull_features(client: FronteggClient) -> list[dict]:
    return pull_paginated_limit_offset(client, FEATURES_PATH, FEATURES_LIMIT)


def pull_feature_flags(client: FronteggClient) -> list[dict]:
    """/feature-flags/v1 is the *opposite* of the other entitlements endpoints:
    it paginates with `_limit/_offset` (underscored, page index).
    Wrapped response shape: {items, _metadata, _links}."""
    flags: list[dict] = []
    page = 0
    while True:
        resp = client.get(FLAGS_PATH, {"_limit": 200, "_offset": page})
        items = resp.get("items", []) if isinstance(resp, dict) else []
        flags.extend(items)
        total = ((resp or {}).get("_metadata") or {}).get("totalItems")
        if not items or total is None or len(flags) >= total:
            return flags
        page += 1


def pull_entitlements(client: FronteggClient, report: Reporter) -> list[dict]:
    """`/entitlements/v2` silently caps `limit` at 10 (`limit=11` returns 0
    items, no error). We page by 10 until a page is empty, or until the
    documented `hasNext` says there's nothing more."""
    offset = 0
    all_items: list[dict] = []
    while True:
        resp = client.get(ENTITLEMENTS_PATH, {"limit": ENTITLEMENTS_LIMIT, "offset": offset})
        items = resp.get("items", []) if isinstance(resp, dict) else []
        if not items:
            break
        all_items.extend(items)
        offset += ENTITLEMENTS_LIMIT
        report.progress("entitlements", len(all_items), None, "plan assignments")
        if isinstance(resp, dict) and resp.get("hasNext") is False:
            break
        if offset > SAFETY_OFFSET_CAP:
            raise ApiError(f"Stopped reading plan assignments after {SAFETY_OFFSET_CAP} records (safety cap).",
                           path=ENTITLEMENTS_PATH)
    return all_items


def pull_hierarchy(client: FronteggClient, tenants: list[dict], failures: list[Failure],
                   report: Reporter) -> tuple[list[dict], list[str]]:
    """`/tenants/hierarchy/v1*` endpoints require a `frontegg-tenant-id` header
    even with a vendor token (without it: 403 "Tenant ID is not specified"),
    and answer relative to that tenant.

    Only tenants with `isReseller: true` have non-trivial trees; every other
    tenant is flat, and tenants carry no `parentTenantId`. So we call once
    per reseller — O(reseller count).

    A tree that can't be read (e.g. 400 for a circular hierarchy) is recorded
    as a failure and skipped; the rest of the export carries on.
    Returns (trees, root tenant IDs whose tree failed)."""
    resellers = [t for t in tenants if t.get("isReseller")]
    trees: list[dict] = []
    failed_roots: list[str] = []
    for i, tid in enumerate((r["tenantId"] for r in resellers), 1):
        try:
            tree = client.get(TREE_PATH, tenant_id=tid)
        except ApiError as e:
            failures.append(Failure.from_error("hierarchy", e, tenant_id=tid))
            failed_roots.append(tid)
            report.warn(f"Couldn't read the hierarchy under account {tid}: {e.message}",
                        step="hierarchy", tenantId=tid, traceId=e.trace_id)
            continue
        if tree:
            trees.append(tree)
        report.progress("hierarchy", i, len(resellers), "reseller accounts")
    return trees, failed_roots


def _lookup_roles(client: FronteggClient, tenant_id: str, user_ids: list[str],
                  report: Reporter) -> list[dict]:
    """One batched lookup, halving the batch if the URL is still too long
    for the server (HTTP 414) — a safety net under ROLES_BATCH_SIZE."""
    try:
        resp = client.get(USER_ROLES_PATH, {"ids": ",".join(user_ids)}, tenant_id=tenant_id)
    except ApiError as e:
        if e.status == 414 and len(user_ids) > 1:
            half = len(user_ids) // 2
            report.warn(f"URL too long for {len(user_ids)} IDs; retrying in batches of {half}", step="roles")
            return (_lookup_roles(client, tenant_id, user_ids[:half], report)
                    + _lookup_roles(client, tenant_id, user_ids[half:], report))
        raise
    return resp if isinstance(resp, list) else []


def users_by_tenant(users: list[dict]) -> dict[str, list[str]]:
    by_tenant: dict[str, list[str]] = {}
    for u in users:
        tenant_ids = u.get("tenantIds") or ([u["tenantId"]] if u.get("tenantId") else [])
        for tid in tenant_ids:
            if tid:
                by_tenant.setdefault(tid, []).append(u["id"])
    return by_tenant


def pull_user_role_assignments(client: FronteggClient, users: list[dict], failures: list[Failure],
                               report: Reporter) -> tuple[list[dict], list[str]]:
    """`/identity/resources/users/v3/roles` is a batched LOOKUP, not a listing:
    `ids=<csv>` of user IDs, tenant-scoped via the `frontegg-tenant-id` header
    (a cross-tenant header returns `[]`). Roles are NOT embedded in the bulk
    `/users/v3` response — its `tenants[]` only carries
    `{tenantId, isDisabled, temporaryExpirationDate}` — so this per-tenant pass
    is needed, and it's the most expensive step of a run.

    A tenant whose lookup fails is recorded as a failure (with its trace ID)
    and its roles are treated as unknown, not empty.
    Returns (assignments, tenant IDs whose lookup failed)."""
    by_tenant = users_by_tenant(users)
    total = len(by_tenant)
    all_assignments: list[dict] = []
    failed_tenants: list[str] = []
    for i, (tid, user_ids) in enumerate(by_tenant.items(), 1):
        tenant_rows: list[dict] = []
        try:
            for start in range(0, len(user_ids), ROLES_BATCH_SIZE):
                tenant_rows.extend(_lookup_roles(client, tid, user_ids[start:start + ROLES_BATCH_SIZE], report))
        except ApiError as e:
            failures.append(Failure.from_error("roles", e, tenant_id=tid))
            failed_tenants.append(tid)
            report.warn(f"Couldn't look up roles in account {tid}: {e.message}",
                        step="roles", tenantId=tid, traceId=e.trace_id)
        else:
            all_assignments.extend(tenant_rows)
        report.progress("roles", i, total, "accounts", force=(i == total))
    return all_assignments, failed_tenants


def walk_tree(node: dict) -> Iterable[dict]:
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children", []) or []:
        yield from walk_tree(child)


class SectionUnavailable(Exception):
    """This environment can't provide the section (plan or permissions)."""

    def __init__(self, reason: str, error: ApiError) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


def pull_login_events(client: FronteggClient, tenant_ids: list[str], start: datetime, end: datetime,
                      failures: list[Failure], report: Reporter,
                      classify: Callable[[dict], str | None]) -> tuple[list[dict], dict]:
    """Login events from the audit log, one account at a time.

    Quirks (see loginevents.py for what the docs do and don't say):
    - the audits API is tenant-scoped: `frontegg-tenant-id` header per call;
    - it pages with `count` (max 200) and an ITEM offset, unlike the
      page-index `_offset` of /users/v3;
    - only login rows are kept (matched on the action text), with the
      classification in `_result`.
    If the very first account is refused with 401/402/403/404, the section is
    unavailable for this environment: raise instead of failing every account.
    """
    from .loginevents import AUDITS_PAGE_SIZE, AUDITS_PATH, UNAVAILABLE_REASONS

    window = {"created_from": iso_z(start), "created_to": iso_z(end), "sortBy": "createdAt", "sortDirection": "asc"}
    events: list[dict] = []
    stats = {"accountsQueried": len(tenant_ids), "rowsScanned": 0, "success": 0, "failure": 0, "failedAccounts": 0}
    answered = 0
    for i, tid in enumerate(tenant_ids, 1):
        offset = 0
        try:
            while True:
                resp = client.get(AUDITS_PATH, {"count": AUDITS_PAGE_SIZE, "offset": offset, **window}, tenant_id=tid)
                if isinstance(resp, dict):
                    rows = resp.get("data") or resp.get("items") or []
                    total = resp.get("total")
                else:
                    rows, total = (resp or []), None
                for row in rows:
                    kind = classify(row)
                    if kind:
                        events.append({**row, "tenantId": row.get("tenantId") or tid, "_result": kind})
                        stats[kind] += 1
                stats["rowsScanned"] += len(rows)
                offset += len(rows)
                if len(rows) < AUDITS_PAGE_SIZE or (total is not None and offset >= int(total)):
                    break
                if offset > SAFETY_OFFSET_CAP:
                    raise ApiError(f"Stopped reading the audit log after {SAFETY_OFFSET_CAP} rows (safety cap).",
                                   path=AUDITS_PATH)
            answered += 1
        except ApiError as e:
            if answered == 0 and e.status in UNAVAILABLE_REASONS:
                raise SectionUnavailable(UNAVAILABLE_REASONS[e.status], e) from None
            failures.append(Failure.from_error("login_events", e, tenant_id=tid))
            stats["failedAccounts"] += 1
            report.warn(f"Couldn't read login events for account {tid}: {e.message}",
                        step="login_events", tenantId=tid, traceId=e.trace_id)
        report.progress("login_events", i, len(tenant_ids), "accounts", force=(i == len(tenant_ids)))
    return events, stats
