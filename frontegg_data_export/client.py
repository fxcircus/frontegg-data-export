"""HTTP client with throttling, retries, re-auth, rate-limit accounting."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .logs import _log

HTTP_TIMEOUT = 30


class FronteggClient:
    def __init__(self, base_url: str, client_id: str, secret: str) -> None:
        self.base_url = base_url.rstrip("/")
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

    def authenticate(self) -> None:
        url = f"{self.base_url}/auth/vendor/"
        body = json.dumps({"clientId": self.client_id, "secret": self.secret}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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
        req = urllib.request.Request(url, headers=headers, method=method)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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
