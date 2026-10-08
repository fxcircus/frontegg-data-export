"""Output folder: atomic writes, run folders, lock, history, baseline, retention, logs."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from frontegg_data_export import logs
from frontegg_data_export.logs import REDACTED, Redactor, RunLog, app_logger
from frontegg_data_export.store import Busy, Store, StoreError, atomic_open, atomic_write_json
from tests.helpers import run_export, temp_dir
from tests.mock_frontegg import Faults, MockFrontegg, make_dataset

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def add_runs(store: Store, statuses: list[str]) -> list[str]:
    ids = []
    for i, status in enumerate(statuses):
        run_id, path = store.create_run_dir(T0.replace(minute=i))
        (path / "summary.json").write_text("{}")
        store.record_run({"runId": run_id, "status": status}, make_baseline=status == "succeeded")
        ids.append(run_id)
    return ids


class AtomicWriteTests(unittest.TestCase):
    def test_replaces_on_success(self):
        with temp_dir() as tmp:
            target = Path(tmp) / "a.json"
            target.write_text("old")
            atomic_write_json(target, {"new": True})
            self.assertEqual(json.loads(target.read_text()), {"new": True})
            self.assertEqual(os.listdir(tmp), ["a.json"])

    def test_leaves_target_untouched_on_failure(self):
        with temp_dir() as tmp:
            target = Path(tmp) / "a.csv"
            target.write_text("old")
            with self.assertRaises(RuntimeError):
                with atomic_open(target) as f:
                    f.write("half")
                    raise RuntimeError("boom")
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(os.listdir(tmp), ["a.csv"])


class RunDirTests(unittest.TestCase):
    def test_ids_are_timestamps_and_unique(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            a, _ = s.create_run_dir(T0)
            b, _ = s.create_run_dir(T0)
            self.assertEqual(a, "2026-10-08T120000Z")
            self.assertEqual(b, "2026-10-08T120000Z-1")
            self.assertEqual(s.list_run_ids(), [a, b])

    def test_run_dir_rejects_paths(self):
        s = Store(Path("/nowhere"))
        for bad in ("../etc", "2026-10-08T120000Z/../../x", "", "latest"):
            with self.subTest(bad=bad), self.assertRaises(StoreError):
                s.run_dir(bad)


class LockTests(unittest.TestCase):
    def test_second_lock_is_busy(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            with s.lock():
                self.assertIsNotNone(s.lock_info())
                with self.assertRaises(Busy) as cm:
                    with s.lock():
                        pass
                self.assertIn("already running", str(cm.exception))
            self.assertFalse(s.lock_path.exists())
            self.assertIsNone(s.lock_info())

    def test_stale_lock_from_a_dead_process_is_taken_over(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            s.lock_path.write_text(json.dumps({"pid": 2_000_000_000, "host": socket.gethostname(),
                                               "since": time.time()}))
            with s.lock():
                self.assertEqual(s.lock_info()["pid"], os.getpid())

    def test_very_old_lock_is_stale(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            s.lock_path.write_text(json.dumps({"pid": os.getpid(), "host": "elsewhere",
                                               "since": time.time() - 30 * 3600}))
            self.assertIsNone(s.lock_info())


class HistoryTests(unittest.TestCase):
    def test_baseline_follows_succeeded_runs_only(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            ids = add_runs(s, ["succeeded", "partial", "failed"])
            self.assertEqual(s.baseline_id(), ids[0])
            self.assertEqual(s.resolve_run("latest").name, ids[1])
            self.assertEqual(s.resolve_run("previous").name, ids[0])
            self.assertEqual(s.resolve_run("baseline").name, ids[0])
            s.set_baseline(ids[1])
            self.assertEqual(s.baseline_id(), ids[1])
            with self.assertRaises(StoreError):
                s.set_baseline(ids[2])

    def test_resolve_by_path_and_unknown(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            [rid] = add_runs(s, ["succeeded"])
            self.assertEqual(s.resolve_run(str(s.runs_dir / rid)), s.runs_dir / rid)
            with self.assertRaises(StoreError):
                s.resolve_run("2020-01-01T000000Z")


class RetentionTests(unittest.TestCase):
    def test_keeps_newest_and_the_baseline(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            ids = add_runs(s, ["succeeded", "partial", "partial", "partial", "partial"])
            (s.runs_dir / "my notes").mkdir()                 # not a run: never touched
            (s.runs_dir / "2026-10-08T999999Z-extra").mkdir() # not a run ID either
            deleted, warnings = s.apply_retention(2)
            self.assertEqual(deleted, ids[1:3])
            self.assertEqual(warnings, [])
            self.assertEqual(s.list_run_ids(), [ids[0], ids[3], ids[4]])  # baseline survives
            self.assertTrue((s.runs_dir / "my notes").is_dir())
            self.assertTrue((s.runs_dir / "2026-10-08T999999Z-extra").is_dir())
            self.assertEqual([r["runId"] for r in s.load_history()["runs"]], [ids[0], ids[3], ids[4]])

    def test_undeletable_run_is_a_warning(self):
        with temp_dir() as tmp:
            s = Store(Path(tmp))
            ids = add_runs(s, ["partial", "partial", "partial"])
            with mock.patch("shutil.rmtree", side_effect=PermissionError(13, "in use")):
                deleted, warnings = s.apply_retention(1)
            self.assertEqual(deleted, [])
            self.assertEqual(len(warnings), 2)
            self.assertEqual(s.list_run_ids(), ids)


class EndToEndStoreTests(unittest.TestCase):
    def test_partial_run_is_not_the_baseline_unless_asked(self):
        ds = make_dataset()
        bad = next(t["tenantId"] for t in ds.tenants if t["name"] == "Acme 02")
        with MockFrontegg(ds) as m, temp_dir() as tmp:
            first = run_export(m, tmp)
            self.assertEqual(first.code, 0)
            m.faults.failing_role_tenants = {bad}
            second = run_export(m, tmp)
            self.assertEqual(second.code, 2)
            self.assertEqual(second.history()["baselineRunId"], first.run_dirs()[0].name)
            self.assertIn("won't be used as the comparison baseline", second.stdout)
            third = run_export(m, tmp, use_as_baseline=True)
            self.assertEqual(third.code, 2)
            self.assertEqual(third.history()["baselineRunId"], third.run_dir.name)

    def test_busy_when_another_export_holds_the_lock(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            with Store(Path(tmp)).lock():
                r = run_export(m, tmp)
            self.assertEqual(r.code, 1)
            self.assertIn("already running", r.stderr)
            self.assertEqual(r.run_dirs(), [])
            self.assertEqual(m.requests, [])

    def test_retention_applies_after_each_run(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            for _ in range(3):
                r = run_export(m, tmp, preset="users", roles=False, keep=2)
            self.assertEqual(len(r.run_dirs()), 2)

    def test_run_log_has_trace_ids(self):
        with MockFrontegg(make_dataset()) as m, temp_dir() as tmp:
            r = run_export(m, tmp, preset="users", roles=False)
            text = (r.run_dir / "run.log").read_text()
            self.assertIn("trace=", text)
            self.assertIn("GET /identity/resources/users/v3", text)


class LogTests(unittest.TestCase):
    def test_redactor(self):
        r = Redactor()
        r.add("FAKE-SECRET-1234")
        r.add("abc")                      # too short to be meaningful; ignored
        line = r("secret=FAKE-SECRET-1234 header Authorization: Bearer eyJhbGciOi.x.y abc")
        self.assertNotIn("FAKE-SECRET-1234", line)
        self.assertNotIn("eyJhbGciOi", line)
        self.assertEqual(line.count(REDACTED), 2)
        self.assertIn("abc", line)

    def test_run_log_redacts(self):
        with temp_dir() as tmp:
            r = Redactor()
            r.add("FAKE-SECRET-5678")
            log = RunLog(Path(tmp) / "run.log", r)
            log("token FAKE-SECRET-5678", "WARN")
            log.close()
            text = (Path(tmp) / "run.log").read_text()
            self.assertIn("[WARN] token [redacted]", text)

    def test_app_log_rotates(self):
        with temp_dir() as tmp, mock.patch.object(logs, "APP_LOG_MAX_BYTES", 2000):
            logger = app_logger(Path(tmp), name="fde-rotation-test")
            try:
                for i in range(200):
                    logger.info("line %d %s", i, "x" * 50)
                files = sorted(os.listdir(tmp))
                self.assertIn("app.log", files)
                self.assertIn("app.log.1", files)
                self.assertLessEqual(len(files), 4)
            finally:
                for h in list(logger.handlers):
                    h.close()
                    logger.removeHandler(h)
                logging.getLogger("fde-rotation-test").handlers.clear()


if __name__ == "__main__":
    unittest.main()
