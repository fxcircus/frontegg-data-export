"""The full-fidelity JSON snapshot, schemaVersion 2.0.

Changes from 1.0 (the frontegg-account-backup format):
- `exportRun` is now `run`, and adds `id`, `status`, `preset` and `trigger`;
- new `sections` (per-section status) and `failures` (what failed, with
  trace IDs);
- the raw API arrays moved under `data`;
- each user gains `tenantRoles` (as in the users-only script) when roles
  were exported.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .fetch import walk_tree

SCHEMA_VERSION = "2.0"


def enrich_users_with_roles(users: list[dict], assignments: list[dict], roles: list[dict],
                            failed_tenants: set[str]) -> list[dict]:
    """Copies of the users, each with `tenantRoles`:

        [{"tenantId": "...", "roles": [{"id", "key", "name"}], "lookupFailed": false}]

    A tenant whose role lookup failed gets `roles: null, lookupFailed: true`
    rather than an empty list, so "unknown" is never mistaken for "none"."""
    role_by_id = {r["id"]: {"id": r["id"], "key": r.get("key"), "name": r.get("name")}
                  for r in roles if r.get("id")}
    by_pair: dict[tuple[str, str], list[str]] = {}
    for a in assignments:
        if a.get("userId") and a.get("tenantId"):
            by_pair.setdefault((a["userId"], a["tenantId"]), []).extend(a.get("roleIds") or [])
    out = []
    for u in users:
        tenant_ids = u.get("tenantIds") or ([u["tenantId"]] if u.get("tenantId") else [])
        tenant_roles = []
        for tid in tenant_ids:
            if tid in failed_tenants:
                tenant_roles.append({"tenantId": tid, "roles": None, "lookupFailed": True})
                continue
            ids = by_pair.get((u.get("id"), tid), [])
            tenant_roles.append({"tenantId": tid, "lookupFailed": False, "roles": [
                role_by_id.get(rid, {"id": rid, "key": None, "name": None}) for rid in ids]})
        out.append({**u, "tenantRoles": tenant_roles})
    return out


def counts_for(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    if "users" in data:
        counts["users"] = len(data["users"])
    if "tenants" in data:
        counts["accounts"] = len(data["tenants"])
        counts["resellerAccounts"] = sum(1 for t in data["tenants"] if t.get("isReseller"))
    if "hierarchyTrees" in data:
        counts["hierarchyTrees"] = len(data["hierarchyTrees"])
        counts["accountsInHierarchy"] = len({n.get("tenantId") for t in data["hierarchyTrees"]
                                             for n in walk_tree(t) if n.get("tenantId")})
    for key, name in (("roles", "roles"), ("userRoleAssignments", "userRoleAssignments"),
                      ("permissions", "permissions"), ("plans", "plans"), ("features", "features"),
                      ("featureFlags", "featureFlags"), ("entitlements", "planAssignments"),
                      ("loginEvents", "loginEvents")):
        if key in data:
            counts[name] = len(data[key])
    return counts


def build_snapshot(run: dict, sections: dict[str, dict], failures: list[dict], data: dict,
                   failed_role_tenants: list[str]) -> dict[str, Any]:
    out_data = dict(data)
    if "users" in data and "userRoleAssignments" in data:
        out_data["users"] = enrich_users_with_roles(data["users"], data["userRoleAssignments"],
                                                    data.get("roles") or [], set(failed_role_tenants))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "app": {"name": "Frontegg Data Export", "version": __version__},
        "run": run,
        "sections": sections,
        "failures": failures,
        "counts": counts_for(data),
        "data": out_data,
    }
