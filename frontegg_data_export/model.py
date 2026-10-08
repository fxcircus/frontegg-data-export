"""Raw API data -> a compact, normalized model of the environment.

The model is what the CSVs, the diff and "Find a user" read. It is written
per run as normalized.json, so comparing two runs never has to load two
full snapshots.

It keeps "unknown" distinct from "empty": a membership whose role lookup
failed has `roleIds: None`, and accounts under a hierarchy tree that couldn't
be read are listed, so the diff can skip them instead of reporting changes
that didn't happen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

MODEL_VERSION = 1


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def parse_ts(value: Any) -> datetime | None:
    """Frontegg returns several timestamp shapes: "...Z" with milliseconds,
    "+00:00" offsets, and (in the entitlements docs) no timezone at all
    ("2022-01-01T12:00:00"), which we treat as UTC. Epoch milliseconds are
    accepted too."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    # Python 3.10's fromisoformat only takes 0, 3 or 6 fractional digits.
    if "." in text:
        head, _, rest = text.partition(".")
        frac = ""
        while rest and rest[0].isdigit():
            frac, rest = frac + rest[0], rest[1:]
        text = f"{head}.{(frac + '000000')[:6]}{rest}" if frac else head + rest
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(value: Any) -> str:
    """ISO 8601 in UTC with a Z, to the second; "" when absent or unparseable."""
    dt = value if isinstance(value, datetime) else parse_ts(value)
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Hierarchy
# --------------------------------------------------------------------------- #
def _walk(node: Any, parent: str | None, root: str, out: dict[str, tuple[str | None, str]]) -> None:
    if not isinstance(node, dict):
        return
    tid = node.get("tenantId")
    if not tid or tid in out:          # guard against loops in the data
        return
    out[tid] = (parent, root)
    for child in node.get("children") or []:
        _walk(child, tid, root, out)


def parents_from_trees(trees: Iterable[dict]) -> dict[str, tuple[str | None, str]]:
    """tenantId -> (parentId, rootId) for every account inside a tree."""
    out: dict[str, tuple[str | None, str]] = {}
    for tree in trees:
        if isinstance(tree, dict) and tree.get("tenantId"):
            _walk(tree, None, tree["tenantId"], out)
    return out


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def _membership_ids(user: dict) -> list[str]:
    ids = [m.get("tenantId") for m in user.get("tenants") or [] if m.get("tenantId")]
    for tid in user.get("tenantIds") or ([user["tenantId"]] if user.get("tenantId") else []):
        if tid and tid not in ids:
            ids.append(tid)
    return ids


def _plan_uses_rules(plan: dict) -> bool:
    """Plans can grant access by targeting rules or a default treatment, with
    no assignment record. Such access is invisible to the assignment list."""
    if plan.get("rules"):
        return True
    return str(plan.get("defaultTreatment", "false")).lower() == "true"


def build_model(data: dict, *, sections: Iterable[str], run_started_at: datetime,
                failed_role_tenants: Iterable[str] = (), failed_roots: Iterable[str] = ()) -> dict:
    sections = list(sections)
    failed_role_tenants = sorted(set(failed_role_tenants))
    failed_roots = sorted(set(failed_roots))
    has_accounts = "tenants" in data
    has_hierarchy = "hierarchyTrees" in data
    has_users = "users" in data
    has_roles = "userRoleAssignments" in data
    has_plans = "entitlements" in data

    # ---- roles --------------------------------------------------------------
    roles = {r["id"]: (r.get("name") or r.get("key") or r["id"]) for r in data.get("roles") or [] if r.get("id")}

    # ---- accounts -----------------------------------------------------------
    tree_index = parents_from_trees(data.get("hierarchyTrees") or []) if has_hierarchy else {}
    accounts: dict[str, dict] = {}
    for t in data.get("tenants") or []:
        tid = t.get("tenantId")
        if not tid:
            continue
        parent, root = tree_index.get(tid, (None, None))
        accounts[tid] = {
            "name": t.get("name") or "",
            "isReseller": bool(t.get("isReseller")),
            "createdAt": iso_z(t.get("createdAt")),
            "parentId": parent,
            "rootId": root,
        }

    # ---- users and memberships ---------------------------------------------
    role_ids: dict[tuple[str, str], list[str]] = {}
    for a in data.get("userRoleAssignments") or []:
        uid, tid = a.get("userId"), a.get("tenantId")
        if uid and tid:
            role_ids.setdefault((uid, tid), []).extend(a.get("roleIds") or [])
    failed_set = set(failed_role_tenants)
    users: dict[str, dict] = {}
    for u in data.get("users") or []:
        uid = u.get("id")
        if not uid:
            continue
        disabled = {m.get("tenantId"): bool(m.get("isDisabled")) for m in u.get("tenants") or []}
        memberships = {}
        for tid in _membership_ids(u):
            if not has_roles or tid in failed_set:
                rids = None                                  # unknown, not empty
            else:
                rids = sorted(set(role_ids.get((uid, tid), [])))
            memberships[tid] = {"disabled": disabled.get(tid, False), "roleIds": rids}
        users[uid] = {
            "email": u.get("email") or "",
            "name": u.get("name") or "",
            "verified": bool(u.get("verified")),
            "createdAt": iso_z(u.get("createdAt")),
            "lastLogin": iso_z(u.get("lastLogin")),
            "memberships": memberships,
        }

    # ---- plans and assignments ---------------------------------------------
    plans = {p["id"]: {"name": p.get("name") or p["id"], "usesRules": _plan_uses_rules(p)}
             for p in data.get("plans") or [] if p.get("id")}
    assignments: dict[str, dict] = {}
    for e in data.get("entitlements") or []:
        pid, tid = e.get("planId"), e.get("tenantId")
        if not pid or not tid:
            continue
        uid = e.get("userId") or ""
        expires = parse_ts(e.get("expirationDate"))
        key = f"{pid}|{tid}|{uid}"
        row = {
            "id": e.get("id") or "",
            "planId": pid,
            "planName": plans.get(pid, {}).get("name") or (e.get("plan") or {}).get("name") or pid,
            "accountId": tid,
            "userId": uid,
            "createdAt": iso_z(e.get("createdAt")),
            "expiresAt": iso_z(expires),
            "expired": bool(expires and expires <= run_started_at),
        }
        # Two assignments of the same plan to the same target: the longest
        # expiry wins (documented behavior), "never" beats any date.
        prev = assignments.get(key)
        if prev is None or _later_expiry(row, prev):
            assignments[key] = row

    return {
        "modelVersion": MODEL_VERSION,
        "runStartedAt": iso_z(run_started_at),
        "sections": sections,
        "has": {"accounts": has_accounts, "hierarchy": has_hierarchy, "users": has_users,
                "roles": has_roles, "plans": has_plans},
        "roles": roles,
        "accounts": accounts,
        "users": users,
        "plans": plans,
        "planAssignments": assignments,
        "failedRoleTenants": failed_role_tenants,
        "failedHierarchyRoots": failed_roots,
    }


def _later_expiry(a: dict, b: dict) -> bool:
    if not a["expiresAt"]:
        return bool(b["expiresAt"])
    return bool(b["expiresAt"]) and a["expiresAt"] > b["expiresAt"]


# --------------------------------------------------------------------------- #
# Lookups used by the CSV writers
# --------------------------------------------------------------------------- #
def index_assignments(model: dict) -> dict[str, list[dict]]:
    """accountId -> assignments, for O(1) lookups when writing many rows."""
    idx: dict[str, list[dict]] = {}
    for a in model["planAssignments"].values():
        idx.setdefault(a["accountId"], []).append(a)
    return idx


def plans_for_membership(idx: dict[str, list[dict]], account_id: str, user_id: str | None,
                         include_expired: bool = False) -> list[dict]:
    """Plan assignments that apply to a user in an account (or, with
    user_id=None, to the account itself): account-level assignments plus that
    user's user-level assignments in that account. Expired ones are skipped
    unless asked for. Plans are NOT inherited through the account hierarchy;
    Frontegg doesn't document any such inheritance."""
    rows = []
    for a in idx.get(account_id, ()):
        if a["expired"] and not include_expired:
            continue
        if a["userId"] == "" or (user_id is not None and a["userId"] == user_id):
            rows.append(a)
    return rows
