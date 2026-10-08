"""Read-only is enforced in code: only GET and POST /auth/vendor/ reach Frontegg."""

from __future__ import annotations

import ast
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from frontegg_data_export import client as client_mod
from frontegg_data_export.client import (
    FronteggClient,
    ReadOnlyViolation,
    check_request_allowed,
    validate_base_url,
)

PACKAGE_DIR = Path(client_mod.__file__).resolve().parent


class AllowListTests(unittest.TestCase):
    def test_get_is_allowed_anywhere(self):
        check_request_allowed("GET", "/identity/resources/users/v3")

    def test_only_the_token_post_is_allowed(self):
        check_request_allowed("POST", "/auth/vendor/")
        for path in ("/auth/vendor", "/auth/vendor/x", "/identity/resources/users/v1",
                     "/auth/vendor/?x=1", "/auth/vendor/../identity/resources/users/v1"):
            with self.subTest(path=path), self.assertRaises(ReadOnlyViolation):
                check_request_allowed("POST", path)

    def test_write_methods_are_refused(self):
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get", "post"):
            with self.subTest(method=method), self.assertRaises(ReadOnlyViolation):
                check_request_allowed(method, "/auth/vendor/" if method.lower() == "post" else "/x")

    def test_refusal_happens_before_any_io(self):
        c = FronteggClient("https://api.example.com", "id", "secret")
        with mock.patch.object(client_mod, "_OPENER") as opener:
            for method in ("PUT", "PATCH", "DELETE"):
                with self.assertRaises(ReadOnlyViolation):
                    c._send(method, "/identity/resources/users/v1")
            with self.assertRaises(ReadOnlyViolation):
                c._send("POST", "/identity/resources/users/v1", body=b"{}")
            opener.open.assert_not_called()


class BaseUrlTests(unittest.TestCase):
    def test_https_and_loopback_http_accepted(self):
        self.assertEqual(validate_base_url("https://api.us.frontegg.com/"), "https://api.us.frontegg.com")
        self.assertEqual(validate_base_url("http://127.0.0.1:8765"), "http://127.0.0.1:8765")
        self.assertEqual(validate_base_url("http://localhost:8765/"), "http://localhost:8765")

    def test_cleartext_and_junk_refused(self):
        for url in ("http://api.frontegg.com", "ftp://api.frontegg.com", "api.frontegg.com",
                    "https://api.frontegg.com?x=1", "", "https://"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_base_url(url)


class RedirectTests(unittest.TestCase):
    def test_redirects_are_not_followed(self):
        seen: list[str] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen.append(f"POST {self.path}")
                self.send_response(307)
                self.send_header("Location", "/somewhere-else")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            c = FronteggClient(f"http://127.0.0.1:{httpd.server_address[1]}", "id", "secret")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                c._send("POST", "/auth/vendor/", body=b"{}")
            self.assertEqual(cm.exception.code, 307)
            self.assertEqual(seen, ["POST /auth/vendor/"])
        finally:
            httpd.shutdown()
            httpd.server_close()


class FullExportTrafficTests(unittest.TestCase):
    def test_full_export_sends_only_get_and_the_token_post(self):
        import functools
        import os
        import tempfile

        from frontegg_data_export import logs, runner
        from tests.mock_frontegg import CLIENT_ID, CLIENT_SECRET, MockFrontegg, make_dataset

        with MockFrontegg(make_dataset()) as m, tempfile.TemporaryDirectory() as tmp:
            env = {"FRONTEGG_CLIENT_ID": CLIENT_ID, "FRONTEGG_CLIENT_SECRET": CLIENT_SECRET,
                   "FRONTEGG_BASE_URL": m.url}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(runner, "APP_DIR", Path(tmp)), \
                    mock.patch.object(runner, "DOTENV_PATH", Path(tmp) / ".env"), \
                    mock.patch.object(runner, "FronteggClient",
                                      functools.partial(FronteggClient, sleep=lambda s: None)), \
                    mock.patch.object(logs, "LOG_PATH", Path(tmp) / "export.log"), \
                    mock.patch.object(logs, "_log_fp", None), \
                    mock.patch("sys.stdout"):
                self.assertEqual(runner.main(), 0)
                if logs._log_fp:
                    logs._log_fp.close()
            methods = {(r["method"], r["path"] if r["method"] != "GET" else "*") for r in m.requests}
            self.assertEqual(methods, {("GET", "*"), ("POST", "/auth/vendor/")})
            self.assertGreater(len(m.requests), 20)


class SingleHttpModuleTests(unittest.TestCase):
    """Only client.py may import an HTTP client library."""

    FORBIDDEN = {"urllib.request", "http.client", "requests", "urllib3", "httpx"}

    def test_no_other_module_imports_an_http_client(self):
        offenders = []
        for py in sorted(PACKAGE_DIR.rglob("*.py")):
            if py.name == "client.py":
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if any(n in self.FORBIDDEN for n in names):
                    offenders.append(f"{py.relative_to(PACKAGE_DIR)}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
