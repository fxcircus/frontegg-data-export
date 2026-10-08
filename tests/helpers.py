"""Shared test helpers: run an export against the mock in a temp folder."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from frontegg_data_export import runner
from frontegg_data_export.client import FronteggClient
from tests.mock_frontegg import CLIENT_ID, CLIENT_SECRET, MockFrontegg


def fast_client(*args, **kwargs) -> FronteggClient:
    kwargs.setdefault("sleep", lambda s: None)
    return FronteggClient(*args, **kwargs)


class ExportResult:
    def __init__(self, code: int, out_dir: Path, stdout: str, stderr: str):
        self.code = code
        self.out_dir = out_dir
        self.stdout = stdout
        self.stderr = stderr

    def run_dirs(self) -> list[Path]:
        runs = self.out_dir / "runs"
        return sorted(p for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else []

    @property
    def run_dir(self) -> Path:
        return self.run_dirs()[-1]

    def json_files(self) -> list[Path]:
        return sorted(self.out_dir.glob("runs/*/snapshot.json"))

    def snapshot(self) -> dict:
        return json.loads((self.run_dir / "snapshot.json").read_text(encoding="utf-8"))

    def summary(self) -> dict:
        return json.loads((self.run_dir / "summary.json").read_text(encoding="utf-8"))

    def history(self) -> dict:
        return json.loads((self.out_dir / "history.json").read_text(encoding="utf-8"))


def run_export(m: MockFrontegg, out_dir: str | Path, secret: str = CLIENT_SECRET, **kwargs) -> ExportResult:
    env = {"FRONTEGG_CLIENT_ID": CLIENT_ID, "FRONTEGG_CLIENT_SECRET": secret, "FRONTEGG_BASE_URL": m.url,
           "FDE_HOME": str(Path(out_dir) / ".fde-home")}
    out, errs = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(runner, "DOTENV_PATH", Path(out_dir) / ".env-absent"), \
            mock.patch.object(runner, "FronteggClient", fast_client), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
        code = runner.main(out_dir=out_dir, **kwargs)
    return ExportResult(code, Path(out_dir), out.getvalue(), errs.getvalue())


@contextlib.contextmanager
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


REPO = Path(__file__).resolve().parent.parent


def cli(args: list[str], m: MockFrontegg | None, home: str | Path, secret: str = CLIENT_SECRET,
        extra_env: dict | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run the real CLI in a subprocess, pointed at the mock."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FRONTEGG_", "FDE_"))}
    env["FDE_HOME"] = str(Path(home) / ".fde-home")
    env["PYTHONIOENCODING"] = "utf-8"
    if m is not None:
        env.update({"FRONTEGG_CLIENT_ID": CLIENT_ID, "FRONTEGG_CLIENT_SECRET": secret, "FRONTEGG_BASE_URL": m.url})
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "frontegg_data_export", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)
