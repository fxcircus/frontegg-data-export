"""Deterministic fake Frontegg environment for tests and manual checks.

Every name, email and ID here is a generated placeholder: accounts are
"Acme NN", people are "<First> Example", emails are @example.com, IPs come
from the documentation range 203.0.113.0/24, and IDs are seeded random UUIDs.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FIRST_NAMES = ["Ada", "Ben", "Cleo", "Dev", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jo",
               "Kai", "Lea", "Max", "Nia", "Omar", "Pia", "Quinn", "Rae", "Sam", "Tia",
               "Uma", "Vic", "Wes", "Yui"]
LAST_NAMES = ["Example", "Sample", "Placeholder", "Testcase", "Demo", "Mock"]

# Names that exercise the CSV writer: accents (BOM) and formula injection.
SPECIAL_NAMES = ["Zoë Exämple", "Renée Exemple", "=SUM(1+1)", "+Plus Example",
                 "-Minus Example", "@At Example"]

LOGIN_OK_ACTION = "User logged in"                 # the action name a real audit log uses
IMPERSONATED_LOGIN_ACTION = "Impersonated by support@example.com - User logged in"
LOGIN_FAILED_ACTION = "User failed to log in"
OTHER_ACTIONS = ["Added user", "Assigned roles", "Created API key", "Reset password"]


def iso(dt: datetime) -> str:
    """Frontegg-style timestamp: milliseconds and a trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class Dataset:
    vendor_id: str
    tenants: list[dict]
    parent: dict[str, str]                        # child tenantId -> parent tenantId
    roles: list[dict]
    permissions: list[dict]
    users: list[dict]
    role_assignments: dict[tuple[str, str], list[str]]   # (userId, tenantId) -> roleIds
    plans: list[dict]
    features: list[dict]
    feature_flags: list[dict]
    entitlements: list[dict]
    audits: list[dict]
    rng: random.Random = field(repr=False, default_factory=random.Random)
    # Roles an account created for itself: /roles/v1 only returns them with that
    # account's frontegg-tenant-id header.
    account_roles: list[dict] = field(default_factory=list)
    # An account users still belong to, but that the account list doesn't return.
    ghost_tenant_id: str = ""

    # ---- helpers ---------------------------------------------------------
    def uid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def tenant(self, tenant_id: str) -> dict | None:
        return next((t for t in self.tenants if t["tenantId"] == tenant_id), None)

    def user(self, user_id: str) -> dict | None:
        return next((u for u in self.users if u["id"] == user_id), None)

    def children_of(self, tenant_id: str) -> list[str]:
        return [c for c, p in self.parent.items() if p == tenant_id]

    def enriched_plans(self) -> list[dict]:
        out = []
        for p in self.plans:
            ents = [e for e in self.entitlements if e["planId"] == p["id"]]
            out.append({
                **p,
                "assignedTenantsCount": sum(1 for e in ents if not e.get("userId")),
                "assignedUsersCount": sum(1 for e in ents if e.get("userId")),
                "featuresCount": len(p.get("featureIds", [])),
            })
        return out


def make_dataset(seed: int = 7, tenants: int = 12, big_tenant_users: int = 230,
                 now: datetime | None = None) -> Dataset:
    """Build a small environment that still hits every pagination path:
    users and tenants span multiple 200-item pages once the big tenant is
    included, entitlements exceed one 10-item page, there are more than 10
    plans, and one tenant has more users than one role-lookup batch."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    base = now - timedelta(days=400)
    ds = Dataset(vendor_id="", tenants=[], parent={}, roles=[], permissions=[], users=[],
                 role_assignments={}, plans=[], features=[], feature_flags=[],
                 entitlements=[], audits=[], rng=rng)
    ds.vendor_id = ds.uid()

    # ---- tenants + hierarchy ---------------------------------------------
    for i in range(tenants):
        if i == 0:
            name, reseller = "Acme Partners", True
        elif i == 1:
            name, reseller = "Acme Resale", True
        elif i == tenants - 1:
            name, reseller = "Acme Big", False
        else:
            name, reseller = f"Acme {i:02d}", False
        created = base + timedelta(days=i * 3)
        ds.tenants.append({
            "tenantId": ds.uid(), "name": name, "isReseller": reseller,
            "vendorId": ds.vendor_id, "createdAt": iso(created), "updatedAt": iso(created),
            "metadata": "{}", "deletedAt": None,
        })
    t = [x["tenantId"] for x in ds.tenants]
    # Acme Partners -> Acme 02, Acme 03, Acme 04 ; Acme 02 -> Acme 05 ; Acme Resale -> Acme 06
    if tenants >= 7:
        ds.parent.update({t[2]: t[0], t[3]: t[0], t[4]: t[0], t[5]: t[2], t[6]: t[1]})

    # ---- permissions + roles ---------------------------------------------
    for key in ["users.read", "users.write", "billing.read", "billing.write", "reports.read", "settings.write"]:
        ds.permissions.append({"id": ds.uid(), "key": f"example.{key}", "name": key.replace(".", " ").title(),
                               "roleIds": []})
    role_defs = [("Admin", "Administrator", [0, 1, 2, 3, 4, 5]), ("Member", "Member", [0, 4]),
                 ("Viewer", "Viewer", [0]), ("Billing", "Billing manager", [2, 3])]
    for key, name, perm_idx in role_defs:
        rid = ds.uid()
        perms = [ds.permissions[i] for i in perm_idx]
        for p in perms:
            p["roleIds"].append(rid)
        ds.roles.append({"id": rid, "key": key, "name": name, "vendorId": ds.vendor_id,
                         "isDefault": key == "Member", "permissions": [{"id": p["id"], "key": p["key"]} for p in perms]})

    # ---- users ------------------------------------------------------------
    specials = list(SPECIAL_NAMES)
    counter = 0

    def new_user(tenant_ids: list[str]) -> dict:
        nonlocal counter
        counter += 1
        if specials and counter % 7 == 0:
            name = specials.pop(0)
        else:
            name = f"{FIRST_NAMES[counter % len(FIRST_NAMES)]} {LAST_NAMES[counter % len(LAST_NAMES)]}"
        created = base + timedelta(days=rng.randint(0, 300), seconds=rng.randint(0, 86399))
        last_login = None if counter % 9 == 0 else iso(now - timedelta(days=rng.randint(1, 60)))
        u = {
            "id": ds.uid(), "email": f"user{counter:04d}@example.com", "name": name,
            "verified": counter % 10 != 0, "mfaEnrolled": counter % 4 == 0, "isLocked": False,
            "provider": "local", "managedBy": "frontegg", "createdAt": iso(created),
            "lastLogin": last_login, "metadata": "{}", "vendorMetadata": None,
            "tenantId": tenant_ids[0] if tenant_ids else None, "tenantIds": list(tenant_ids),
            "tenants": [{"tenantId": tid, "isDisabled": False, "temporaryExpirationDate": None}
                        for tid in tenant_ids],
        }
        ds.users.append(u)
        return u

    for i, tid in enumerate(t):
        n = big_tenant_users if i == len(t) - 1 else rng.randint(1, 8)
        for _ in range(n):
            new_user([tid])
    # ~10% of small-tenant users also belong to a second tenant
    small = [u for u in ds.users if u["tenantId"] != t[-1]]
    for u in rng.sample(small, max(1, len(small) // 10)):
        other = rng.choice([x for x in t[:-1] if x not in u["tenantIds"]])
        u["tenantIds"].append(other)
        u["tenants"].append({"tenantId": other, "isDisabled": False, "temporaryExpirationDate": None})
    # one disabled membership, one user with no memberships at all
    ds.users[3]["tenants"][0]["isDisabled"] = True
    new_user([])

    # ---- role assignments (1-2 roles per membership) ---------------------
    role_ids = [r["id"] for r in ds.roles]
    for u in ds.users:
        for tid in u["tenantIds"]:
            ds.role_assignments[(u["id"], tid)] = sorted(rng.sample(role_ids, rng.randint(1, 2)))

    # ---- features, flags, plans ------------------------------------------
    for i in range(7):
        ds.features.append({"id": ds.uid(), "key": f"feature-{chr(97 + i)}", "name": f"Feature {chr(65 + i)}",
                            "createdAt": iso(base), "updatedAt": iso(base)})
    ds.feature_flags = [{"id": ds.uid(), "on": True, "defaultTreatment": "true", "name": "New reports"},
                        {"id": ds.uid(), "on": False, "defaultTreatment": "false", "name": "Beta export"}]
    plan_names = ["Free", "Starter", "Pro", "Business", "Enterprise", "Add-on A", "Add-on B",
                  "Add-on C", "Add-on D", "Add-on E", "Add-on F", "Trial"]
    for i, name in enumerate(plan_names):
        ds.plans.append({
            "id": ds.uid(), "name": name, "description": f"{name} plan",
            "featureIds": [f["id"] for f in ds.features[: (i % 7) + 1]],
            "defaultTreatment": "false", "rules": [], "assignOnSignup": False,
            "createdAt": iso(base + timedelta(days=i)), "updatedAt": iso(base + timedelta(days=i)),
        })

    # ---- entitlements -----------------------------------------------------
    def add_ent(plan_idx: int, tenant_id: str, user_id: str | None, expiration: str | None) -> None:
        created = base + timedelta(days=rng.randint(0, 300))
        ds.entitlements.append({
            "id": ds.uid(), "planId": ds.plans[plan_idx]["id"], "tenantId": tenant_id,
            "userId": user_id, "expirationDate": expiration,
            "createdAt": iso(created), "updatedAt": iso(created),
        })

    past = (now - timedelta(days=30)).replace(microsecond=0)
    future = (now + timedelta(days=365)).replace(microsecond=0)
    for i, tid in enumerate(t):
        if i % 4 == 3:
            continue                                   # some accounts have no plan at all
        exp = None
        if i % 5 == 1:
            exp = past.strftime("%Y-%m-%dT%H:%M:%S")   # expired; no timezone, like the docs' example
        elif i % 5 == 2:
            exp = iso(future)
        add_ent(i % len(ds.plans), tid, None, exp)
    # user-level assignments, some in tenants without an account-level plan
    for u in ds.users[::6]:
        if u["tenantIds"]:
            add_ent(5 + len(ds.entitlements) % 5, u["tenantIds"][0], u["id"],
                    None if len(ds.entitlements) % 3 else iso(future))

    # ---- audit logs (login events and noise) ------------------------------
    for u in ds.users:
        for tid in u["tenantIds"]:
            for k in range(rng.randint(0, 3)):
                when = now - timedelta(days=rng.randint(0, 20), seconds=rng.randint(0, 86399))
                action = rng.choice([LOGIN_OK_ACTION, LOGIN_OK_ACTION, LOGIN_FAILED_ACTION] + OTHER_ACTIONS)
                ds.audits.append({     # field names as the real audits API returns them
                    "frontegg_id": ds.uid(), "tenantId": tid, "vendorId": ds.vendor_id,
                    "environmentName": "Development",
                    "actorId": u["id"], "email": u["email"], "action": action,
                    "severity": "Medium" if action == LOGIN_FAILED_ACTION else "Info",
                    "ip": f"203.0.113.{rng.randint(1, 254)}",
                    "userAgent": "Mozilla/5.0 (placeholder)", "description": action,
                    "createdAt": iso(when),
                })

    # ---- seen in a real environment (added last so earlier data is unchanged)
    # An account-level role, defined by one account and assigned there.
    acct = t[3] if len(t) > 3 else t[0]
    custom = {"id": ds.uid(), "key": "Auditor", "name": "Auditor", "vendorId": ds.vendor_id, "tenantId": acct,
              "level": 0, "isDefault": False, "permissions": []}
    ds.account_roles.append(custom)
    member = next((u for u in ds.users if acct in u["tenantIds"]), None)
    if member:
        ds.role_assignments[(member["id"], acct)] = sorted(ds.role_assignments[(member["id"], acct)] + [custom["id"]])
    # A membership in an account that no longer exists: the account list doesn't
    # return it, but users still list it and the role lookup still answers.
    ghost = ds.uid()
    ds.ghost_tenant_id = ghost
    lingering = ds.users[10] if len(ds.users) > 10 else ds.users[0]
    lingering["tenantIds"].append(ghost)
    lingering["tenants"].append({"tenantId": ghost, "isDisabled": False, "temporaryExpirationDate": None})
    ds.role_assignments[(lingering["id"], ghost)] = [ds.roles[1]["id"]]
    # Impersonated logins, as the real audit log words them.
    for u in ds.users[:2]:
        if u["tenantIds"]:
            ds.audits.append({"frontegg_id": ds.uid(), "tenantId": u["tenantIds"][0], "vendorId": ds.vendor_id,
                              "environmentName": "Development", "actorId": u["id"], "email": u["email"],
                              "action": IMPERSONATED_LOGIN_ACTION, "severity": "Info", "ip": "", "userAgent": "",
                              "description": IMPERSONATED_LOGIN_ACTION,
                              "createdAt": iso(now - timedelta(days=rng.randint(0, 20)))})
    ds.audits.sort(key=lambda a: a["createdAt"])
    return ds


def mutate(ds: Dataset, now: datetime | None = None) -> dict:
    """Apply a known set of changes (the "second run" of a diff scenario).
    Returns a description of what changed so tests can assert on it."""
    now = now or datetime.now(timezone.utc)
    t = [x["tenantId"] for x in ds.tenants]
    changes: dict = {}

    # users removed / added
    removed = [ds.users[1], ds.users[2]]
    for u in removed:
        ds.users.remove(u)
        for tid in u["tenantIds"]:
            ds.role_assignments.pop((u["id"], tid), None)
    changes["users_removed"] = [u["id"] for u in removed]
    added = []
    for k in range(3):
        uid = ds.uid()
        u = {"id": uid, "email": f"new{k}@example.com", "name": f"New Example {k}", "verified": False,
             "mfaEnrolled": False, "isLocked": False, "provider": "local", "managedBy": "frontegg",
             "createdAt": iso(now), "lastLogin": None, "metadata": "{}", "vendorMetadata": None,
             "tenantId": t[2], "tenantIds": [t[2]],
             "tenants": [{"tenantId": t[2], "isDisabled": False, "temporaryExpirationDate": None}]}
        ds.users.append(u)
        ds.role_assignments[(uid, t[2])] = [ds.roles[1]["id"]]
        added.append(uid)
    changes["users_added"] = added

    # field changes on existing users
    u_name, u_email, u_verify = ds.users[4], ds.users[5], ds.users[9]
    changes["renamed_user"] = (u_name["id"], u_name["name"], "Renamed Example")
    u_name["name"] = "Renamed Example"
    changes["email_user"] = (u_email["id"], u_email["email"], "changed@example.com")
    u_email["email"] = "changed@example.com"
    changes["verified_user"] = (u_verify["id"], u_verify["verified"], not u_verify["verified"])
    u_verify["verified"] = not u_verify["verified"]

    # membership changes: disable one, add one, change roles of one
    u_dis = ds.users[6]
    u_dis["tenants"][0]["isDisabled"] = True
    changes["disabled_membership"] = (u_dis["id"], u_dis["tenants"][0]["tenantId"])
    u_join = ds.users[7]
    target = next(x for x in t[:-1] if x not in u_join["tenantIds"])
    u_join["tenantIds"].append(target)
    u_join["tenants"].append({"tenantId": target, "isDisabled": False, "temporaryExpirationDate": None})
    ds.role_assignments[(u_join["id"], target)] = [ds.roles[2]["id"]]
    changes["membership_added"] = (u_join["id"], target)
    u_roles = ds.users[8]
    key = (u_roles["id"], u_roles["tenantIds"][0])
    ds.role_assignments[key] = [ds.roles[0]["id"], ds.roles[3]["id"]]
    changes["roles_changed"] = key

    # volatile only: new logins must not count as changes
    for u in ds.users[10:15]:
        u["lastLogin"] = iso(now)
        u["updatedAt"] = iso(now)
    changes["logged_in"] = [u["id"] for u in ds.users[10:15]]

    # accounts: rename, move, add, remove
    ds.tenants[3]["name"] = "Acme 03 Renamed"
    changes["account_renamed"] = t[3]
    ds.parent[t[4]] = t[1]                     # Acme 04 moves from Acme Partners to Acme Resale
    changes["account_moved"] = t[4]
    new_tid = ds.uid()
    ds.tenants.append({"tenantId": new_tid, "name": "Acme New", "isReseller": False, "vendorId": ds.vendor_id,
                       "createdAt": iso(now), "updatedAt": iso(now), "metadata": "{}", "deletedAt": None})
    changes["account_added"] = new_tid
    gone = ds.tenants[7]
    ds.tenants.remove(gone)
    for u in ds.users:
        if gone["tenantId"] in u["tenantIds"]:
            u["tenantIds"].remove(gone["tenantId"])
            u["tenants"] = [m for m in u["tenants"] if m["tenantId"] != gone["tenantId"]]
            u["tenantId"] = u["tenantIds"][0] if u["tenantIds"] else None
            ds.role_assignments.pop((u["id"], gone["tenantId"]), None)
    ds.entitlements = [e for e in ds.entitlements if e["tenantId"] != gone["tenantId"]]
    changes["account_removed"] = gone["tenantId"]

    # plan assignments: add, remove, change expiry
    ds.entitlements.append({"id": ds.uid(), "planId": ds.plans[2]["id"], "tenantId": new_tid, "userId": None,
                            "expirationDate": None, "createdAt": iso(now), "updatedAt": iso(now)})
    changes["entitlement_added"] = (ds.plans[2]["id"], new_tid)
    dropped = next(e for e in ds.entitlements if e["userId"] is None and e["tenantId"] != new_tid)
    ds.entitlements.remove(dropped)
    changes["entitlement_removed"] = (dropped["planId"], dropped["tenantId"])
    extended = next(e for e in ds.entitlements if e["userId"] is not None)
    extended["expirationDate"] = iso((now + timedelta(days=700)).replace(microsecond=0))
    changes["entitlement_expiry_changed"] = (extended["planId"], extended["tenantId"], extended["userId"])
    return changes
