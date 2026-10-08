"""Shared test helpers: run an export against the mock in a temp folder."""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from frontegg_data_export import logs, runner
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

    def json_files(self) -> list[Path]:
        return sorted(self.out_dir.glob("frontegg_account_backup_*.json"))

    def snapshot(self) -> dict:
        files = self.json_files()
        assert len(files) == 1, files
        return json.loads(files[0].read_text(encoding="utf-8"))


def run_export(m: MockFrontegg, tmp: str, secret: str = CLIENT_SECRET, **kwargs) -> ExportResult:
    env = {"FRONTEGG_CLIENT_ID": CLIENT_ID, "FRONTEGG_CLIENT_SECRET": secret, "FRONTEGG_BASE_URL": m.url}
    out, errs = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(runner, "APP_DIR", Path(tmp)), \
            mock.patch.object(runner, "DOTENV_PATH", Path(tmp) / ".env"), \
            mock.patch.object(runner, "FronteggClient", functools.partial(fast_client)), \
            mock.patch.object(logs, "LOG_PATH", Path(tmp) / "export.log"), \
            mock.patch.object(logs, "_log_fp", None), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
        try:
            code = runner.main(**kwargs)
        finally:
            if logs._log_fp:
                logs._log_fp.close()
    return ExportResult(code, Path(tmp), out.getvalue(), errs.getvalue())


@contextlib.contextmanager
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp
