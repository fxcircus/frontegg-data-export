"""Retries, backoff, Retry-After parsing, re-auth and error reporting."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from unittest import mock

from frontegg_data_export import client as client_mod
from frontegg_data_export.client import (
    ApiError,
    AuthError,
    FronteggClient,
    backoff_delay,
    parse_retry_after,
)
from tests.mock_frontegg import CLIENT_ID, CLIENT_SECRET, Faults, MockFrontegg, make_dataset


class FakeResponse:
    def __init__(self, status: int, body, headers: dict | None = None):
        self.status = status
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Scripted responses keyed by (method, path). Each entry is a status
    code, a (status, body, headers) tuple, or an exception instance."""

    def __init__(self, script: dict):
        self.script = {k: list(v) for k, v in script.items()}
        self.seen: list[tuple[str, str]] = []

    def open(self, req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path.split("?", 1)[0]
        key = (req.get_method(), path)
        self.seen.append(key)
        queue = self.script[key]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            item = (item, {"ok": True} if item < 300 else {"errors": ["boom"]}, {})
        status, body, headers = item
        if status >= 300:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            raise urllib.error.HTTPError(req.full_url, status, "err", headers, io.BytesIO(raw))
        return FakeResponse(status, body, headers)


AUTH_OK = (200, {"token": "tok", "expiresIn": 86400}, {})


def make_client(test: unittest.TestCase, script: dict, **kw) -> tuple[FronteggClient, FakeOpener, list[float]]:
    opener = FakeOpener({("POST", "/auth/vendor/"): [AUTH_OK], **script})
    sleeps: list[float] = []
    patcher = mock.patch.object(client_mod, "_OPENER", opener)
    patcher.start()
    test.addCleanup(patcher.stop)
    kw.setdefault("rand", lambda: 1.0)
    kw.setdefault("rate", 1e9)               # pacing has its own tests; keep it out of these
    kw.setdefault("ceilings_per_min", {})
    c = FronteggClient("https://api.example.com", "id", "secret", sleep=sleeps.append, **kw)
    return c, opener, sleeps


class RetryAfterTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_retry_after("7"), 7.0)
        self.assertEqual(parse_retry_after(" 0 "), 0.0)
        self.assertEqual(parse_retry_after("-3"), 0.0)
        self.assertEqual(parse_retry_after("1.5"), 1.5)

    def test_http_date(self):
        now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        later = format_datetime(now + timedelta(seconds=30), usegmt=True)
        self.assertAlmostEqual(parse_retry_after(later, now=now.timestamp()), 30.0)
        earlier = format_datetime(now - timedelta(seconds=30), usegmt=True)
        self.assertEqual(parse_retry_after(earlier, now=now.timestamp()), 0.0)

    def test_missing_or_garbage(self):
        for v in (None, "", "soon", "Mon, 99 Foo 2026"):
            with self.subTest(v=v):
                self.assertIsNone(parse_retry_after(v))


class BackoffTests(unittest.TestCase):
    def test_full_jitter_bounds(self):
        self.assertEqual(backoff_delay(0, lambda: 1.0), 1.0)
        self.assertEqual(backoff_delay(3, lambda: 1.0), 8.0)
        self.assertEqual(backoff_delay(10, lambda: 1.0), 60.0)    # capped
        self.assertEqual(backoff_delay(5, lambda: 0.0), 0.0)
        self.assertEqual(backoff_delay(2, lambda: 0.5), 2.0)


class RetryTests(unittest.TestCase):
    def test_5xx_is_retried_then_succeeds(self):
        c, opener, sleeps = make_client(self, {("GET", "/x"): [503, 502, 500, (200, {"v": 1}, {})]})
        self.assertEqual(c.get("/x"), {"v": 1})
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])
        self.assertEqual(c.retries, 3)

    def test_429_honours_retry_after_seconds(self):
        c, _, sleeps = make_client(self, {("GET", "/x"): [(429, {}, {"Retry-After": "7"}), 200]})
        c.get("/x")
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(c.h429, 1)

    def test_429_honours_retry_after_date(self):
        now = 1_800_000_000.0
        when = format_datetime(datetime.fromtimestamp(now + 12, tz=timezone.utc), usegmt=True)
        c, _, sleeps = make_client(self, {("GET", "/x"): [(429, {}, {"Retry-After": when}), 200]},
                                   wall_clock=lambda: now)
        c.get("/x")
        self.assertEqual(sleeps, [12.0])

    def test_retry_after_is_capped(self):
        c, _, sleeps = make_client(self, {("GET", "/x"): [(503, {}, {"Retry-After": "86400"}), 200]})
        c.get("/x")
        self.assertEqual(sleeps, [300.0])

    def test_timeouts_and_dropped_connections_are_retried(self):
        c, _, sleeps = make_client(self, {("GET", "/x"): [
            TimeoutError("timed out"), urllib.error.URLError("connection reset"),
            ConnectionResetError(), 200]})
        self.assertEqual(c.get("/x"), {"ok": True})
        self.assertEqual(len(sleeps), 3)

    def test_gives_up_after_max_attempts_with_status_and_trace(self):
        c, opener, sleeps = make_client(
            self, {("GET", "/x"): [(500, {"errors": ["db down"]}, {"frontegg-trace-id": "trace-1"})]},
            max_attempts=4)
        with self.assertRaises(ApiError) as cm:
            c.get("/x")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(cm.exception.trace_id, "trace-1")
        self.assertIn("db down", cm.exception.message)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(opener.seen.count(("GET", "/x")), 4)

    def test_network_failure_exhausted(self):
        c, _, _ = make_client(self, {("GET", "/x"): [TimeoutError()]}, max_attempts=3)
        with self.assertRaises(ApiError) as cm:
            c.get("/x")
        self.assertIsNone(cm.exception.status)
        self.assertIn("timed out", cm.exception.message)

    def test_client_errors_are_not_retried(self):
        for status in (400, 403, 404, 414):
            with self.subTest(status=status):
                c, opener, sleeps = make_client(self, {("GET", "/x"): [status]})
                with self.assertRaises(ApiError) as cm:
                    c.get("/x")
                self.assertEqual(cm.exception.status, status)
                self.assertEqual(sleeps, [])

    def test_401_reauths_once(self):
        c, opener, _ = make_client(self, {("GET", "/x"): [401, 200]})
        self.assertEqual(c.get("/x"), {"ok": True})
        self.assertEqual(opener.seen.count(("POST", "/auth/vendor/")), 2)

    def test_persistent_401_is_an_error_not_a_loop(self):
        c, opener, _ = make_client(self, {("GET", "/x"): [401]})
        with self.assertRaises(ApiError) as cm:
            c.get("/x")
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(opener.seen.count(("POST", "/auth/vendor/")), 2)


class AuthTests(unittest.TestCase):
    def test_bad_credentials_say_where_to_look(self):
        c, _, sleeps = make_client(self, {("POST", "/auth/vendor/"): [401]})
        with self.assertRaises(AuthError) as cm:
            c.authenticate()
        self.assertIn("Keys & domains", cm.exception.message)
        self.assertIn("region", cm.exception.message)
        self.assertEqual(sleeps, [])

    def test_auth_5xx_is_retried(self):
        c, _, sleeps = make_client(self, {("POST", "/auth/vendor/"): [503, AUTH_OK]})
        c.authenticate()
        self.assertEqual(c.token, "tok")
        self.assertEqual(len(sleeps), 1)

    def test_not_frontegg(self):
        c, _, _ = make_client(self, {("POST", "/auth/vendor/"): [(200, b"<html>hello</html>", {})]})
        with self.assertRaises(AuthError) as cm:
            c.authenticate()
        self.assertIn("base URL", cm.exception.message)

    def test_auth_error_does_not_echo_the_secret(self):
        c, _, _ = make_client(self, {("POST", "/auth/vendor/"): [(401, {"errors": ["bad secret: secret"]}, {})]})
        with self.assertRaises(AuthError) as cm:
            c.authenticate()
        self.assertNotIn("secret:", cm.exception.message)


class MockServerRetryTests(unittest.TestCase):
    """The same behaviors end to end against the mock's injected faults."""

    def _client(self, m: MockFrontegg, sleeps: list) -> FronteggClient:
        return FronteggClient(m.url, CLIENT_ID, CLIENT_SECRET, sleep=sleeps.append, rand=lambda: 0.0,
                              rate=1e9, ceilings_per_min={})

    def _all_users(self, c: FronteggClient) -> list:
        out, page = [], 0
        while True:
            items = c.get("/identity/resources/users/v3", {"_limit": 200, "_offset": page})["items"]
            if not items:
                return out
            out.extend(items)
            page += 1

    def test_intermittent_5xx_does_not_abort(self):
        ds = make_dataset()
        with MockFrontegg(ds, Faults(error_5xx_every=2)) as m:
            sleeps: list = []
            c = self._client(m, sleeps)
            self.assertEqual(len(self._all_users(c)), len(ds.users))
            self.assertGreater(c.retries, 0)

    def test_rate_limited_with_date_retry_after(self):
        ds = make_dataset()
        with MockFrontegg(ds, Faults(rate_limit_every=2, retry_after="date")) as m:
            sleeps: list = []
            c = self._client(m, sleeps)
            self.assertEqual(len(self._all_users(c)), len(ds.users))
            self.assertGreater(c.h429, 0)
            self.assertTrue(all(0 <= s <= 2 for s in sleeps), sleeps)

    def test_token_expiry_mid_run(self):
        ds = make_dataset()
        with MockFrontegg(ds, Faults(token_uses=1)) as m:
            c = self._client(m, [])
            self.assertEqual(len(self._all_users(c)), len(ds.users))
            self.assertGreater(len(m.calls_to("/auth/vendor/")), 1)


if __name__ == "__main__":
    unittest.main()
