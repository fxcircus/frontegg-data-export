"""The only module that talks to Frontegg.

Read-only is enforced here, not by convention: every request goes through
`FronteggClient._send()`, which refuses anything other than `GET` or the
single `POST /auth/vendor/` that mints the token, before any network I/O.
Redirects are not followed, so a 3xx can't replay the token or the POST
somewhere else.

Transient failures (429, any 5xx, timeouts, dropped connections) are retried
with full-jitter exponential backoff. A `Retry-After` header wins over the
backoff when present, as seconds or as an HTTP date.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from typing import Any, Callable

from .logs import _log

HTTP_TIMEOUT = 30
AUTH_PATH = "/auth/vendor/"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

MAX_ATTEMPTS = 6          # first try + 5 retries
BACKOFF_BASE = 1.0        # seconds; attempt n waits up to base * 2**n ...
BACKOFF_CAP = 60.0        # ... but never more than this
RETRY_AFTER_CAP = 300.0   # don't let a server park us for longer than 5 minutes


class ReadOnlyViolation(RuntimeError):
    """Raised before any I/O when code tries to send a non-read request."""


class ApiError(Exception):
    """A request that failed for good (after any retries)."""

    def __init__(self, message: str, *, status: int | None = None, method: str = "GET",
                 path: str = "", trace_id: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.method = method
        self.path = path
        self.trace_id = trace_id


class AuthError(ApiError):
    """Could not get a token. The message says what to fix."""


def check_request_allowed(method: str, path: str) -> None:
    if method == "GET":
        return
    if method == "POST" and path == AUTH_PATH:
        return
    raise ReadOnlyViolation(
        f"Refusing {method} {path}: this tool only reads from Frontegg "
        f"(GET, plus POST {AUTH_PATH} to get a token).")


def validate_base_url(url: str) -> str:
    """Accept https:// anywhere, or http:// only on this machine (the mock)."""
    parts = urllib.parse.urlsplit((url or "").strip())
    host = parts.hostname or ""
    if parts.query or parts.fragment or not host:
        raise ValueError(f"Not a valid API base URL: {url!r}")
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS):
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    raise ValueError(f"The API base URL must start with https:// (got {url!r})")


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP date (RFC 9110).
    Returns seconds to wait (never negative), or None if absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


def backoff_delay(attempt: int, rand: Callable[[], float] = random.random,
                  base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP) -> float:
    """Full jitter: uniform in [0, min(cap, base * 2**attempt)]."""
    return rand() * min(cap, base * (2 ** attempt))


def is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None   # urllib then raises HTTPError for the 3xx


_OPENER = urllib.request.build_opener(_NoRedirect)

# Errors that mean "the network let us down", worth another try.
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


def _read_error_body(e: urllib.error.HTTPError) -> bytes:
    try:
        return e.read() or b""
    except Exception:  # a broken error body must not hide the status
        return b""


def _error_text(raw: bytes) -> str:
    """A short, human-readable reason from a Frontegg error body."""
    text = raw[:300].decode("utf-8", errors="replace").strip()
    try:
        body = json.loads(raw)
    except ValueError:
        return text
    if isinstance(body, dict):
        errs = body.get("errors") or body.get("message") or body.get("error")
        if isinstance(errs, list):
            return "; ".join(str(x) for x in errs)[:300]
        if errs:
            return str(errs)[:300]
    return text


class FronteggClient:
    def __init__(self, base_url: str, client_id: str, secret: str, *,
                 timeout: float = HTTP_TIMEOUT, max_attempts: int = MAX_ATTEMPTS,
                 backoff_base: float = BACKOFF_BASE, backoff_cap: float = BACKOFF_CAP,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 rand: Callable[[], float] = random.random) -> None:
        self.base_url = validate_base_url(base_url)
        self.client_id = client_id
        self.secret = secret
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.sleep = sleep
        self.clock = clock
        self.wall_clock = wall_clock
        self.rand = rand
        self.token: str | None = None
        self.token_expires_at: float = 0.0
        # running stats
        self.calls = 0
        self.retries = 0
        self.errors = 0
        self.h429 = 0
        self.rate_limit_headers_seen = 0
        self.last_trace_id = ""

    # ---- the single choke point -----------------------------------------
    def _send(self, method: str, path: str, *, params: dict | None = None,
              body: bytes | None = None, headers: dict | None = None):
        check_request_allowed(method, path)
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        return _OPENER.open(req, timeout=self.timeout)

    def _wait_before_retry(self, attempt: int, retry_after: str | None) -> float:
        delay = parse_retry_after(retry_after, now=self.wall_clock())
        if delay is None:
            delay = backoff_delay(attempt, self.rand, self.backoff_base, self.backoff_cap)
        delay = min(delay, RETRY_AFTER_CAP)
        self.retries += 1
        self.sleep(delay)
        return delay

    # ---- authentication ---------------------------------------------------
    def authenticate(self) -> None:
        body = json.dumps({"clientId": self.client_id, "secret": self.secret}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        host = urllib.parse.urlsplit(self.base_url).netloc
        for attempt in range(self.max_attempts):
            t0 = self.clock()
            last = attempt + 1 >= self.max_attempts
            try:
                with self._send("POST", AUTH_PATH, body=body, headers=headers) as resp:
                    raw = resp.read()
                    trace = resp.headers.get("frontegg-trace-id", "")
            except urllib.error.HTTPError as e:
                trace = e.headers.get("frontegg-trace-id", "") if e.headers else ""
                reason = _error_text(_read_error_body(e))
                _log(f"POST {AUTH_PATH} -> {e.code} trace={trace}", "WARN")
                if is_retryable_status(e.code) and not last:
                    if e.code == 429:
                        self.h429 += 1
                    self._wait_before_retry(attempt, e.headers.get("Retry-After") if e.headers else None)
                    continue
                raise AuthError(_auth_failure_message(e.code, host, reason),
                                status=e.code, method="POST", path=AUTH_PATH, trace_id=trace) from None
            except _NETWORK_ERRORS as e:
                _log(f"POST {AUTH_PATH} network error: {_describe_network_error(e)}", "WARN")
                if not last:
                    self._wait_before_retry(attempt, None)
                    continue
                raise AuthError(f"Couldn't reach {host}: {_describe_network_error(e)}. "
                                "Check your internet connection and the API base URL.",
                                method="POST", path=AUTH_PATH) from None
            try:
                payload = json.loads(raw)
                token = payload["token"]
            except (ValueError, KeyError, TypeError):
                raise AuthError(f"{host} answered, but not like Frontegg's API. Check the API base URL.",
                                method="POST", path=AUTH_PATH, trace_id=trace) from None
            self.token = token
            expires_in = int(payload.get("expiresIn") or 3600)
            self.token_expires_at = self.clock() + expires_in - 60  # 60s safety margin
            _log(f"AUTH OK expiresIn={expires_in}s elapsed={self.clock()-t0:.2f}s trace={trace}")
            return
        raise AssertionError("unreachable")

    def _ensure_token(self) -> None:
        if not self.token or self.clock() >= self.token_expires_at:
            if self.token:
                _log("Re-authenticating (token expiring)…", "WARN")
            self.authenticate()

    # ---- reads ------------------------------------------------------------
    def get(self, path: str, params: dict | None = None, tenant_id: str | None = None) -> Any:
        reauthed = False
        for attempt in range(self.max_attempts):
            last = attempt + 1 >= self.max_attempts
            self._ensure_token()
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            if tenant_id is not None:
                headers["frontegg-tenant-id"] = tenant_id
            target = path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
            t0 = self.clock()
            try:
                with self._send("GET", path, params=params, headers=headers) as resp:
                    raw = resp.read()
                    status, resp_headers = resp.status, resp.headers
            except urllib.error.HTTPError as e:
                raw = _read_error_body(e)
                status, resp_headers = e.code, (e.headers or {})
            except _NETWORK_ERRORS as e:
                why = _describe_network_error(e)
                _log(f"GET {target} network error: {why} attempt={attempt + 1}", "WARN")
                if not last:
                    self._wait_before_retry(attempt, None)
                    continue
                self.errors += 1
                raise ApiError(f"Network error after {self.max_attempts} attempts: {why}",
                               path=path) from None

            self.calls += 1
            trace = resp_headers.get("frontegg-trace-id", "") or ""
            if trace:
                self.last_trace_id = trace
            rl_limit = resp_headers.get("x-rate-limit-limit", "") or ""
            if rl_limit:
                self.rate_limit_headers_seen += 1
            elapsed = self.clock() - t0
            tenant_note = f" tenant={tenant_id}" if tenant_id else ""
            level = "INFO" if 200 <= status < 300 else "WARN"
            _log(f"GET {target} -> {status} {elapsed:.2f}s trace={trace}{tenant_note} rl={rl_limit}", level)

            if 200 <= status < 300:
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    if not last:
                        self._wait_before_retry(attempt, None)
                        continue
                    self.errors += 1
                    raise ApiError("Frontegg returned a response that isn't valid JSON.",
                                   status=status, path=path, trace_id=trace) from None
            if status == 401 and not reauthed and not last:
                reauthed = True
                _log(f"GET {target} -> 401, getting a new token and retrying", "WARN")
                self.token = None
                continue
            if is_retryable_status(status) and not last:
                if status == 429:
                    self.h429 += 1
                delay = self._wait_before_retry(attempt, resp_headers.get("Retry-After"))
                _log(f"GET {target} -> {status}; retrying in {delay:.1f}s (attempt {attempt + 2}/{self.max_attempts})",
                     "WARN")
                continue
            self.errors += 1
            reason = _error_text(raw)
            _log(f"GET {target} -> {status} {reason}", "ERROR")
            raise ApiError(f"HTTP {status}: {reason}" if reason else f"HTTP {status}",
                           status=status, path=path, trace_id=trace)
        raise AssertionError("unreachable")


def _describe_network_error(e: BaseException) -> str:
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        return str(e.reason)
    if isinstance(e, TimeoutError):
        return "the request timed out"
    return type(e).__name__ + (f": {e}" if str(e) else "")


def _auth_failure_message(status: int, host: str, reason: str) -> str:
    if status in (400, 401, 403):
        return ("Frontegg rejected the Client ID or API key. Copy both again from your environment's "
                "Keys & domains page in the Frontegg Portal, and check that the region matches "
                f"that environment (currently {host}).")
    if status == 404:
        return f"{host} doesn't look like a Frontegg API address. Check the region or the custom base URL."
    if status == 429:
        return "Frontegg is rate-limiting logins from this IP address. Wait a minute and try again."
    if 500 <= status <= 599:
        return f"Frontegg's login service is having problems (HTTP {status}). Try again in a few minutes."
    return f"Couldn't get a token from {host} (HTTP {status}{': ' + reason if reason else ''})."
