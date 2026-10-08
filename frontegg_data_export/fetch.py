"""Pull helpers — each one encodes a specific Frontegg pagination quirk.
Request pacing lives in the client's throttle, not here.

Tuning constants reflect live-verified Frontegg API behaviour. Most you should
NOT need to change. Each comment notes why the value is what it is so you can
adjust safely if your environment differs.
"""

from __future__ import annotations

import time
from typing import Iterable

from .client import ApiError, FronteggClient
from .progress import info, progress, warn
from .status import Failure

PAGE_SIZE_TENANTS = 200       # max accepted by /tenants/v2. NB: `_offset` on this endpoint is a PAGE INDEX (0..totalPages-1), not an item offset.
PAGE_SIZE_USERS = 200         # max accepted by /users/v3. Same page-index `_offset` quirk.
ENTITLEMENTS_LIMIT = 10       # /entitlements/v2 silently caps `limit` at 10. Values above this return 0 items. We paginate until empty.
ROLES_BATCH_SIZE = 100        # docs allow up to 250 user IDs per /v3/roles call, but the resulting URL (~9 KB) trips HTTP 414 "URI Too Large" on big tenants. 100 keeps the URL under ~4 KB.
SAFETY_OFFSET_CAP = 200_000   # ceiling for the entitlements page-until-empty loop. Raise this if your environment has more than 200K entitlement records.


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
        if offset > SAFETY_OFFSET_CAP:
            warn(f"safety cap hit at offset={offset} on /entitlements/v2")
            break
    return all_items


def pull_hierarchy(client: FronteggClient, tenants: list[dict],
                   failures: list[Failure]) -> tuple[list[dict], list[str]]:
    """`/tenants/hierarchy/v1*` endpoints require a `frontegg-tenant-id` header.
    Only tenants with `isReseller: true` have non-trivial trees; every other
    tenant is flat. So we only call once per reseller — O(reseller count).

    A tree that can't be read (e.g. 400 for a circular hierarchy) is recorded
    as a failure and skipped; the rest of the export carries on.
    Returns (trees, root tenant IDs whose tree failed)."""
    resellers = [t for t in tenants if t.get("isReseller")]
    info(f"resellers (hierarchy roots): {len(resellers)}")
    trees: list[dict] = []
    failed_roots: list[str] = []
    for tid in (r["tenantId"] for r in resellers):
        try:
            tree = client.get("/tenants/resources/hierarchy/v1/tree", tenant_id=tid)
        except ApiError as e:
            failures.append(Failure.from_error("hierarchy", e, tenant_id=tid))
            failed_roots.append(tid)
            warn(f"hierarchy tree failed for account {tid}: {e.message}")
            continue
        if tree:
            trees.append(tree)
    return trees, failed_roots


def _lookup_roles(client: FronteggClient, tenant_id: str, user_ids: list[str]) -> list[dict]:
    """One batched lookup, halving the batch if the URL is still too long
    for the server (HTTP 414) — a safety net under ROLES_BATCH_SIZE."""
    try:
        resp = client.get("/identity/resources/users/v3/roles", {"ids": ",".join(user_ids)},
                          tenant_id=tenant_id)
    except ApiError as e:
        if e.status == 414 and len(user_ids) > 1:
            half = len(user_ids) // 2
            warn(f"URL too long for {len(user_ids)} IDs; retrying in batches of {half}")
            return (_lookup_roles(client, tenant_id, user_ids[:half])
                    + _lookup_roles(client, tenant_id, user_ids[half:]))
        raise
    return resp if isinstance(resp, list) else []


def pull_user_role_assignments(client: FronteggClient, users: list[dict],
                               failures: list[Failure]) -> tuple[list[dict], list[str]]:
    """For each `(tenant, [user-ids])`, one batched call to /v3/roles?ids=…
    (Up to ROLES_BATCH_SIZE ids per call, tenant scoped via header.)

    A tenant whose lookup fails is recorded as a failure (with its trace ID)
    and its roles are treated as unknown, not empty.
    Returns (assignments, tenant IDs whose lookup failed)."""
    by_tenant: dict[str, list[str]] = {}
    for u in users:
        tenant_ids = u.get("tenantIds") or ([u["tenantId"]] if u.get("tenantId") else [])
        for tid in tenant_ids:
            if tid:
                by_tenant.setdefault(tid, []).append(u["id"])
    total_tenants = len(by_tenant)
    info(f"tenants with users to query: {total_tenants}")
    all_assignments: list[dict] = []
    failed_tenants: list[str] = []
    t0 = time.time()
    for i, (tid, user_ids) in enumerate(by_tenant.items(), 1):
        tenant_rows: list[dict] = []
        try:
            for chunk_start in range(0, len(user_ids), ROLES_BATCH_SIZE):
                chunk = user_ids[chunk_start:chunk_start + ROLES_BATCH_SIZE]
                tenant_rows.extend(_lookup_roles(client, tid, chunk))
        except ApiError as e:
            failures.append(Failure.from_error("roles", e, tenant_id=tid))
            failed_tenants.append(tid)
            warn(f"role lookup failed for account {tid}: {e.message}")
        else:
            all_assignments.extend(tenant_rows)
        if i % 100 == 0 or i == total_tenants:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta_s = int((total_tenants - i) / rate) if rate else 0
            progress(f"tenants {i}/{total_tenants}  assignments={len(all_assignments)}  "
                     f"elapsed={int(elapsed)}s  eta≈{eta_s}s")
    return all_assignments, failed_tenants


# --------------------------------------------------------------------------- #
# Tree walker (used for hierarchy stats in the final summary)
# --------------------------------------------------------------------------- #
def _walk_tree(node: dict) -> Iterable[dict]:
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children", []) or []:
        yield from _walk_tree(child)
