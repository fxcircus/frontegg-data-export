"""What to export: sections, presets, the steps they need, and the estimate.

Sections are what a person chooses. Steps are the API work needed to produce
them; one step can serve several sections (the account list, for instance,
is read whenever account names are needed).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

SECTIONS = ("users", "roles", "accounts", "hierarchy", "plans", "catalog", "login_events")

SECTION_LABELS = {
    "users": "Users",
    "roles": "Roles per account",
    "accounts": "Accounts",
    "hierarchy": "Account hierarchy",
    "plans": "Plans and plan assignments",
    "catalog": "Permissions, features and feature flags",
    "login_events": "Login events",
}

PRESETS = {
    # The users-only script's behavior: users and their roles per account.
    "users": {"label": "Users only", "sections": ("users", "roles")},
    "standard": {"label": "Users, accounts and plans",
                 "sections": ("users", "roles", "accounts", "hierarchy", "plans")},
    # Everything the account-backup script exported.
    "full": {"label": "Full backup",
             "sections": ("users", "roles", "accounts", "hierarchy", "plans", "catalog")},
}
DEFAULT_PRESET = "standard"

# Steps in the order they run. (key, label)
STEPS = (
    ("auth", "Get an access token"),
    ("role_catalog", "Read the role catalog"),
    ("catalog", "Read permissions, features and feature flags"),
    ("plan_catalog", "Read plans"),
    ("accounts", "Read accounts"),
    ("hierarchy", "Read the account hierarchy"),
    ("users", "Read users"),
    ("roles", "Look up each user's roles per account"),
    ("entitlements", "Read plan assignments"),
    ("login_events", "Read login events"),
    ("write", "Write files"),
)
STEP_LABELS = dict(STEPS)


@dataclass
class Selection:
    preset: str
    sections: tuple[str, ...]
    steps: tuple[str, ...] = field(default=())

    @property
    def label(self) -> str:
        return PRESETS[self.preset]["label"] if self.preset in PRESETS else "Custom"

    def has(self, section: str) -> bool:
        return section in self.sections


def resolve(preset: str | None = None, sections: list[str] | tuple[str, ...] | None = None,
            roles: bool | None = None, login_events: bool | None = None) -> Selection:
    """Turn a preset (or an explicit section list) plus toggles into the
    sections to export and the steps needed to produce them."""
    if sections:
        unknown = [s for s in sections if s not in SECTIONS]
        if unknown:
            raise ValueError(f"Unknown section(s): {', '.join(unknown)}. Choose from: {', '.join(SECTIONS)}")
        chosen = set(sections)
        name = "custom"
    else:
        name = preset or DEFAULT_PRESET
        if name not in PRESETS:
            raise ValueError(f"Unknown preset {name!r}. Choose from: {', '.join(PRESETS)}")
        chosen = set(PRESETS[name]["sections"])
    if roles is not None:
        chosen.add("roles") if roles else chosen.discard("roles")
    if login_events is not None:
        chosen.add("login_events") if login_events else chosen.discard("login_events")
    if "roles" in chosen:
        chosen.add("users")            # roles are looked up for the exported users
    ordered = tuple(s for s in SECTIONS if s in chosen)
    if not ordered:
        raise ValueError("Choose at least one section to export.")
    return Selection(preset=name, sections=ordered, steps=_steps_for(chosen))


def _steps_for(chosen: set[str]) -> tuple[str, ...]:
    need = {"auth", "write"}
    if "roles" in chosen:
        need |= {"role_catalog", "roles"}
    if "catalog" in chosen:
        need.add("catalog")
    if "plans" in chosen:
        need |= {"plan_catalog", "entitlements"}
    # Account names are needed almost everywhere; the list is cheap
    # (one call per 200 accounts).
    if chosen & {"users", "accounts", "hierarchy", "plans", "login_events"}:
        need.add("accounts")
    if "hierarchy" in chosen:
        need.add("hierarchy")
    # Users are needed for users.csv, for role lookups, for users without a
    # plan, and to know which accounts have people (login events).
    if chosen & {"users", "roles", "plans", "login_events"}:
        need.add("users")
    if "login_events" in chosen:
        need.add("login_events")
    return tuple(k for k, _ in STEPS if k in need)


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #
# Observed sustained pace against the real API (latency-bound), used when no
# previous run tells us better.
DEFAULT_OBSERVED_PACE = 3.0


@dataclass
class Counts:
    users: int | None = None
    accounts: int | None = None
    entitlements: int | None = None
    resellers: int | None = None
    accounts_with_users: int | None = None
    plans: int | None = None
    features: int | None = None
    login_events: int | None = None


@dataclass
class Estimate:
    calls: int
    seconds: int
    basis: str                       # "previous run", "record counts", or "unknown"
    per_step: dict[str, int]
    notes: list[str]

    def to_dict(self) -> dict:
        return {"calls": self.calls, "seconds": self.seconds, "basis": self.basis,
                "perStep": self.per_step, "notes": self.notes}


def _pages(n: int, size: int) -> int:
    return max(1, math.ceil(n / size))


def estimate(selection: Selection, rate: float, counts: Counts | None,
             previous_steps: dict[str, dict] | None = None,
             previous_pace: float | None = None,
             ceilings_per_min: dict[str, float] | None = None) -> Estimate:
    """Estimate API calls and duration.

    `previous_steps` is the per-step {calls, seconds} from the last run;
    when a step ran last time its call count is reused. Otherwise calls are
    derived from record counts. Duration uses the slower of the chosen rate
    and the pace observed last time, and respects the per-endpoint ceilings.
    """
    from .client import ENDPOINT_CEILINGS_PER_MIN

    ceilings = ENDPOINT_CEILINGS_PER_MIN if ceilings_per_min is None else ceilings_per_min
    prev = previous_steps or {}
    c = counts or Counts()
    notes: list[str] = []
    per_step: dict[str, int] = {}
    unknown = False

    def from_counts(step: str) -> int | None:
        if step == "auth":
            return 1
        if step == "role_catalog":
            return 1
        if step == "catalog":       # permissions + features (until empty) + flags
            return 1 + (_pages(c.features, 100) + 1 if c.features is not None else 2) + 1
        if step == "plan_catalog":
            return (_pages(c.plans, 200) if c.plans is not None else 1) + 1
        if step == "accounts":
            return _pages(c.accounts, 200) if c.accounts is not None else None
        if step == "hierarchy":
            if c.resellers is None:
                notes.append("Plus one call per reseller account for the hierarchy.")
                return 0
            return c.resellers
        if step == "users":
            return _pages(c.users, 200) if c.users is not None else None
        if step == "roles":
            n = c.accounts_with_users
            if n is None and c.users is not None and c.accounts is not None:
                n = min(c.users, c.accounts)
                notes.append("Role lookups are an upper bound: one call per account that has users.")
            if n is None:
                return None
            return n + (c.users // 100 if c.users else 0)
        if step == "entitlements":
            return _pages(c.entitlements, 10) if c.entitlements is not None else None
        if step == "login_events":
            n = c.accounts_with_users
            if n is None and c.users is not None and c.accounts is not None:
                n = min(c.users, c.accounts)
            if n is None:
                return None
            extra = (c.login_events or 0) // 200
            notes.append("Login events need one call per account that has users.")
            return n + extra
        return 0

    used_previous = False
    for step in selection.steps:
        if step == "write":
            continue
        if step in prev and "calls" in prev[step]:
            per_step[step] = int(prev[step]["calls"])
            used_previous = True
            continue
        n = from_counts(step)
        if n is None:
            unknown = True
            n = 0
        per_step[step] = n

    pace = min(rate, previous_pace or DEFAULT_OBSERVED_PACE)
    seconds = 0.0
    step_paths = {"users": "/identity/resources/users/v3", "accounts": "/tenants/resources/tenants/v2"}
    for step, n in per_step.items():
        t = n / pace
        path = step_paths.get(step)
        if path and path in ceilings and n > 1:
            t = max(t, (n - 1) * 60.0 / ceilings[path])
        seconds += t
    calls = sum(per_step.values())
    if unknown and not used_previous:
        basis = "unknown"
        notes.insert(0, "Some counts aren't known yet, so this is a lower bound.")
    else:
        basis = "previous run" if used_previous else "record counts"
    return Estimate(calls=calls, seconds=int(round(seconds)), basis=basis, per_step=per_step,
                    notes=list(dict.fromkeys(notes)))


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return "under a minute" if seconds >= 1 else "a few seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours, mins = divmod(minutes, 60)
    return f"about {hours} h {mins:02d} min"
