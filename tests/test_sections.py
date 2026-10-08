"""Sections, presets, the steps they imply, and the estimate."""

from __future__ import annotations

import io
import json
import unittest

from frontegg_data_export.progress import Reporter
from frontegg_data_export.sections import Counts, estimate, format_duration, resolve
from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import MockFrontegg, make_dataset


class ResolveTests(unittest.TestCase):
    def test_users_only_preset(self):
        sel = resolve("users")
        self.assertEqual(sel.sections, ("users", "roles"))
        # account list is read for names; no plans, no hierarchy, no catalog
        self.assertEqual(sel.steps, ("auth", "role_catalog", "accounts", "users", "roles", "write"))

    def test_users_only_without_roles(self):
        sel = resolve("users", roles=False)
        self.assertEqual(sel.sections, ("users",))
        self.assertNotIn("roles", sel.steps)
        self.assertNotIn("role_catalog", sel.steps)

    def test_standard_is_the_default(self):
        sel = resolve()
        self.assertEqual(sel.preset, "standard")
        self.assertEqual(sel.label, "Users, accounts and plans")
        self.assertEqual(sel.sections, ("users", "roles", "accounts", "hierarchy", "plans"))
        self.assertNotIn("catalog", sel.steps)

    def test_full_backup_has_everything_the_base_exported(self):
        sel = resolve("full")
        for step in ("role_catalog", "catalog", "plan_catalog", "accounts", "hierarchy", "users",
                     "roles", "entitlements"):
            self.assertIn(step, sel.steps)
        self.assertNotIn("login_events", sel.steps)

    def test_login_events_toggle(self):
        sel = resolve("users", roles=False, login_events=True)
        self.assertIn("login_events", sel.sections)
        self.assertIn("accounts", sel.steps)
        self.assertIn("users", sel.steps)

    def test_custom_sections(self):
        sel = resolve(sections=["accounts"])
        self.assertEqual(sel.preset, "custom")
        self.assertEqual(sel.steps, ("auth", "accounts", "write"))

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            resolve("everything")
        with self.assertRaises(ValueError):
            resolve(sections=["users", "groups"])
        with self.assertRaises(ValueError):
            resolve(sections=["roles"], roles=False)


class EstimateTests(unittest.TestCase):
    MEDIUM = Counts(users=5000, accounts=5000, entitlements=5000, resellers=20, plans=15, features=20)

    def test_users_only_without_roles_is_about_one_call_per_200_users(self):
        est = estimate(resolve("users", roles=False), 4.0, self.MEDIUM)
        self.assertEqual(est.per_step["users"], 25)
        self.assertEqual(est.per_step["accounts"], 25)
        self.assertEqual(est.calls, 1 + 25 + 25)
        self.assertEqual(est.basis, "record counts")
        # the users listing ceiling (50/min) dominates: ~24 x 1.2 s plus accounts at 30/min
        self.assertGreater(est.seconds, 60)

    def test_role_lookups_dominate(self):
        est = estimate(resolve("users"), 4.0, self.MEDIUM)
        self.assertEqual(est.per_step["roles"], 5000 + 50)
        self.assertGreater(est.calls, 5000)

    def test_standard_preset(self):
        est = estimate(resolve(), 4.0, self.MEDIUM)
        self.assertEqual(est.per_step["entitlements"], 500)
        self.assertEqual(est.per_step["hierarchy"], 20)
        self.assertAlmostEqual(est.seconds / 60, est.calls / 3 / 60, delta=3)

    def test_previous_run_wins_and_pace_is_the_slower_one(self):
        prev = {"users": {"calls": 30}, "roles": {"calls": 2900}}
        est = estimate(resolve("users", roles=True), 12.0, self.MEDIUM, previous_steps=prev, previous_pace=2.5)
        self.assertEqual(est.basis, "previous run")
        self.assertEqual(est.per_step["roles"], 2900)
        self.assertGreaterEqual(est.seconds, 2900 / 2.5)

    def test_unknown_counts(self):
        est = estimate(resolve(), 4.0, None)
        self.assertEqual(est.basis, "unknown")
        self.assertTrue(est.notes)

    def test_format_duration(self):
        self.assertEqual(format_duration(0), "a few seconds")
        self.assertEqual(format_duration(45), "under a minute")
        self.assertEqual(format_duration(20 * 60), "about 20 minutes")
        self.assertEqual(format_duration(95 * 60), "about 1 h 35 min")


class PresetRunTests(unittest.TestCase):
    def test_users_only_reads_no_plans_or_hierarchy(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            r = run_export(m, tmp, preset="users")
            self.assertEqual(r.code, 0)
            paths = {req["path"] for req in m.requests}
            self.assertNotIn("/entitlements/resources/entitlements/v2", paths)
            self.assertNotIn("/tenants/resources/hierarchy/v1/tree", paths)
            self.assertIn("/identity/resources/users/v3/roles", paths)
            self.assertEqual(set(r.snapshot()["counts"]), {"roles", "tenants", "users", "userRoleAssignments"})

    def test_roles_toggle_off_skips_the_expensive_step(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            self.assertEqual(run_export(m, tmp, preset="standard", roles=False).code, 0)
            self.assertEqual(m.calls_to("/identity/resources/users/v3/roles"), [])

    def test_step_stats_are_recorded_for_the_next_estimate(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            steps = run_export(m, tmp, preset="full").snapshot()["exportRun"]["steps"]
            self.assertEqual(steps["users"]["calls"], 2)
            self.assertGreater(steps["roles"]["calls"], 10)


class JsonlProgressTests(unittest.TestCase):
    def test_every_line_is_json_with_an_event(self):
        out = io.StringIO()
        r = Reporter("jsonl", stream=out, err_stream=io.StringIO())
        r.run_started("t", "s", preset="users")
        r.step("users", "Read users", 1, 3)
        r.progress("users", 10, 20, "users", force=True)
        r.warn("careful", step="roles", tenantId="t1", traceId="abc")
        r.run_finished("partial", "done", "details", exitCode=2)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([e["event"] for e in events],
                         ["run_started", "step_started", "progress", "warning", "run_finished"])
        self.assertEqual(events[3]["traceId"], "abc")
        self.assertEqual(events[-1]["exitCode"], 2)
        self.assertTrue(all("elapsed" in e and "apiCalls" in e for e in events))

    def test_quiet_prints_only_the_result(self):
        out = io.StringIO()
        r = Reporter("quiet", stream=out, err_stream=io.StringIO())
        r.run_started("t", "s")
        r.step("users", "Read users", 1, 3)
        r.info("hello")
        r.run_finished("succeeded", "Export finished", "details")
        self.assertEqual(out.getvalue(), "succeeded: Export finished\n")


if __name__ == "__main__":
    unittest.main()
