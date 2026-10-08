"""Orchestrates one export run."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .client import FronteggClient
from .config import APP_DIR, DOTENV_PATH, LOG_PATH, load_config, parse_rate
from .fetch import (
    PAGE_SIZE_TENANTS,
    PAGE_SIZE_USERS,
    _walk_tree,
    pull_entitlements,
    pull_hierarchy,
    pull_pages_by_pageindex,
    pull_paginated_limit_offset,
    pull_user_role_assignments,
)
from .progress import banner, info, ok, step


def main(rate: str | float | None = None) -> int:
    started_at = datetime.now(timezone.utc)

    env = load_config(DOTENV_PATH)
    base_url = env["FRONTEGG_BASE_URL"].rstrip("/")

    banner(
        "Frontegg Account Backup",
        f"started {started_at.isoformat()}  base={base_url}",
    )
    info(f"Output dir: {APP_DIR}")
    info(f"Log file  : {LOG_PATH.name}")
    info("Read-only — GET requests only (plus one POST /auth/vendor/ for the token).")

    NSTEPS = 8

    # ---- 1. Authenticate ---------------------------------------------------
    step(1, NSTEPS, "Authenticate with vendor endpoint")
    client = FronteggClient(base_url, env["FRONTEGG_CLIENT_ID"], env["FRONTEGG_CLIENT_SECRET"],
                            rate=parse_rate(rate))
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
    out_path = APP_DIR / out_name
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
