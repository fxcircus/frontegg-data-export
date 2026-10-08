"""The command line, run as a real subprocess against the mock."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.helpers import cli, temp_dir
from tests.mock_frontegg import Faults, MockFrontegg, make_dataset

FAST = ["--rate", "16"]


def small():
    return make_dataset(tenants=5, big_tenant_users=12)


class CliTests(unittest.TestCase):
    def test_usage_error_is_64_not_2(self):
        with temp_dir() as tmp:
            r = cli(["run", "--preset", "everything"], None, tmp)
            self.assertEqual(r.returncode, 64)
            r = cli(["frobnicate"], None, tmp)
            self.assertEqual(r.returncode, 64)

    def test_missing_credentials_explains_where_to_find_them(self):
        with temp_dir() as tmp:
            r = cli(["run", "--out", tmp], None, tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Keys & domains", r.stderr)
            self.assertIn("FRONTEGG_CLIENT_ID", r.stderr)

    def test_run_jsonl_then_runs_estimate_and_diff(self):
        ds = small()
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            r = cli(["run", "--preset", "users", "--no-roles", "--progress", "jsonl", "--out", tmp, *FAST], m, tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            events = [json.loads(line) for line in r.stdout.splitlines()]
            self.assertEqual(events[0]["event"], "run_started")
            self.assertEqual(events[-1]["event"], "run_finished")
            self.assertEqual(events[-1]["status"], "succeeded")
            self.assertEqual(events[-1]["exitCode"], 0)
            run_dir = Path(events[-1]["outputDir"])
            self.assertTrue((run_dir / "users.csv").exists())

            r2 = cli(["run", "--preset", "users", "--no-roles", "--quiet", "--out", tmp, *FAST], m, tmp)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(r2.stdout.strip(), "succeeded: Export finished")

            runs = cli(["runs", "--out", tmp], None, tmp)
            self.assertEqual(runs.returncode, 0)
            self.assertIn("(baseline)", runs.stdout)
            self.assertEqual(len(json.loads(cli(["runs", "--json", "--out", tmp], None, tmp).stdout)["runs"]), 2)

            est = json.loads(cli(["estimate", "--preset", "users", "--no-roles", "--json", "--out", tmp],
                                 None, tmp).stdout)
            self.assertEqual(est["basis"], "previous run")
            self.assertEqual(est["perStep"]["users"], 1)

            d = cli(["diff", "--out", tmp, "--csv", str(Path(tmp) / "c.csv")], None, tmp)
            self.assertEqual(d.returncode, 0, d.stderr)
            self.assertIn("Users: 0 added, 0 removed", d.stdout)
            self.assertTrue((Path(tmp) / "c.csv").exists())

    def test_partial_run_exits_2(self):
        ds = small()
        bad = ds.tenants[2]["tenantId"]
        # 400 is not retried, so the subprocess doesn't sit through real backoff
        with MockFrontegg(ds, Faults(failing_role_tenants={bad}, failing_role_status=400)) as m, temp_dir() as tmp:
            r = cli(["run", "--preset", "users", "--out", tmp, *FAST], m, tmp)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("some data is missing", r.stdout)

    def test_failed_run_exits_1(self):
        with MockFrontegg(small()) as m, temp_dir() as tmp:
            r = cli(["run", "--out", tmp, *FAST], m, tmp, secret="wrong-secret")
            self.assertEqual(r.returncode, 1)
            self.assertIn("rejected", r.stderr)

    def test_test_connection(self):
        with MockFrontegg(small()) as m, temp_dir() as tmp:
            ok = cli(["test-connection", "--json"], m, tmp)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(json.loads(ok.stdout)["users"], len(m.ds.users))
            bad = cli(["test-connection"], m, tmp, secret="nope")
            self.assertEqual(bad.returncode, 1)
            self.assertIn("Keys & domains", bad.stderr)

    def test_estimate_probe_with_no_history(self):
        with MockFrontegg(small()) as m, temp_dir() as tmp:
            r = cli(["estimate", "--probe", "--json", "--out", tmp], m, tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            est = json.loads(r.stdout)
            self.assertEqual(est["basis"], "record counts")
            self.assertGreater(est["calls"], 3)
            self.assertEqual(len(m.requests), 1 + 3)     # token + 3 cheap reads


if __name__ == "__main__":
    unittest.main()
