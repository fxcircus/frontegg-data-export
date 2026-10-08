"""The output folder: one subfolder per run, a history index, the diff
baseline, retention, atomic writes, and a lock so two exports (for example
the app and a scheduled run) never write at the same time.

    <output>/
      history.json            run index + baselineRunId
      .lock                   present while an export runs
      runs/2026-10-08T120000Z/
        summary.json  snapshot.json  normalized.json  run.log  *.csv
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator

RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z(?:-\d{1,3})?$")
DEFAULT_KEEP = 30
STALE_LOCK_SECONDS = 26 * 3600     # longer than any run could take (token TTL is 24 h)


class StoreError(Exception):
    pass


class Busy(StoreError):
    """Another export holds the lock."""


# --------------------------------------------------------------------------- #
# Atomic writes: temp file in the same folder, fsync, then rename over.
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def atomic_open(path: Path, mode: str = "w", encoding: str | None = "utf-8",
                newline: str | None = None) -> Iterator[IO[Any]]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    binary = "b" in mode
    try:
        with os.fdopen(fd, mode, encoding=None if binary else encoding,
                       newline=None if binary else newline) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    with atomic_open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def atomic_write_json(path: Path, data: Any, indent: int | None = 2) -> None:
    with atomic_open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
        f.write("\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


# --------------------------------------------------------------------------- #
# Process liveness, for stale-lock detection.
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TERMINATE the process on Windows; ask instead.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5             # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                         # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def new_run_id(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.runs_dir = self.root / "runs"
        self.history_path = self.root / "history.json"
        self.lock_path = self.root / ".lock"

    # ---- lock -------------------------------------------------------------
    def lock_info(self) -> dict | None:
        """The current lock holder, or None if nothing is running."""
        info = read_json(self.lock_path)
        if not info:
            return None
        pid = int(info.get("pid") or 0)
        age = time.time() - float(info.get("since") or 0)
        same_host = info.get("host") == socket.gethostname()
        if age > STALE_LOCK_SECONDS or (same_host and not pid_alive(pid)):
            return None
        return info

    @contextlib.contextmanager
    def lock(self, purpose: str = "export") -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "since": time.time(),
                              "purpose": purpose}).encode()
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = self.lock_info()
                if holder:
                    since = datetime.fromtimestamp(float(holder["since"]), tz=timezone.utc)
                    raise Busy(f"Another export is already running (started "
                               f"{since.isoformat(timespec='seconds')}, process {holder.get('pid')}). "
                               "Wait for it to finish, then try again.") from None
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.lock_path)          # stale: its process is gone
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            break
        else:
            raise Busy("Couldn't take the export lock. Try again in a moment.")
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.lock_path)

    # ---- runs -------------------------------------------------------------
    def create_run_dir(self, started_at: datetime) -> tuple[str, Path]:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        base = new_run_id(started_at)
        for n in range(0, 1000):
            run_id = base if n == 0 else f"{base}-{n}"
            path = self.runs_dir / run_id
            try:
                path.mkdir()
            except FileExistsError:
                continue
            return run_id, path
        raise StoreError("Couldn't create a folder for this run.")

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id or ""):
            raise StoreError(f"Not a run ID: {run_id!r}")
        return self.runs_dir / run_id

    def list_run_ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.name for p in self.runs_dir.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name))

    # ---- history ----------------------------------------------------------
    def load_history(self) -> dict:
        h = read_json(self.history_path, default=None) or {}
        h.setdefault("runs", [])
        h.setdefault("baselineRunId", None)
        return h

    def save_history(self, history: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.history_path, history)

    def record_run(self, entry: dict, make_baseline: bool) -> dict:
        h = self.load_history()
        h["runs"] = [r for r in h["runs"] if r.get("runId") != entry["runId"]] + [entry]
        h["runs"].sort(key=lambda r: r["runId"])
        if make_baseline:
            h["baselineRunId"] = entry["runId"]
        self.save_history(h)
        return h

    def set_baseline(self, run_id: str) -> None:
        h = self.load_history()
        entry = next((r for r in h["runs"] if r.get("runId") == run_id), None)
        if not entry:
            raise StoreError(f"No run {run_id} in the history.")
        if entry.get("status") == "failed":
            raise StoreError("A failed run can't be the comparison baseline.")
        h["baselineRunId"] = run_id
        self.save_history(h)

    def baseline_id(self) -> str | None:
        bid = self.load_history().get("baselineRunId")
        return bid if bid and (self.runs_dir / bid).is_dir() else None

    def resolve_run(self, ref: str) -> Path:
        """A run ID, a run folder path, or one of: latest, previous, baseline."""
        ids = self.list_run_ids()
        if ref == "latest":
            ok = [r["runId"] for r in self.load_history()["runs"] if r.get("status") != "failed"]
            ok = [i for i in ok if i in ids]
            if not ok:
                raise StoreError("There are no completed runs yet.")
            return self.runs_dir / ok[-1]
        if ref == "previous":
            ok = [r["runId"] for r in self.load_history()["runs"] if r.get("status") != "failed"]
            ok = [i for i in ok if i in ids]
            if len(ok) < 2:
                raise StoreError("There's no previous completed run to compare with.")
            return self.runs_dir / ok[-2]
        if ref == "baseline":
            bid = self.baseline_id()
            if not bid:
                raise StoreError("There's no baseline run yet.")
            return self.runs_dir / bid
        if RUN_ID_RE.match(ref) and ref in ids:
            return self.runs_dir / ref
        p = Path(ref).expanduser()
        if p.is_dir() and (p / "summary.json").exists():
            return p
        raise StoreError(f"Couldn't find a run called {ref!r}.")

    # ---- retention --------------------------------------------------------
    def apply_retention(self, keep: int, protect: set[str] | None = None) -> tuple[list[str], list[str]]:
        """Keep the newest `keep` runs. Never delete the baseline or anything in
        `protect`. Only folders whose names look like run IDs are ever touched.
        Returns (deleted IDs, warnings)."""
        keep = max(1, int(keep))
        protect = set(protect or ())
        bid = self.baseline_id()
        if bid:
            protect.add(bid)
        ids = self.list_run_ids()
        doomed = [i for i in ids[:-keep] if i not in protect] if len(ids) > keep else []
        deleted, warnings = [], []
        for run_id in doomed:
            path = self.runs_dir / run_id
            try:
                shutil.rmtree(path)
                deleted.append(run_id)
            except OSError as e:
                # Typically a CSV that's open in Excel on Windows. Try again next run.
                warnings.append(f"Couldn't delete old run {run_id} ({e.strerror or e}). "
                                "Close any of its files that are open; it will be retried next time.")
        if deleted:
            h = self.load_history()
            h["runs"] = [r for r in h["runs"] if r.get("runId") not in deleted]
            self.save_history(h)
        return deleted, warnings
