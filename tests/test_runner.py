"""Run status and exit codes, end to end against the mock."""

from __future__ import annotations

import unittest

from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import Faults, MockFrontegg, make_dataset


def small_tenants(ds) -> list[str]:
    return [t["tenantId"] for t in ds.tenants if t["name"].startswith("Acme 0")]


class RunStatusTests(unittest.TestCase):
    def test_clean_run_succeeds_with_exit_0(self):
        ds = make_dataset()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertEqual(r.code, 0)
            snap = r.snapshot()
            self.assertEqual(snap["exportRun"]["status"], "succeeded")
            self.assertEqual(snap["exportRun"]["failures"], [])
            self.assertEqual(snap["counts"]["userRoleAssignments"], len(ds.role_assignments))

    def test_failed_role_lookup_is_partial_with_exit_2(self):
        ds = make_dataset()
        bad = small_tenants(ds)[0]
        with MockFrontegg(ds, Faults(failing_role_tenants={bad})) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertEqual(r.code, 2)
            run = r.snapshot()["exportRun"]
            self.assertEqual(run["status"], "partial")
            self.assertEqual(run["failedRoleLookupTenants"], [bad])
            [f] = run["failures"]
            self.assertEqual((f["section"], f["tenantId"], f["httpStatus"]), ("roles", bad, 500))
            self.assertTrue(f["traceId"])
            self.assertIn("support", f["hint"])
            # every other tenant's roles are still there
            expected = sum(1 for (_, tid) in ds.role_assignments if tid != bad)
            self.assertEqual(r.snapshot()["counts"]["userRoleAssignments"], expected)
            self.assertIn("partial", r.stdout)

    def test_failed_hierarchy_tree_is_partial_not_fatal(self):
        ds = make_dataset()
        root = next(t["tenantId"] for t in ds.tenants if t["isReseller"])
        with MockFrontegg(ds, Faults(failing_tree_tenants={root})) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertEqual(r.code, 2)
            run = r.snapshot()["exportRun"]
            self.assertEqual(run["failedHierarchyRoots"], [root])
            self.assertEqual(run["failures"][0]["section"], "hierarchy")
            self.assertIn("loop", run["failures"][0]["hint"])
            self.assertEqual(r.snapshot()["counts"]["hierarchyTrees"], 1)

    def test_core_list_failure_fails_with_exit_1_and_writes_nothing(self):
        ds = make_dataset()
        faults = Faults(failing_list_paths={"/identity/resources/users/v3"})
        with MockFrontegg(ds, faults) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertEqual(r.code, 1)
            self.assertEqual(r.json_files(), [])
            self.assertIn("users", r.stderr)
            self.assertIn("nothing was written", r.stdout)

    def test_bad_credentials_fail_with_exit_1(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            r = run_export(m, tmp, secret="wrong")
            self.assertEqual(r.code, 1)
            self.assertIn("Keys & domains", r.stderr)
            self.assertEqual(r.json_files(), [])

    def test_transient_errors_still_succeed(self):
        ds = make_dataset()
        with MockFrontegg(ds, Faults(error_5xx_every=4, rate_limit_every=7)) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertEqual(r.code, 0)
            self.assertGreater(r.snapshot()["exportRun"]["retries"], 0)

    def test_414_halves_the_role_batch(self):
        ds = make_dataset()
        with MockFrontegg(ds, Faults(max_url_length=2500)) as m, temp_dir() as tmp:
            r = run_export(m, tmp)
            self.assertEqual(r.code, 0)
            self.assertEqual(r.snapshot()["counts"]["userRoleAssignments"], len(ds.role_assignments))
            sizes = {len(c["query"]["ids"][0].split(",")) for c in m.calls_to("/identity/resources/users/v3/roles")
                     if c["target"] and len(c["target"]) <= 2500}
            self.assertIn(50, sizes)


if __name__ == "__main__":
    unittest.main()
