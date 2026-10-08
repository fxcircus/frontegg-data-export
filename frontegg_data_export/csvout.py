"""Spreadsheet-friendly CSVs built from the normalized model.

Every file is:
- UTF-8 with a byte-order mark, so Excel shows accented names correctly;
- CRLF line endings, standard quoting;
- protected against formula injection: a cell that starts with = + - @, a
  tab or a carriage return gets a leading single quote;
- multi-value cells joined with "; ", dates in ISO 8601 UTC, yes/no booleans,
  IDs resolved to names, rows in a stable order.

Columns are the same whatever was exported. A section that wasn't exported
leaves its cells blank; a role lookup that failed says "(lookup failed)".

The row builders are pure; `write_csv` is the only function that writes.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from .model import index_assignments, plans_for_membership
from .store import atomic_open

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
JOIN = "; "
LOOKUP_FAILED = "(lookup failed)"

USERS_HEADER = ["user_id", "email", "name", "account_id", "account_name", "roles", "plans", "verified",
                "disabled_in_account", "created_at", "last_login"]
ACCOUNTS_HEADER = ["account_id", "name", "parent_account_id", "parent_account_name", "is_reseller", "created_at",
                   "user_count", "plans"]
PLAN_ASSIGNMENTS_HEADER = ["plan_name", "plan_id", "account_id", "account_name", "user_id", "user_email",
                           "created_at", "expires_at", "expired", "level", "assignment_id"]
USERS_WITHOUT_PLAN_HEADER = ["user_id", "email", "name", "account_id", "account_name", "roles", "expired_plans",
                             "created_at", "last_login"]


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        value = JOIN.join(str(v) for v in value if v not in (None, ""))
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        text = "'" + text
    return text


def write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> int:
    """Write atomically; returns the number of data rows."""
    n = 0
    with atomic_open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(header)
        for row in rows:
            w.writerow([cell(v) for v in row])
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Row builders
# --------------------------------------------------------------------------- #
def _lower(s: str) -> str:
    return (s or "").casefold()


def account_name(model: dict, account_id: str) -> str:
    return (model["accounts"].get(account_id) or {}).get("name", "")


def role_cell(model: dict, membership: dict) -> str | list[str]:
    if not model["has"]["roles"]:
        return ""
    if membership["roleIds"] is None:
        return LOOKUP_FAILED
    roles = model["roles"]
    return sorted((roles.get(r, r) for r in membership["roleIds"]), key=_lower)


def _sorted_users(model: dict) -> list[tuple[str, dict]]:
    return sorted(model["users"].items(), key=lambda kv: (_lower(kv[1]["email"]), kv[0]))


def _sorted_memberships(model: dict, user: dict) -> list[tuple[str, dict]]:
    return sorted(user["memberships"].items(), key=lambda kv: (_lower(account_name(model, kv[0])), kv[0]))


def users_rows(model: dict) -> Iterable[list[Any]]:
    idx = index_assignments(model) if model["has"]["plans"] else None
    for uid, u in _sorted_users(model):
        if not u["memberships"]:
            yield [uid, u["email"], u["name"], "", "", "", "", u["verified"], "", u["createdAt"], u["lastLogin"]]
            continue
        for tid, m in _sorted_memberships(model, u):
            plans = "" if idx is None else sorted({a["planName"] for a in plans_for_membership(idx, tid, uid)},
                                                    key=_lower)
            yield [uid, u["email"], u["name"], tid, account_name(model, tid), role_cell(model, m), plans,
                   u["verified"], m["disabled"], u["createdAt"], u["lastLogin"]]


def accounts_rows(model: dict) -> Iterable[list[Any]]:
    user_count: dict[str, int] = {}
    for u in model["users"].values():
        for tid in u["memberships"]:
            user_count[tid] = user_count.get(tid, 0) + 1
    idx = index_assignments(model) if model["has"]["plans"] else None
    hierarchy = model["has"]["hierarchy"]
    for tid, a in sorted(model["accounts"].items(), key=lambda kv: (_lower(kv[1]["name"]), kv[0])):
        parent = a["parentId"] if hierarchy else None
        plans = "" if idx is None else sorted({p["planName"] for p in plans_for_membership(idx, tid, None)},
                                                key=_lower)
        yield [tid, a["name"], parent or "", account_name(model, parent) if parent else "", a["isReseller"],
               a["createdAt"], user_count.get(tid, 0) if model["has"]["users"] else "", plans]


def plan_assignment_rows(model: dict) -> Iterable[list[Any]]:
    users = model["users"]
    rows = sorted(model["planAssignments"].values(),
                  key=lambda a: (_lower(a["planName"]), _lower(account_name(model, a["accountId"])),
                                 _lower((users.get(a["userId"]) or {}).get("email", "")), a["id"]))
    for a in rows:
        email = (users.get(a["userId"]) or {}).get("email", "") if a["userId"] else ""
        yield [a["planName"], a["planId"], a["accountId"], account_name(model, a["accountId"]), a["userId"], email,
               a["createdAt"], a["expiresAt"], a["expired"], "user" if a["userId"] else "account", a["id"]]


def users_without_plan_rows(model: dict) -> Iterable[list[Any]]:
    """A membership (user U in account A) has no plan when no assignment E has
    E.account == A, E.user empty (account-level) or == U (user-level), and
    E.expiry empty or after the run started. No inheritance through the
    hierarchy. Plans granted only by targeting rules aren't visible here."""
    idx = index_assignments(model)
    for uid, u in _sorted_users(model):
        for tid, m in _sorted_memberships(model, u):
            if plans_for_membership(idx, tid, uid):
                continue
            expired = sorted({a["planName"] for a in plans_for_membership(idx, tid, uid, include_expired=True)},
                             key=_lower)
            yield [uid, u["email"], u["name"], tid, account_name(model, tid), role_cell(model, m), expired,
                   u["createdAt"], u["lastLogin"]]


def write_all(run_dir: Path, model: dict, sections: Iterable[str],
              login_events: list[dict] | None = None) -> dict[str, int]:
    """Write the CSVs that apply to the exported sections. Returns {file: rows}."""
    from .loginevents import LOGIN_EVENTS_HEADER, event_rows

    sections = set(sections)
    written: dict[str, int] = {}
    if "users" in sections:
        written["users.csv"] = write_csv(run_dir / "users.csv", USERS_HEADER, users_rows(model))
    if "accounts" in sections or "hierarchy" in sections:
        written["accounts.csv"] = write_csv(run_dir / "accounts.csv", ACCOUNTS_HEADER, accounts_rows(model))
    if "plans" in sections:
        written["plan_assignments.csv"] = write_csv(run_dir / "plan_assignments.csv", PLAN_ASSIGNMENTS_HEADER,
                                                    plan_assignment_rows(model))
        if model["has"]["users"]:
            written["users_without_plan.csv"] = write_csv(run_dir / "users_without_plan.csv",
                                                          USERS_WITHOUT_PLAN_HEADER, users_without_plan_rows(model))
    if "login_events" in sections and login_events is not None:
        written["login_events.csv"] = write_csv(run_dir / "login_events.csv", LOGIN_EVENTS_HEADER,
                                                event_rows(login_events, model))
    return written
