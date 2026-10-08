"""The API key and the access tokens never reach a log, an output file, the
console, or a progress event — in successful, partial and failed runs.

(Phase 2 extends this to every response from the local app's API.)
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.helpers import cli, temp_dir
from tests.mock_frontegg import CLIENT_ID, Faults, MockFrontegg, make_dataset

SECRET = "FAKE-SECRET-d1e2a3d4"
TOKEN_MARKER = "mock-token-"          # every token the mock issues starts with this


def all_files(root: Path) -> list[Path]:
    return [p for p in Path(root).rglob("*") if p.is_file()]


class SecretLeakTests(unittest.TestCase):
    def assertClean(self, text: str, where: str) -> None:
        self.assertNotIn(SECRET, text, f"API key leaked into {where}")
        self.assertNotIn(TOKEN_MARKER, text, f"access token leaked into {where}")

    def assertTreeClean(self, root: Path) -> int:
        files = all_files(root)
        for f in files:
            data = f.read_bytes()
            self.assertNotIn(SECRET.encode(), data, f"API key leaked into {f}")
            self.assertNotIn(TOKEN_MARKER.encode(), data, f"access token leaked into {f}")
        return len(files)

    def test_no_leaks_anywhere(self):
        ds = make_dataset(tenants=6, big_tenant_users=20)
        bad = ds.tenants[3]["tenantId"]
        with MockFrontegg(ds, Faults(failing_role_tenants={bad}, failing_role_status=400,
                                     rate_limit_every=9, token_uses=15), secret=SECRET) as m, temp_dir() as tmp:
            outputs = []
            # partial run, all sections, with JSON Lines progress, retries and a mid-run re-auth
            r = cli(["run", "--preset", "full", "--login-events", "--progress", "jsonl", "--out", tmp,
                     "--rate", "16"], m, tmp, secret=SECRET)
            self.assertEqual(r.returncode, 2, r.stderr)
            for line in r.stdout.splitlines():
                event = json.loads(line)
                self.assertClean(json.dumps(event), f"progress event {event.get('event')}")
            outputs.append(("run --progress jsonl", r))
            # succeeded run, console output
            m.faults.failing_role_tenants = set()
            r = cli(["run", "--preset", "full", "--out", tmp, "--rate", "16"], m, tmp, secret=SECRET)
            self.assertEqual(r.returncode, 0, r.stderr)
            outputs.append(("run (console)", r))
            outputs.append(("test-connection", cli(["test-connection", "--json"], m, tmp, secret=SECRET)))
            outputs.append(("estimate", cli(["estimate", "--out", tmp], m, tmp, secret=SECRET)))
            outputs.append(("diff", cli(["diff", "--json", "--out", tmp], m, tmp, secret=SECRET)))
            outputs.append(("runs", cli(["runs", "--json", "--out", tmp], m, tmp, secret=SECRET)))
            self.assertGreater(len(m.calls_to("/auth/vendor/")), 2, "the scenario should re-authenticate")
            for name, proc in outputs:
                self.assertClean(proc.stdout, f"{name} stdout")
                self.assertClean(proc.stderr, f"{name} stderr")
            n = self.assertTreeClean(Path(tmp))
            self.assertGreater(n, 15)

    def test_rejected_key_is_not_echoed(self):
        with MockFrontegg(make_dataset(tenants=3, big_tenant_users=3), secret="the-right-one") as m, \
                temp_dir() as tmp:
            for args in (["run", "--out", tmp], ["test-connection"], ["estimate", "--probe", "--out", tmp]):
                r = cli(args, m, tmp, secret=SECRET)
                self.assertNotEqual(r.returncode, 0)
                self.assertClean(r.stdout + r.stderr, " ".join(args[:1]))
            self.assertTreeClean(Path(tmp))
            # the key did reach the mock, in the one request that's meant to carry it
            bodies = [json.loads(c["body"]) for c in m.calls_to("/auth/vendor/")]
            self.assertTrue(all(b == {"clientId": CLIENT_ID, "secret": SECRET} for b in bodies))


if __name__ == "__main__":
    unittest.main()
