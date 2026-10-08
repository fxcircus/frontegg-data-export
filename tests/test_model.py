"""The normalized model and snapshot 2.0."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from frontegg_data_export.model import (
    build_model,
    index_assignments,
    iso_z,
    parents_from_trees,
    parse_ts,
    plans_for_membership,
)
from frontegg_data_export.snapshot import enrich_users_with_roles
from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import Faults, MockFrontegg, make_dataset

RUN_AT = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def tiny_data() -> dict:
    return {
        "roles": [{"id": "r-admin", "key": "Admin", "name": "Administrator"},
                  {"id": "r-view", "key": "Viewer", "name": ""}],
        "tenants": [{"tenantId": "t-root", "name": "Acme Partners", "isReseller": True},
                    {"tenantId": "t-child", "name": "Acme 02"},
                    {"tenantId": "t-grand", "name": "Acme 05"},
                    {"tenantId": "t-flat", "name": "Acme 09", "createdAt": "2025-01-02T03:04:05.678Z"}],
        "hierarchyTrees": [{"tenantId": "t-root", "children": [
            {"tenantId": "t-child", "children": [{"tenantId": "t-grand", "children": []}]}]}],
        "users": [
            {"id": "u1", "email": "ada@example.com", "name": "Ada Example", "verified": True,
             "lastLogin": "2026-10-01T00:00:00.000Z", "tenantIds": ["t-child", "t-flat"],
             "tenants": [{"tenantId": "t-child", "isDisabled": False}, {"tenantId": "t-flat", "isDisabled": True}]},
            {"id": "u2", "email": "ben@example.com", "name": "Ben Sample", "tenantIds": ["t-grand"],
             "tenants": [{"tenantId": "t-grand"}]},
        ],
        "userRoleAssignments": [{"userId": "u1", "tenantId": "t-child", "roleIds": ["r-view", "r-admin"]}],
        "plans": [{"id": "p-pro", "name": "Pro"}, {"id": "p-addon", "name": "Add-on", "rules": [{"x": 1}]},
                  {"id": "p-free", "name": "Free", "defaultTreatment": "true"}],
        "entitlements": [
            {"id": "e1", "planId": "p-pro", "tenantId": "t-child", "userId": None, "expirationDate": None},
            {"id": "e2", "planId": "p-addon", "tenantId": "t-flat", "userId": "u1",
             "expirationDate": "2026-01-01T00:00:00"},                                   # expired, no tz
            {"id": "e3", "planId": "p-pro", "tenantId": "t-grand", "userId": None,
             "expirationDate": "2027-01-01T00:00:00Z"},
            {"id": "e4", "planId": "p-pro", "tenantId": "t-grand", "userId": None,
             "expirationDate": "2028-01-01T00:00:00Z"},                                  # same target, later
        ],
    }


class TimestampTests(unittest.TestCase):
    def test_shapes(self):
        utc = timezone.utc
        self.assertEqual(parse_ts("2022-01-01T12:00:00"), datetime(2022, 1, 1, 12, tzinfo=utc))
        self.assertEqual(parse_ts("2024-03-04T05:06:07.123Z"), datetime(2024, 3, 4, 5, 6, 7, 123000, tzinfo=utc))
        self.assertEqual(parse_ts("2024-03-04T07:06:07+02:00"), datetime(2024, 3, 4, 5, 6, 7, tzinfo=utc))
        self.assertEqual(parse_ts("2024-01-01T00:00:00.1234567Z").microsecond, 123456)
        self.assertEqual(parse_ts(1_700_000_000_000), datetime.fromtimestamp(1_700_000_000, tz=utc))
        for bad in (None, "", "yesterday", "2024-13-01"):
            self.assertIsNone(parse_ts(bad))

    def test_iso_z(self):
        self.assertEqual(iso_z("2024-03-04T05:06:07.999Z"), "2024-03-04T05:06:07Z")
        self.assertEqual(iso_z(None), "")


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.m = build_model(tiny_data(), sections=["users", "roles", "accounts", "hierarchy", "plans"],
                             run_started_at=RUN_AT, failed_role_tenants=["t-grand"])

    def test_hierarchy_parents_and_roots(self):
        acc = self.m["accounts"]
        self.assertEqual((acc["t-child"]["parentId"], acc["t-child"]["rootId"]), ("t-root", "t-root"))
        self.assertEqual((acc["t-grand"]["parentId"], acc["t-grand"]["rootId"]), ("t-child", "t-root"))
        self.assertEqual((acc["t-root"]["parentId"], acc["t-root"]["rootId"]), (None, "t-root"))
        self.assertEqual((acc["t-flat"]["parentId"], acc["t-flat"]["rootId"]), (None, None))
        self.assertEqual(acc["t-flat"]["createdAt"], "2025-01-02T03:04:05Z")

    def test_loops_in_tree_data_do_not_hang(self):
        loop = {"tenantId": "a", "children": [{"tenantId": "b", "children": [{"tenantId": "a", "children": []}]}]}
        self.assertEqual(parents_from_trees([loop]), {"a": (None, "a"), "b": ("a", "a")})

    def test_memberships_roles_and_unknowns(self):
        u1 = self.m["users"]["u1"]["memberships"]
        self.assertEqual(u1["t-child"], {"disabled": False, "roleIds": ["r-admin", "r-view"]})
        self.assertEqual(u1["t-flat"], {"disabled": True, "roleIds": []})          # looked up: none
        u2 = self.m["users"]["u2"]["memberships"]
        self.assertEqual(u2["t-grand"]["roleIds"], None)                            # lookup failed: unknown
        self.assertEqual(self.m["roles"]["r-view"], "Viewer")                       # name falls back to key

    def test_roles_not_exported_means_unknown(self):
        data = tiny_data()
        del data["userRoleAssignments"]
        m = build_model(data, sections=["users"], run_started_at=RUN_AT)
        self.assertIsNone(m["users"]["u1"]["memberships"]["t-child"]["roleIds"])
        self.assertFalse(m["has"]["roles"])

    def test_plan_assignments(self):
        pa = self.m["planAssignments"]
        self.assertEqual(pa["p-addon|t-flat|u1"]["expired"], True)
        self.assertEqual(pa["p-addon|t-flat|u1"]["expiresAt"], "2026-01-01T00:00:00Z")
        self.assertEqual(pa["p-pro|t-child|"]["expiresAt"], "")
        self.assertEqual(pa["p-pro|t-grand|"]["id"], "e4")                   # the longer expiry wins
        self.assertEqual(len(pa), 3)
        self.assertEqual({k for k, v in self.m["plans"].items() if v["usesRules"]}, {"p-addon", "p-free"})

    def test_plans_for_membership_has_no_hierarchy_inheritance(self):
        idx = index_assignments(self.m)
        names = lambda rows: sorted(r["planName"] for r in rows)  # noqa: E731
        self.assertEqual(names(plans_for_membership(idx, "t-child", "u1")), ["Pro"])
        self.assertEqual(names(plans_for_membership(idx, "t-flat", "u1")), [])            # expired
        self.assertEqual(names(plans_for_membership(idx, "t-flat", "u1", include_expired=True)), ["Add-on"])
        # t-root has no assignment of its own; t-child's plan does not flow up or down
        self.assertEqual(names(plans_for_membership(idx, "t-root", None)), [])
        self.assertEqual(names(plans_for_membership(idx, "t-grand", "u2")), ["Pro"])


class SnapshotTests(unittest.TestCase):
    def test_tenant_roles_keep_unknown_distinct(self):
        data = tiny_data()
        users = enrich_users_with_roles(data["users"], data["userRoleAssignments"], data["roles"], {"t-flat"})
        tr = {t["tenantId"]: t for t in users[0]["tenantRoles"]}
        self.assertEqual([r["key"] for r in tr["t-child"]["roles"]], ["Viewer", "Admin"])
        self.assertEqual(tr["t-flat"], {"tenantId": "t-flat", "roles": None, "lookupFailed": True})
        self.assertNotIn("tenantRoles", data["users"][0])        # the input is not mutated

    def test_end_to_end_files(self):
        ds = make_dataset()
        bad = next(t["tenantId"] for t in ds.tenants if t["name"] == "Acme 02")
        with MockFrontegg(ds, Faults(failing_role_tenants={bad})) as m, temp_dir() as tmp:
            r = run_export(m, tmp, preset="full")
            snap = r.snapshot()
            self.assertEqual(snap["schemaVersion"], "2.0")
            self.assertEqual(set(snap["data"]), {"roles", "permissions", "features", "featureFlags", "plans",
                                                 "tenants", "hierarchyTrees", "users", "userRoleAssignments",
                                                 "entitlements"})
            self.assertEqual(snap["counts"]["planAssignments"], len(ds.entitlements))
            self.assertTrue(all("tenantRoles" in u for u in snap["data"]["users"]))
            model = json.loads((r.run_dir / "normalized.json").read_text(encoding="utf-8"))
            self.assertEqual(len(model["users"]), len(ds.users))
            self.assertEqual(model["failedRoleTenants"], [bad])
            self.assertIn("normalized.json", r.summary()["files"])


if __name__ == "__main__":
    unittest.main()
