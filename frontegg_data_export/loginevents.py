"""Login events from Frontegg's audit log: date range, classification and
CSV rows. Pure; the API calls are in fetch.pull_login_events.

What Frontegg documents (developers.frontegg.com, audits API):
- `GET /audits/resources/audits/v2` returns audit logs "for a specific
  account (tenant)": one or more calls per account, with the
  `frontegg-tenant-id` header.
- `count` (1-200) and an item-based `offset`; `created_from` / `created_to`
  date-time filters; `sortBy` / `sortDirection`.
- The response schema isn't documented; Frontegg's SDK shows
  `{data: [...], total}` with fields such as tenantId, user, action,
  severity and ip.
- The action names used for successful and failed logins are NOT documented.
  So rows are classified by matching the action text (configurable), and the
  raw action is kept in the CSV so the classification can be checked.
  Observed in a real environment's audit log: successful logins are
  "User logged in" (and "Impersonated by <email> - User logged in"); rows
  carry the user's ID as `actorId`. No failed-login rows were seen over 30
  days, so the failure wording (or whether failures are audited at all) is
  still unconfirmed.
- Audit logs are listed as an Enterprise feature. When the API refuses
  (401/402/403/404) before any account has answered, the section is marked
  unavailable and the rest of the export carries on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .model import iso_z, parse_ts

AUDITS_PATH = "/audits/resources/audits/v2"
AUDITS_PAGE_SIZE = 200                 # documented maximum for `count`
DEFAULT_MAX_DAYS = 30

# Matched case-insensitively against the action (and description). A row is a
# login event if it matches a LOGIN term; it's a failure if it also matches a
# FAILURE term. Override with settings.json keys loginEventTerms and
# loginFailureTerms.
DEFAULT_LOGIN_TERMS = ("login", "log in", "log-in", "logged in", "sign in", "signin", "sign-in", "signed in",
                       "authenticated", "authentication")
DEFAULT_FAILURE_TERMS = ("fail", "invalid", "denied", "locked", "reject", "incorrect", "unsuccessful", "blocked")

LOGIN_EVENTS_HEADER = ["timestamp", "result", "action", "user_email", "user_id", "account_id", "account_name", "ip",
                       "user_agent", "severity", "description"]

UNAVAILABLE_REASONS = {
    401: "Frontegg didn't let this API key read the audit log. Audit logs may not be included in this "
         "environment's Frontegg plan (they're listed as an Enterprise feature).",
    402: "Audit logs aren't included in this environment's Frontegg plan.",
    403: "Frontegg didn't let this API key read the audit log. Audit logs may not be included in this "
         "environment's Frontegg plan (they're listed as an Enterprise feature).",
    404: "This environment doesn't have the audit-log API. Audit logs may not be included in its Frontegg plan.",
}


def date_range(now: datetime, last_success_started: datetime | None, since: datetime | None,
               max_days: int) -> tuple[datetime, datetime, bool]:
    """(from, to, capped). Default: since the previous succeeded run; never
    further back than `max_days`. An explicit `since` is capped the same way."""
    max_days = max(1, int(max_days))
    floor = now - timedelta(days=max_days)
    start = since or last_success_started or floor
    capped = start < floor
    return (max(start, floor), now, capped)


def classify(row: dict, login_terms: Iterable[str] = DEFAULT_LOGIN_TERMS,
             failure_terms: Iterable[str] = DEFAULT_FAILURE_TERMS) -> str | None:
    """"success", "failure", or None when the row isn't a login event."""
    text = f"{row.get('action') or ''} {row.get('description') or ''}".casefold()
    if not any(t.casefold() in text for t in login_terms):
        return None
    return "failure" if any(t.casefold() in text for t in failure_terms) else "success"


def _first(row: dict, *keys: str) -> Any:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def event_rows(events: list[dict], model: dict) -> Iterable[list[Any]]:
    """CSV rows, oldest first. Fields are filled from whichever names the API
    used, and IDs/emails are resolved through the exported users and accounts."""
    by_email = {u["email"].casefold(): uid for uid, u in model.get("users", {}).items() if u.get("email")}
    rows = []
    for e in events:
        user = e.get("user")
        email = _first(e, "email", "userEmail") or (user if isinstance(user, str) and "@" in user else None) or ""
        uid = (_first(e, "userId", "actorId", "frontegg_user_id")
               or (by_email.get(email.casefold()) if email else None) or "")
        if not email and uid:
            email = (model.get("users", {}).get(uid) or {}).get("email", "")
        tid = _first(e, "tenantId", "tenant_id") or ""
        when = parse_ts(_first(e, "createdAt", "time", "timestamp", "created_at"))
        rows.append([iso_z(when), e.get("_result", ""), e.get("action") or "", email, uid, tid,
                     (model.get("accounts", {}).get(tid) or {}).get("name", ""),
                     _first(e, "ip", "ipAddress") or "", _first(e, "userAgent", "user_agent") or "",
                     e.get("severity") or "", _first(e, "description", "message") or ""])
    rows.sort(key=lambda r: (r[0], r[5], r[3]))
    return rows
