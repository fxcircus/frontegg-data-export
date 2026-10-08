"""CSV writer: BOM, injection escaping, joins, and each file's rules."""

from __future__ import annotations

import csv
import io
import unittest
from pathlib import Path

from frontegg_data_export import csvout
from frontegg_data_export.csvout import cell, write_csv
from frontegg_data_export.model import build_model
from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import MockFrontegg, make_dataset
from tests.test_model import RUN_AT, tiny_data


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class CellTests(unittest.TestCase):
    def test_formula_injection_is_neutralised(self):
        for raw in ("=SUM(1+1)", "+1", "-1", "@cmd", "\tx", "\rx", "=HYPERLINK(\"http://x\")"):
            with self.subTest(raw=raw):
                self.assertEqual(cell(raw), "'" + raw)

    def test_ordinary_values(self):
        self.assertEqual(cell("Ada Example"), "Ada Example")
        self.assertEqual(cell("a=b"), "a=b")               # only a leading character matters
        self.assertEqual(cell(None), "")
        self.assertEqual(cell(True), "yes")
        self.assertEqual(cell(False), "no")
        self.assertEqual(cell(42), "42")

    def test_multi_value_join(self):
        self.assertEqual(cell(["Admin", "Viewer"]), "Admin; Viewer")
        self.assertEqual(cell(["Admin", None, ""]), "Admin")
        self.assertEqual(cell(["=evil", "ok"]), "'=evil; ok")
        self.assertEqual(cell([]), "")


class WriterTests(unittest.TestCase):
    def test_bom_crlf_and_quoting(self):
        with temp_dir() as tmp:
            path = Path(tmp) / "x.csv"
            n = write_csv(path, ["a", "b"], [["Zoë Exämple", 'say "hi", ok'], ["line\nbreak", "=1"]])
            self.assertEqual(n, 2)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"a,b\r\n", raw)
            rows = read_csv(path)
            self.assertEqual(rows[0], {"a": "Zoë Exämple", "b": 'say "hi", ok'})
            self.assertEqual(rows[1], {"a": "line\nbreak", "b": "'=1"})


class RowRuleTests(unittest.TestCase):
    def setUp(self):
        self.model = build_model(tiny_data(), sections=["users", "roles", "accounts", "hierarchy", "plans"],
                                 run_started_at=RUN_AT, failed_role_tenants=["t-grand"])

    def rows(self, header, gen):
        return [dict(zip(header, (cell(v) for v in row))) for row in gen]

    def test_users_rows(self):
        rows = self.rows(csvout.USERS_HEADER, csvout.users_rows(self.model))
        by = {(r["email"], r["account_name"]): r for r in rows}
        r = by[("ada@example.com", "Acme 02")]
        self.assertEqual((r["roles"], r["plans"], r["verified"], r["disabled_in_account"]),
                         ("Administrator; Viewer", "Pro", "yes", "no"))
        self.assertEqual(r["last_login"], "2026-10-01T00:00:00Z")
        r = by[("ada@example.com", "Acme 09")]
        self.assertEqual((r["roles"], r["plans"], r["disabled_in_account"]), ("", "", "yes"))
        self.assertEqual(by[("ben@example.com", "Acme 05")]["roles"], "(lookup failed)")
        self.assertEqual([r["email"] for r in rows], ["ada@example.com", "ada@example.com", "ben@example.com"])

    def test_user_without_memberships_gets_one_row(self):
        data = tiny_data()
        data["users"].append({"id": "u3", "email": "solo@example.com", "name": "Solo Example",
                              "tenantIds": [], "tenants": []})
        model = build_model(data, sections=["users"], run_started_at=RUN_AT)
        rows = self.rows(csvout.USERS_HEADER, csvout.users_rows(model))
        solo = [r for r in rows if r["email"] == "solo@example.com"]
        self.assertEqual(len(solo), 1)
        self.assertEqual(solo[0]["account_id"], "")

    def test_sections_not_exported_leave_blanks(self):
        data = tiny_data()
        for k in ("userRoleAssignments", "entitlements", "plans", "hierarchyTrees"):
            del data[k]
        model = build_model(data, sections=["users"], run_started_at=RUN_AT)
        rows = self.rows(csvout.USERS_HEADER, csvout.users_rows(model))
        self.assertTrue(all(r["roles"] == "" and r["plans"] == "" for r in rows))
        accts = self.rows(csvout.ACCOUNTS_HEADER, csvout.accounts_rows(model))
        self.assertTrue(all(r["parent_account_id"] == "" and r["plans"] == "" for r in accts))

    def test_accounts_rows(self):
        rows = {r["account_id"]: r for r in self.rows(csvout.ACCOUNTS_HEADER, csvout.accounts_rows(self.model))}
        self.assertEqual((rows["t-grand"]["parent_account_name"], rows["t-grand"]["parent_account_id"]),
                         ("Acme 02", "t-child"))
        self.assertEqual(rows["t-root"]["is_reseller"], "yes")
        self.assertEqual(rows["t-child"]["user_count"], "1")
        self.assertEqual(rows["t-root"]["user_count"], "0")
        self.assertEqual(rows["t-grand"]["plans"], "Pro")
        self.assertEqual(rows["t-flat"]["plans"], "")       # only a user-level, expired plan there

    def test_plan_assignment_rows(self):
        rows = self.rows(csvout.PLAN_ASSIGNMENTS_HEADER, csvout.plan_assignment_rows(self.model))
        addon = next(r for r in rows if r["plan_name"] == "Add-on")
        self.assertEqual((addon["level"], addon["user_email"], addon["expired"], addon["expires_at"]),
                         ("user", "ada@example.com", "yes", "2026-01-01T00:00:00Z"))
        pro = [r for r in rows if r["plan_name"] == "Pro"]
        self.assertEqual({(r["level"], r["user_id"], r["expired"]) for r in pro}, {("account", "", "no")})

    def test_users_without_plan_rule(self):
        rows = self.rows(csvout.USERS_WITHOUT_PLAN_HEADER, csvout.users_without_plan_rows(self.model))
        self.assertEqual([(r["email"], r["account_name"], r["expired_plans"]) for r in rows],
                         [("ada@example.com", "Acme 09", "Add-on")])

    def test_user_level_plan_counts(self):
        data = tiny_data()
        data["entitlements"].append({"id": "e9", "planId": "p-pro", "tenantId": "t-flat", "userId": "u1",
                                     "expirationDate": None})
        model = build_model(data, sections=["users", "plans"], run_started_at=RUN_AT)
        rows = list(csvout.users_without_plan_rows(model))
        self.assertEqual(rows, [])


class EndToEndCsvTests(unittest.TestCase):
    def test_standard_preset_files(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            names = sorted(p.name for p in r.run_dir.glob("*.csv"))
            self.assertEqual(names, ["accounts.csv", "plan_assignments.csv", "users.csv",
                                     "users_without_plan.csv"])
            users = read_csv(r.run_dir / "users.csv")
            self.assertEqual(len(users), sum(max(1, len(u["tenantIds"])) for u in ds.users))
            injected = [u["name"] for u in users if u["name"].startswith("'")]
            self.assertIn("'=SUM(1+1)", injected)
            self.assertIn("Zoë Exämple", {u["name"] for u in users})
            self.assertTrue((r.run_dir / "users.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
            accounts = read_csv(r.run_dir / "accounts.csv")
            moved = next(a for a in accounts if a["name"] == "Acme 05")
            self.assertEqual(moved["parent_account_name"], "Acme 02")
            self.assertEqual(len(read_csv(r.run_dir / "plan_assignments.csv")), len(ds.entitlements))
            self.assertEqual(r.summary()["rows"]["users.csv"], len(users))

    def test_users_only_preset_writes_only_users_csv(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            r = run_export(m, tmp, preset="users")
            self.assertEqual(sorted(p.name for p in r.run_dir.glob("*.csv")), ["users.csv"])
            users = read_csv(r.run_dir / "users.csv")
            self.assertTrue(all(u["account_name"] for u in users if u["account_id"]))
            self.assertTrue(any(u["roles"] for u in users))
            self.assertTrue(all(u["plans"] == "" for u in users))

    def test_json_only(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            r = run_export(m, tmp, preset="users", formats=("json",))
            self.assertEqual(list(r.run_dir.glob("*.csv")), [])
            self.assertTrue((r.run_dir / "snapshot.json").exists())

    def test_rules_note(self):
        ds = make_dataset()
        ds.plans[0]["rules"] = [{"attribute": "country", "op": "in", "value": ["XX"]}]
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertTrue(any("targeting rules" in n for n in r.summary()["notes"]))


if __name__ == "__main__":
    unittest.main()
