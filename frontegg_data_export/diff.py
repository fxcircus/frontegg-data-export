"""What changed between two runs. Pure: two normalized models in, a list of
changes and a summary out.

Compared:
- users: added, removed; email, name and verified changes;
- memberships: added, removed; disabled; roles (only when both runs know
  the roles for that membership);
- accounts: added, removed, renamed, moved within the hierarchy (only when
  both runs read the tree that account is in), reseller flag;
- plan assignments: added, removed, expiry changes. An assignment is keyed by
  (plan, account, user), so one re-created with a new ID isn't a change.

Ignored as volatile: last login, created/updated timestamps, metadata.
"Users who logged in since the earlier run" is reported as a count only.

A section that wasn't exported in both runs is listed under `notCompared`
instead of showing up as everything added or removed.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass

from .model import parse_ts

CHANGES_HEADER = ["change_type", "entity", "id", "name_or_email", "account_id", "account_name", "field",
                  "before", "after"]
ENTITY_ORDER = {"account": 0, "user": 1, "membership": 2, "plan_assignment": 3}
TYPE_ORDER = {"added": 0, "removed": 1, "changed": 2}
FIRST_RUN_MESSAGE = "First run. Nothing to compare yet."


@dataclass(frozen=True)
class Change:
    change_type: str          # added | removed | changed
    entity: str               # user | membership | account | plan_assignment
    id: str
    name_or_email: str
    account_id: str = ""
    account_name: str = ""
    field: str = ""
    before: str = ""
    after: str = ""

    def row(self) -> list[str]:
        return list(astuple(self))

    def to_dict(self) -> dict:
        return dict(zip(CHANGES_HEADER, self.row()))


def _yn(v: bool) -> str:
    return "yes" if v else "no"


def _acct_name(model: dict, tid: str | None) -> str:
    if not tid:
        return ""
    return (model.get("accounts", {}).get(tid) or {}).get("name", "")


def _role_names(model: dict, ids: list[str]) -> str:
    roles = model.get("roles", {})
    return "; ".join(sorted((roles.get(r, r) for r in ids), key=str.casefold))


def _accounts_of(model: dict, user: dict) -> tuple[str, str]:
    tids = sorted(user["memberships"], key=lambda t: _acct_name(model, t).casefold())
    return "; ".join(tids), "; ".join(_acct_name(model, t) for t in tids)


def diff_models(old: dict, new: dict) -> tuple[list[Change], dict]:
    changes: list[Change] = []
    not_compared: list[str] = []
    skipped_roles = 0
    skipped_parents = 0
    has_old, has_new = old.get("has", {}), new.get("has", {})

    def both(key: str) -> bool:
        return bool(has_old.get(key) and has_new.get(key))

    # ---- accounts -----------------------------------------------------------
    renamed = moved = 0
    if both("accounts"):
        oa, na = old["accounts"], new["accounts"]
        compare_parents = both("hierarchy")
        if not compare_parents:
            not_compared.append("Account moves: the hierarchy wasn't exported in both runs.")
        old_failed = set(old.get("failedHierarchyRoots") or [])
        new_failed = set(new.get("failedHierarchyRoots") or [])
        for tid in na.keys() - oa.keys():
            changes.append(Change("added", "account", tid, na[tid]["name"], tid, na[tid]["name"]))
        for tid in oa.keys() - na.keys():
            changes.append(Change("removed", "account", tid, oa[tid]["name"], tid, oa[tid]["name"]))
        for tid in oa.keys() & na.keys():
            o, n = oa[tid], na[tid]
            if o["name"] != n["name"]:
                renamed += 1
                changes.append(Change("changed", "account", tid, n["name"], tid, n["name"], "name",
                                      o["name"], n["name"]))
            if o["isReseller"] != n["isReseller"]:
                changes.append(Change("changed", "account", tid, n["name"], tid, n["name"], "reseller",
                                      _yn(o["isReseller"]), _yn(n["isReseller"])))
            if compare_parents and o.get("parentId") != n.get("parentId"):
                unknown = (tid in old_failed or tid in new_failed
                           or (o.get("rootId") and o["rootId"] in new_failed)
                           or (n.get("rootId") and n["rootId"] in old_failed))
                if unknown:
                    skipped_parents += 1
                    continue
                moved += 1
                changes.append(Change("changed", "account", tid, n["name"], tid, n["name"], "parent",
                                      _acct_name(old, o.get("parentId")) or "(top level)",
                                      _acct_name(new, n.get("parentId")) or "(top level)"))
    else:
        not_compared.append("Accounts weren't exported in both runs.")

    # ---- users and memberships ---------------------------------------------
    logged_in = 0
    roles_comparable = both("roles")
    if both("users"):
        ou, nu = old["users"], new["users"]
        if not roles_comparable:
            not_compared.append("Roles weren't looked up in both runs.")
        # Logged in since the earlier run: last login moved forward, or (for a
        # new user) is at or after the earlier run's start.
        old_start = parse_ts(old.get("runStartedAt"))
        for uid, u in nu.items():
            last = parse_ts(u.get("lastLogin"))
            if not last:
                continue
            if uid in ou:
                prev = parse_ts(ou[uid].get("lastLogin"))
                if prev is None or last > prev:
                    logged_in += 1
            elif old_start is None or last >= old_start:
                logged_in += 1
        for uid in nu.keys() - ou.keys():
            u = nu[uid]
            ids, names = _accounts_of(new, u)
            changes.append(Change("added", "user", uid, u["email"] or u["name"], ids, names))
        for uid in ou.keys() - nu.keys():
            u = ou[uid]
            ids, names = _accounts_of(old, u)
            changes.append(Change("removed", "user", uid, u["email"] or u["name"], ids, names))
        for uid in ou.keys() & nu.keys():
            o, n = ou[uid], nu[uid]
            label = n["email"] or n["name"]
            for field in ("email", "name"):
                if o[field] != n[field]:
                    changes.append(Change("changed", "user", uid, label, field=field, before=o[field], after=n[field]))
            if o["verified"] != n["verified"]:
                changes.append(Change("changed", "user", uid, label, field="verified",
                                      before=_yn(o["verified"]), after=_yn(n["verified"])))
            om, nm = o["memberships"], n["memberships"]
            for tid in nm.keys() - om.keys():
                changes.append(Change("added", "membership", uid, label, tid, _acct_name(new, tid)))
            for tid in om.keys() - nm.keys():
                changes.append(Change("removed", "membership", uid, label, tid,
                                      _acct_name(new, tid) or _acct_name(old, tid)))
            for tid in om.keys() & nm.keys():
                a, b = om[tid], nm[tid]
                name = _acct_name(new, tid) or _acct_name(old, tid)
                if a["disabled"] != b["disabled"]:
                    changes.append(Change("changed", "membership", uid, label, tid, name, "disabled",
                                          _yn(a["disabled"]), _yn(b["disabled"])))
                if roles_comparable:
                    if a["roleIds"] is None or b["roleIds"] is None:
                        skipped_roles += 1
                    elif sorted(a["roleIds"]) != sorted(b["roleIds"]):
                        changes.append(Change("changed", "membership", uid, label, tid, name, "roles",
                                              _role_names(old, a["roleIds"]), _role_names(new, b["roleIds"])))
    else:
        not_compared.append("Users weren't exported in both runs.")

    # ---- plan assignments ---------------------------------------------------
    expiry_changed = 0
    if both("plans"):
        op, np_ = old["planAssignments"], new["planAssignments"]

        def user_email(model: dict, uid: str) -> str:
            return (model.get("users", {}).get(uid) or {}).get("email", "") or uid

        for key in np_.keys() - op.keys():
            a = np_[key]
            changes.append(Change("added", "plan_assignment", a["id"], a["planName"], a["accountId"],
                                  _acct_name(new, a["accountId"]), "user" if a["userId"] else "",
                                  "", user_email(new, a["userId"]) if a["userId"] else ""))
        for key in op.keys() - np_.keys():
            a = op[key]
            changes.append(Change("removed", "plan_assignment", a["id"], a["planName"], a["accountId"],
                                  _acct_name(new, a["accountId"]) or _acct_name(old, a["accountId"]),
                                  "user" if a["userId"] else "",
                                  user_email(old, a["userId"]) if a["userId"] else "", ""))
        for key in op.keys() & np_.keys():
            a, b = op[key], np_[key]
            if a["expiresAt"] != b["expiresAt"]:
                expiry_changed += 1
                changes.append(Change("changed", "plan_assignment", b["id"], b["planName"], b["accountId"],
                                      _acct_name(new, b["accountId"]), "expires_at",
                                      a["expiresAt"] or "never", b["expiresAt"] or "never"))
    else:
        not_compared.append("Plan assignments weren't exported in both runs.")

    if skipped_roles:
        not_compared.append(f"Role changes weren't compared for {skipped_roles} membership(s) because a role "
                            "lookup failed in one of the runs.")
    if skipped_parents:
        not_compared.append(f"Moves weren't compared for {skipped_parents} account(s) because a hierarchy tree "
                            "couldn't be read in one of the runs.")

    changes.sort(key=lambda c: (ENTITY_ORDER[c.entity], TYPE_ORDER[c.change_type], c.name_or_email.casefold(),
                                c.account_name.casefold(), c.field, c.id))

    def count(entity: str, kind: str) -> int:
        return sum(1 for c in changes if c.entity == entity and c.change_type == kind)

    # existing users with a field change or any membership change
    changed_users = {c.id for c in changes
                     if (c.entity == "user" and c.change_type == "changed") or c.entity == "membership"}

    summary = {
        "from": old.get("runStartedAt"),
        "to": new.get("runStartedAt"),
        "total": len(changes),
        "counts": {
            "usersAdded": count("user", "added"),
            "usersRemoved": count("user", "removed"),
            "usersChanged": len(changed_users),
            "membershipsAdded": count("membership", "added"),
            "membershipsRemoved": count("membership", "removed"),
            "accountsAdded": count("account", "added"),
            "accountsRemoved": count("account", "removed"),
            "accountsRenamed": renamed,
            "accountsMoved": moved,
            "planAssignmentsAdded": count("plan_assignment", "added"),
            "planAssignmentsRemoved": count("plan_assignment", "removed"),
            "planExpiryChanges": expiry_changed,
        },
        "usersLoggedInSince": logged_in,
        "notCompared": not_compared,
    }
    return changes, summary


def first_run_summary() -> dict:
    return {"from": None, "to": None, "total": 0, "counts": {}, "usersLoggedInSince": None,
            "notCompared": [], "message": FIRST_RUN_MESSAGE}
