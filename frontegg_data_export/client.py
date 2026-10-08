"""The only module that talks to Frontegg.

Read-only is enforced here, not by convention: every request goes through
`FronteggClient._send()`, which refuses anything other than `GET` or the
single `POST /auth/vendor/` that mints the token, before any network I/O.
Redirects are not followed, so a 3xx can't replay the token or the POST
somewhere else.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .logs import _log

HTTP_TIMEOUT = 30
AUTH_PATH = "/auth/vendor/"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ReadOnlyViolation(RuntimeError):
    """Raised before any I/O when code tries to send a non-read request."""


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None   # urllib then raises HTTPError for the 3xx


_OPENER = urllib.request.build_opener(_NoRedirect)


class FronteggClient:
    def __init__(self, base_url: str, client_id: str, secret: str) -> None:
        self.base_url = validate_base_url(base_url)
        self.client_id = client_id
        self.secret = secret
        self.token: str | None = None
        self.token_expires_at: float = 0.0
        # running stats
        self.calls = 0
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
        return _OPENER.open(req, timeout=HTTP_TIMEOUT)

    def authenticate(self) -> None:
        body = json.dumps({"clientId": self.client_id, "secret": self.secret}).encode()
        t0 = time.time()
        try:
            with self._send("POST", AUTH_PATH, body=body,
                            headers={"Content-Type": "application/json", "Accept": "application/json"}) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Vendor authentication failed: HTTP {e.code} — {e.read()[:300]!r}")
        self.token = payload["token"]
        expires_in = int(payload.get("expiresIn", 3600))
        self.token_expires_at = time.time() + expires_in - 60  # 60s safety margin
        _log(f"AUTH OK expiresIn={expires_in}s elapsed={time.time()-t0:.2f}s")

    def _maybe_reauth(self) -> None:
        if not self.token or time.time() >= self.token_expires_at:
            _log("Re-authenticating (token expiring)…", "WARN")
            self.authenticate()

    def get(self, path: str, params: dict | None = None, tenant_id: str | None = None) -> Any:
        self._maybe_reauth()
        return self._request("GET", path, params=params, tenant_id=tenant_id)

    def _request(self, method: str, path: str, params: dict | None = None,
                 tenant_id: str | None = None, attempt: int = 0) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if tenant_id is not None:
            headers["frontegg-tenant-id"] = tenant_id
        t0 = time.time()
        try:
            with self._send(method, path, params=params, headers=headers) as resp:
                raw = resp.read()
                self.calls += 1
                elapsed = time.time() - t0
                self.last_trace_id = resp.headers.get("frontegg-trace-id", "")
                rl_limit = resp.headers.get("x-rate-limit-limit", "")
                if rl_limit:
                    self.rate_limit_headers_seen += 1
                _log(f"{method} {url} -> {resp.status} {elapsed:.2f}s trace={self.last_trace_id} rl={rl_limit}")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            elapsed = time.time() - t0
            preview = (e.read() or b"")[:300].decode("utf-8", errors="replace")
            if e.code == 401 and attempt == 0:
                _log(f"{method} {url} -> 401, re-auth then retry", "WARN")
                self.authenticate()
                return self._request(method, path, params=params, tenant_id=tenant_id, attempt=attempt + 1)
            if e.code == 429:
                self.h429 += 1
                retry_after = int(e.headers.get("retry-after") or 0) or 5 * (attempt + 1)
                _log(f"{method} {url} -> 429 retry_after={retry_after}s attempt={attempt}", "WARN")
                if attempt < 4:
                    time.sleep(retry_after)
                    return self._request(method, path, params=params, tenant_id=tenant_id, attempt=attempt + 1)
            self.errors += 1
            _log(f"{method} {url} -> {e.code} {preview}", "ERROR")
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 4:
                wait_s = min(2 ** attempt, 30)
                _log(f"{method} {url} network error: {e}; sleeping {wait_s}s and retrying", "WARN")
                time.sleep(wait_s)
                return self._request(method, path, params=params, tenant_id=tenant_id, attempt=attempt + 1)
            self.errors += 1
            _log(f"{method} {url} gave up after retries: {e}", "ERROR")
            raise
