"""Login events: date range, classification, pagination, and unavailability."""

from __future__ import annotations

import csv
import unittest
from datetime import datetime, timedelta, timezone

from frontegg_data_export.loginevents import AUDITS_PATH, classify, date_range
from frontegg_data_export.sections import Counts, estimate, resolve
from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import Faults, MockFrontegg, make_dataset
from tests.mock_frontegg.data import IMPERSONATED_LOGIN_ACTION, LOGIN_FAILED_ACTION, LOGIN_OK_ACTION

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


class DateRangeTests(unittest.TestCase):
    def test_since_previous_success(self):
        last = NOW - timedelta(days=7)
        self.assertEqual(date_range(NOW, last, None, 30), (last, NOW, False))

    def test_first_run_uses_the_maximum(self):
        self.assertEqual(date_range(NOW, None, None, 30), (NOW - timedelta(days=30), NOW, False))

    def test_capped(self):
        start, end, capped = date_range(NOW, NOW - timedelta(days=90), None, 30)
        self.assertEqual((start, capped), (NOW - timedelta(days=30), True))
        start, _, capped = date_range(NOW, None, NOW - timedelta(days=400), 14)
        self.assertEqual((start, capped), (NOW - timedelta(days=14), True))

    def test_explicit_since_wins_inside_the_cap(self):
        since = NOW - timedelta(days=3)
        self.assertEqual(date_range(NOW, NOW - timedelta(days=1), since, 30)[0], since)


class ClassifyTests(unittest.TestCase):
    def test_known_and_guessed_names(self):
        cases = {
            LOGIN_OK_ACTION: "success", LOGIN_FAILED_ACTION: "failure", "Login": "success",
            IMPERSONATED_LOGIN_ACTION: "success",
            "login.completed": "success", "login.invalid_password": "failure",
            "frontegg.user.authenticated": "success", "frontegg.user.failedAuthentication": "failure",
            "User signed in with SSO": "success", "User login blocked": "failure",
            "User logged out": None, "API token created": None, "User updated profile": None,
        }
        for action, want in cases.items():
            with self.subTest(action=action):
                self.assertEqual(classify({"action": action}), want)

    def test_custom_terms(self):
        self.assertEqual(classify({"action": "Anmeldung"}, login_terms=("anmeldung",)), "success")
        self.assertIsNone(classify({"action": "Login"}, login_terms=("anmeldung",)))


class EndToEndTests(unittest.TestCase):
    def test_login_events_csv_and_paging(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            r = run_export(m, tmp, preset="users", roles=False, login_events=True)
            self.assertEqual(r.code, 0, r.stderr)
            note = r.snapshot()["sections"]["login_events"]
            self.assertEqual(note["status"], "ok")
            calls = m.calls_to(AUDITS_PATH)
            self.assertTrue(all(c["tenant"] for c in calls), "every audits call carries the tenant header")
            big = next(t["tenantId"] for t in ds.tenants if t["name"] == "Acme Big")
            self.assertEqual([c["query"]["offset"][0] for c in calls if c["tenant"] == big], ["0", "200"])
            with open(r.run_dir / "login_events.csv", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), note["success"] + note["failure"])
            self.assertGreater(note["failure"], 0)
            self.assertEqual({row["result"] for row in rows}, {"success", "failure"})
            sample = rows[0]
            self.assertTrue(sample["user_email"].endswith("@example.com"))
            by_email = {u["email"]: u["id"] for u in ds.users}
            self.assertTrue(all(row["user_id"] == by_email[row["user_email"]] for row in rows),
                            "user_id is the user logged into, even for impersonated logins")
            self.assertIn(IMPERSONATED_LOGIN_ACTION, {row["action"] for row in rows})
            self.assertTrue(sample["account_name"].startswith("Acme"))
            self.assertTrue(sample["ip"].startswith("203.0.113."))
            stamps = [row["timestamp"] for row in rows]
            self.assertEqual(stamps, sorted(stamps))

    def test_second_run_reads_only_since_the_previous_success(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            first = run_export(m, tmp, preset="users", roles=False, login_events=True)
            m.requests.clear()
            second = run_export(m, tmp, preset="users", roles=False, login_events=True)
            sent = {c["query"]["created_from"][0] for c in m.calls_to(AUDITS_PATH)}
            self.assertEqual(sent, {first.summary()["startedAt"].replace("+00:00", "Z")})
            self.assertFalse(second.snapshot()["sections"]["login_events"]["capped"])

    def test_a_run_without_login_events_leaves_no_gap(self):
        with MockFrontegg(make_dataset(tenants=4, big_tenant_users=5)) as m, temp_dir() as tmp:
            first = run_export(m, tmp, preset="users", roles=False, login_events=True)
            run_export(m, tmp, preset="users", roles=False)                  # no login events this time
            m.requests.clear()
            run_export(m, tmp, preset="users", roles=False, login_events=True)
            sent = {c["query"]["created_from"][0] for c in m.calls_to(AUDITS_PATH)}
            self.assertEqual(sent, {first.snapshot()["sections"]["login_events"]["to"]})

    def test_a_partial_read_is_read_again(self):
        ds = make_dataset(tenants=4, big_tenant_users=5)
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            first = run_export(m, tmp, preset="users", roles=False, login_events=True)
            m.faults.audits_mode = "error"
            self.assertEqual(run_export(m, tmp, preset="users", roles=False, login_events=True).code, 2)
            m.faults.audits_mode = "ok"
            m.requests.clear()
            run_export(m, tmp, preset="users", roles=False, login_events=True)
            sent = {c["query"]["created_from"][0] for c in m.calls_to(AUDITS_PATH)}
            self.assertEqual(sent, {first.snapshot()["sections"]["login_events"]["to"]})

    def test_unavailable_does_not_fail_the_run(self):
        for mode in ("forbidden", "not_found"):
            with self.subTest(mode=mode):
                with MockFrontegg(make_dataset(), Faults(audits_mode=mode)) as m, temp_dir() as tmp:
                    r = run_export(m, tmp, preset="users", roles=False, login_events=True)
                    self.assertEqual(r.code, 0)
                    note = r.snapshot()["sections"]["login_events"]
                    self.assertEqual(note["status"], "unavailable")
                    self.assertIn("plan", note["reason"])
                    self.assertEqual(len(m.calls_to(AUDITS_PATH)), 1, "stops after the first refusal")
                    self.assertFalse((r.run_dir / "login_events.csv").exists())
                    self.assertIn("Login events are unavailable", r.stdout)

    def test_server_errors_make_the_run_partial(self):
        with MockFrontegg(make_dataset(tenants=4, big_tenant_users=5), Faults(audits_mode="error")) as m, \
                temp_dir() as tmp:
            r = run_export(m, tmp, preset="users", roles=False, login_events=True)
            self.assertEqual(r.code, 2)
            self.assertEqual(r.snapshot()["sections"]["login_events"]["status"], "partial")
            self.assertTrue(all(f["section"] == "login_events" for f in r.snapshot()["failures"]))

    def test_off_by_default(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            run_export(m, tmp)
            self.assertEqual(m.calls_to(AUDITS_PATH), [])

    def test_estimate_includes_the_per_account_calls(self):
        est = estimate(resolve("users", roles=False, login_events=True), 4.0,
                       Counts(users=5000, accounts=3000))
        self.assertEqual(est.per_step["login_events"], 3000)


if __name__ == "__main__":
    unittest.main()
