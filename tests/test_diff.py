"""The diff engine (pure) and changes.csv end to end."""

from __future__ import annotations

import copy
import csv
import unittest

from frontegg_data_export.diff import CHANGES_HEADER, FIRST_RUN_MESSAGE, diff_models
from frontegg_data_export.model import build_model
from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import Faults, MockFrontegg, make_dataset, mutate
from tests.test_model import RUN_AT, tiny_data

ALL = ["users", "roles", "accounts", "hierarchy", "plans"]


def model(data: dict, **kw) -> dict:
    kw.setdefault("sections", ALL)
    kw.setdefault("run_started_at", RUN_AT)
    return build_model(data, **kw)


def rows(changes) -> set[tuple]:
    return {(c.change_type, c.entity, c.field, c.before, c.after) for c in changes}


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.old_data = tiny_data()
        self.new_data = copy.deepcopy(self.old_data)

    def diff(self, **kw):
        return diff_models(model(self.old_data, **kw.get("old", {})), model(self.new_data, **kw.get("new", {})))

    def test_no_changes(self):
        changes, summary = self.diff()
        self.assertEqual(changes, [])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["notCompared"], [])

    def test_volatile_fields_are_ignored_but_logins_counted(self):
        u = self.new_data["users"][0]
        u["lastLogin"] = "2026-10-09T00:00:00Z"
        u["updatedAt"] = "2026-10-09T00:00:00Z"
        u["metadata"] = '{"x": 1}'
        u["createdAt"] = "2020-01-01T00:00:00Z"
        changes, summary = self.diff()
        self.assertEqual(changes, [])
        self.assertEqual(summary["usersLoggedInSince"], 1)

    def test_user_added_removed_and_field_changes(self):
        self.new_data["users"][0].update(email="ada.new@example.com", name="Ada Renamed", verified=False)
        self.new_data["users"].pop(1)
        self.new_data["users"].append({"id": "u9", "email": "new@example.com", "name": "New Example",
                                       "tenantIds": ["t-child"], "tenants": [{"tenantId": "t-child"}]})
        changes, summary = self.diff()
        self.assertEqual(rows(changes), {
            ("added", "user", "", "", ""), ("removed", "user", "", "", ""),
            ("changed", "user", "email", "ada@example.com", "ada.new@example.com"),
            ("changed", "user", "name", "Ada Example", "Ada Renamed"),
            ("changed", "user", "verified", "yes", "no"),
        })
        added = next(c for c in changes if c.change_type == "added")
        self.assertEqual((added.name_or_email, added.account_name), ("new@example.com", "Acme 02"))
        c = summary["counts"]
        self.assertEqual((c["usersAdded"], c["usersRemoved"], c["usersChanged"]), (1, 1, 1))

    def test_memberships_disabled_and_roles(self):
        u = self.new_data["users"][0]
        u["tenantIds"] = ["t-child", "t-root"]
        u["tenants"] = [{"tenantId": "t-child", "isDisabled": True}, {"tenantId": "t-root"}]   # t-flat removed
        self.new_data["userRoleAssignments"] = [{"userId": "u1", "tenantId": "t-child", "roleIds": ["r-admin"]}]
        changes, _ = self.diff()
        self.assertEqual(rows(changes), {
            ("added", "membership", "", "", ""), ("removed", "membership", "", "", ""),
            ("changed", "membership", "disabled", "no", "yes"),
            ("changed", "membership", "roles", "Administrator; Viewer", "Administrator"),
        })
        added = next(c for c in changes if c.change_type == "added")
        self.assertEqual(added.account_name, "Acme Partners")

    def test_failed_role_lookup_is_skipped_not_reported(self):
        self.new_data["userRoleAssignments"] = []
        changes, summary = self.diff(new={"failed_role_tenants": ["t-child"]})
        self.assertNotIn("roles", {c.field for c in changes})
        # t-flat had no roles in either run and was looked up both times: unchanged, not skipped
        self.assertTrue(any("1 membership" in n for n in summary["notCompared"]))

    def test_roles_not_exported_in_one_run(self):
        del self.new_data["userRoleAssignments"]
        changes, summary = self.diff(new={"sections": ["users", "accounts", "hierarchy", "plans"]})
        self.assertEqual(changes, [])
        self.assertIn("Roles weren't looked up in both runs.", summary["notCompared"])

    def test_accounts_added_removed_renamed_moved(self):
        t = {x["tenantId"]: x for x in self.new_data["tenants"]}
        t["t-flat"]["name"] = "Acme 09 Renamed"
        t["t-root"]["isReseller"] = False
        self.new_data["tenants"].append({"tenantId": "t-new", "name": "Acme New"})
        # t-grand moves from under t-child to directly under t-root
        self.new_data["hierarchyTrees"] = [{"tenantId": "t-root", "children": [
            {"tenantId": "t-child", "children": []}, {"tenantId": "t-grand", "children": []}]}]
        changes, summary = self.diff()
        self.assertEqual(rows(changes), {
            ("added", "account", "", "", ""),
            ("changed", "account", "name", "Acme 09", "Acme 09 Renamed"),
            ("changed", "account", "reseller", "yes", "no"),
            ("changed", "account", "parent", "Acme 02", "Acme Partners"),
        })
        c = summary["counts"]
        self.assertEqual((c["accountsAdded"], c["accountsRenamed"], c["accountsMoved"]), (1, 1, 1))

    def test_moves_under_a_failed_tree_are_skipped(self):
        self.new_data["hierarchyTrees"] = []          # the tree couldn't be read this time
        changes, summary = self.diff(new={"failed_roots": ["t-root"]})
        self.assertNotIn("parent", {c.field for c in changes})
        self.assertTrue(any("2 account(s)" in n for n in summary["notCompared"]))

    def test_moved_to_top_level(self):
        self.new_data["hierarchyTrees"] = [{"tenantId": "t-root", "children": []},
                                           {"tenantId": "t-child", "children": [{"tenantId": "t-grand"}]}]
        changes, _ = self.diff()
        moved = [c for c in changes if c.field == "parent"]
        self.assertEqual([(c.name_or_email, c.before, c.after) for c in moved],
                         [("Acme 02", "Acme Partners", "(top level)")])

    def test_plan_assignments(self):
        ents = self.new_data["entitlements"]
        ents[:] = [e for e in ents if e["id"] != "e1"]                      # removed (account-level)
        ents.append({"id": "e7", "planId": "p-addon", "tenantId": "t-child", "userId": "u1",
                     "expirationDate": None})                              # added (user-level)
        next(e for e in ents if e["id"] == "e4")["expirationDate"] = None   # extended to never
        changes, summary = self.diff()
        self.assertEqual(rows(changes), {
            ("removed", "plan_assignment", "", "", ""),
            ("added", "plan_assignment", "user", "", "ada@example.com"),
            ("changed", "plan_assignment", "expires_at", "2028-01-01T00:00:00Z", "never"),
        })

    def test_recreated_assignment_is_not_a_change(self):
        next(e for e in self.new_data["entitlements"] if e["id"] == "e1")["id"] = "e1-recreated"
        changes, _ = self.diff()
        self.assertEqual(changes, [])

    def test_section_missing_in_one_run_is_not_compared(self):
        for k in ("tenants", "hierarchyTrees", "entitlements", "plans"):
            del self.new_data[k]
        changes, summary = self.diff(new={"sections": ["users", "roles"]})
        self.assertEqual(changes, [])
        self.assertIn("Accounts weren't exported in both runs.", summary["notCompared"])
        self.assertIn("Plan assignments weren't exported in both runs.", summary["notCompared"])

    def test_output_order_is_stable(self):
        self.new_data["users"].pop(0)
        self.new_data["tenants"].append({"tenantId": "t-new", "name": "Acme New"})
        changes, _ = self.diff()
        self.assertEqual([c.entity for c in changes], ["account", "user"])


class EndToEndDiffTests(unittest.TestCase):
    def test_first_run_then_known_changes(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            first = run_export(m, tmp)
            self.assertEqual(first.summary()["changes"]["message"], FIRST_RUN_MESSAGE)
            with open(first.run_dir / "changes.csv", encoding="utf-8-sig") as f:
                self.assertEqual(list(csv.reader(f)), [CHANGES_HEADER])

            expected = mutate(ds)
            second = run_export(m, tmp)
            self.assertEqual(second.code, 0)
            ch = second.summary()["changes"]
            self.assertEqual(ch["comparedWith"], first.run_dirs()[0].name)
            c = ch["counts"]
            self.assertEqual((c["usersAdded"], c["usersRemoved"]), (3, 2))
            self.assertEqual((c["accountsAdded"], c["accountsRemoved"], c["accountsRenamed"], c["accountsMoved"]),
                             (1, 1, 1, 1))
            self.assertEqual(c["planAssignmentsAdded"], 1)
            self.assertGreaterEqual(c["planAssignmentsRemoved"], 1)
            self.assertEqual(c["planExpiryChanges"], 1)
            self.assertGreaterEqual(ch["usersLoggedInSince"], len(expected["logged_in"]))
            with open(second.run_dir / "changes.csv", encoding="utf-8-sig") as f:
                got = list(csv.DictReader(f))
            self.assertEqual(len(got), ch["total"])
            fields = {(r["entity"], r["field"]) for r in got if r["change_type"] == "changed"}
            for want in [("user", "name"), ("user", "email"), ("user", "verified"), ("membership", "disabled"),
                         ("membership", "roles"), ("account", "name"), ("account", "parent"),
                         ("plan_assignment", "expires_at")]:
                self.assertIn(want, fields)
            renamed = next(r for r in got if r["field"] == "name" and r["entity"] == "user")
            self.assertEqual(renamed["after"], "Renamed Example")

    def test_partial_run_compares_but_skips_unknown_roles(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            run_export(m, tmp)
            bad = next(t["tenantId"] for t in ds.tenants if t["name"] == "Acme Big")
            m.faults.failing_role_tenants = {bad}
            second = run_export(m, tmp)
            self.assertEqual(second.code, 2)
            ch = second.summary()["changes"]
            self.assertEqual(ch["total"], 0)
            self.assertTrue(any("role lookup failed" in n for n in ch["notCompared"]))


if __name__ == "__main__":
    unittest.main()
