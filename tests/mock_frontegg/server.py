"""A local mock of the Frontegg API that reproduces the quirks the exporter
handles, plus switchable faults. Standard library only; binds to 127.0.0.1.

    mock = MockFrontegg(make_dataset())
    mock.start()            # mock.url -> "http://127.0.0.1:<port>"
    ...
    mock.stop()
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .data import Dataset

CLIENT_ID = "mock-client-id"
CLIENT_SECRET = "mock-client-secret"


@dataclass
class Faults:
    max_url_length: int = 8000            # longer request targets get 414 URI Too Large
    rate_limit_every: int = 0             # every Nth GET gets 429
    retry_after: str | None = "0"         # Retry-After value on 429: seconds, "date" for an HTTP date, or None
    error_5xx_every: int = 0              # every Nth GET gets a 5xx
    error_5xx_status: int = 503
    token_uses: int = 0                   # each token is valid for N GETs, then 401 (0 = unlimited)
    expires_in: int = 86400               # what /auth/vendor/ reports
    failing_role_tenants: set[str] = field(default_factory=set)   # role lookups here always fail ...
    failing_role_status: int = 500                                # ... with this status
    failing_tree_tenants: set[str] = field(default_factory=set)   # tree calls here get 400 (circular)
    failing_list_paths: set[str] = field(default_factory=set)     # these paths always 500
    audits_mode: str = "ok"               # ok | forbidden | not_found | error
    rate_limit_headers: bool = True       # x-rate-limit-* on /identity/ routes


class MockFrontegg:
    def __init__(self, dataset: Dataset, faults: Faults | None = None,
                 client_id: str = CLIENT_ID, secret: str = CLIENT_SECRET) -> None:
        self.ds = dataset
        self.faults = faults or Faults()
        self.client_id = client_id
        self.secret = secret
        self.requests: list[dict] = []
        self.tokens: dict[str, int] = {}     # token -> remaining uses (-1 = unlimited)
        self.lock = threading.Lock()
        self._get_count = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle --------------------------------------------------------
    def start(self, port: int = 0) -> "MockFrontegg":
        mock = self

        class Handler(_Handler):
            pass
        Handler.mock = mock
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> "MockFrontegg":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- helpers for tests ------------------------------------------------
    def revoke_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def calls_to(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]


def _qs_first(qs: dict, key: str, default=None):
    v = qs.get(key)
    return v[0] if v else default


def _int(qs: dict, key: str, default: int) -> int:
    try:
        return int(_qs_first(qs, key, default))
    except (TypeError, ValueError):
        return default


class _Handler(BaseHTTPRequestHandler):
    mock: MockFrontegg
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:   # keep test output quiet
        pass

    # ---- plumbing ---------------------------------------------------------
    def _send(self, status: int, body, headers: dict | None = None) -> None:
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("frontegg-trace-id", uuid.uuid4().hex)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, method: str, path: str, qs: dict, body: bytes | None) -> None:
        m = self.mock
        with m.lock:
            m.requests.append({
                "method": method, "path": path, "query": qs, "target": self.path,
                "tenant": self.headers.get("frontegg-tenant-id"),
                "authorization": self.headers.get("Authorization"),
                "body": body,
            })

    # ---- methods ----------------------------------------------------------
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        self._record("POST", parts.path, parse_qs(parts.query), body)
        if parts.path != "/auth/vendor/":
            return self._send(404, {"errors": ["Not found"]})
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._send(400, {"errors": ["Invalid JSON"]})
        m = self.mock
        if payload.get("clientId") != m.client_id or payload.get("secret") != m.secret:
            return self._send(401, {"errors": ["Unauthorized"]})
        token = "mock-token-" + uuid.uuid4().hex
        with m.lock:
            m.tokens[token] = m.faults.token_uses or -1
        return self._send(200, {"token": token, "expiresIn": m.faults.expires_in})

    def do_PUT(self) -> None:
        self._reject("PUT")

    def do_PATCH(self) -> None:
        self._reject("PATCH")

    def do_DELETE(self) -> None:
        self._reject("DELETE")

    def _reject(self, method: str) -> None:
        parts = urlsplit(self.path)
        self._record(method, parts.path, parse_qs(parts.query), None)
        self._send(405, {"errors": ["Method not allowed"]})

    def do_GET(self) -> None:
        m, f = self.mock, self.mock.faults
        parts = urlsplit(self.path)
        path, qs = parts.path, parse_qs(parts.query, keep_blank_values=True)
        self._record("GET", path, qs, None)

        if len(self.path) > f.max_url_length:
            return self._send(414, {"errors": ["URI Too Large"]})

        # auth
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        with m.lock:
            uses = m.tokens.get(token)
            if uses is None or uses == 0:
                valid = False
            else:
                valid = True
                if uses > 0:
                    m.tokens[token] = uses - 1
            m._get_count += 1
            n = m._get_count
        if not valid:
            return self._send(401, {"errors": ["Unauthorized"]})

        # injected faults
        if f.rate_limit_every and n % f.rate_limit_every == 0:
            headers = {}
            if f.retry_after == "date":
                headers["Retry-After"] = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=1), usegmt=True)
            elif f.retry_after is not None:
                headers["Retry-After"] = f.retry_after
            return self._send(429, {"errors": ["Too Many Requests"]}, headers)
        if f.error_5xx_every and n % f.error_5xx_every == 0:
            return self._send(f.error_5xx_status, {"errors": ["Service unavailable"]})
        if path in f.failing_list_paths:
            return self._send(500, {"errors": ["Internal error"]})

        handler = ROUTES.get(path)
        if handler is None:
            return self._send(404, {"errors": [f"No route for {path}"]})
        status, body = handler(self, qs)
        headers = {}
        if f.rate_limit_headers and path.startswith("/identity/"):
            headers = {"x-rate-limit-limit": "1000", "x-rate-limit-remaining": "999", "x-rate-limit-reset": "60"}
        return self._send(status, body, headers)

    # ---- routes -----------------------------------------------------------
    def _page_index(self, items: list, qs: dict) -> tuple[int, dict]:
        """`_limit`/`_offset` where `_offset` is a PAGE INDEX. Plain
        `limit`/`offset` return a stale small page without an error."""
        if "_limit" not in qs and ("limit" in qs or "offset" in qs):
            stale = items[:10]
            return 200, {"items": stale, "_metadata": {"totalItems": len(stale), "totalPages": 1}}
        limit = min(_int(qs, "_limit", 50), 200)
        page = _int(qs, "_offset", 0)
        chunk = items[page * limit:(page + 1) * limit]
        total_pages = (len(items) + limit - 1) // limit
        return 200, {"items": chunk, "_metadata": {"totalItems": len(items), "totalPages": total_pages},
                     "_links": {}}

    def r_users(self, qs):
        users = []
        for u in self.mock.ds.users:
            # Quirk: the bulk listing does NOT carry roles per tenant.
            users.append({**u, "tenants": [dict(t) for t in u["tenants"]]})
        return self._page_index(users, qs)

    def r_tenants(self, qs):
        return self._page_index(self.mock.ds.tenants, qs)

    def r_roles(self, qs):
        # Quirk: account-level roles only come back with that account's tenant header.
        tenant = self.headers.get("frontegg-tenant-id")
        extra = [r for r in self.mock.ds.account_roles if tenant and r["tenantId"] == tenant]
        return 200, [*self.mock.ds.roles, *extra]

    def r_permissions(self, qs):
        return 200, self.mock.ds.permissions

    def r_user_roles(self, qs):
        tenant = self.headers.get("frontegg-tenant-id")
        if not tenant:
            return 400, {"errors": ["frontegg-tenant-id header is required"]}
        if tenant in self.mock.faults.failing_role_tenants:
            return self.mock.faults.failing_role_status, {"errors": ["Role lookup failed"]}
        ids: list[str] = []
        for v in qs.get("ids", []):
            ids.extend(x for x in v.split(",") if x)
        out = []
        for uid in ids:
            role_ids = self.mock.ds.role_assignments.get((uid, tenant))
            if role_ids is not None:
                out.append({"userId": uid, "tenantId": tenant, "vendorId": self.mock.ds.vendor_id,
                            "roleIds": list(role_ids)})
        return 200, out

    def r_tree(self, qs):
        tenant = self.headers.get("frontegg-tenant-id")
        if not tenant:
            return 403, {"errors": ["Tenant ID is not specified"]}
        if tenant in self.mock.faults.failing_tree_tenants:
            return 400, {"errors": ["Circular dependency detected in hierarchy"]}
        ds = self.mock.ds

        def node(tid: str) -> dict:
            t = ds.tenant(tid) or {}
            return {"tenantId": tid, "name": t.get("name"), "children": [node(c) for c in ds.children_of(tid)]}
        return 200, node(tenant)

    def _limit_offset(self, items: list, qs: dict, max_limit: int | None = None):
        if "_limit" in qs and "limit" not in qs:
            return 200, {"items": items[:10]}     # stale small page, no error
        limit = _int(qs, "limit", 10)
        offset = _int(qs, "offset", 0)
        if max_limit is not None and limit > max_limit:
            return 400, {"errors": [f"limit must not be greater than {max_limit}"]}
        return 200, {"items": items[offset:offset + limit]}

    def r_plans_enriched(self, qs):
        return self._limit_offset(self.mock.ds.enriched_plans(), qs)

    def r_plans(self, qs):
        # Quirk: the non-enriched listing returns at most 10 records, silently.
        return 200, {"items": self.mock.ds.plans[:10]}

    def r_features(self, qs):
        return self._limit_offset(self.mock.ds.features, qs, max_limit=100)

    def r_flags(self, qs):
        return self._page_index(self.mock.ds.feature_flags, qs)

    def r_entitlements(self, qs):
        limit = _int(qs, "limit", 10)
        offset = _int(qs, "offset", 0)
        items = self.mock.ds.entitlements
        if limit > 10:
            return 200, {"items": [], "hasNext": False}   # Quirk: silently capped at 10
        chunk = items[offset:offset + limit]
        return 200, {"items": chunk, "hasNext": offset + limit < len(items)}

    def r_audits(self, qs):
        mode = self.mock.faults.audits_mode
        if mode == "forbidden":
            return 403, {"errors": ["Forbidden"]}
        if mode == "not_found":
            return 404, {"errors": ["Not found"]}
        if mode == "error":
            return 500, {"errors": ["Internal error"]}
        tenant = self.headers.get("frontegg-tenant-id")
        if not tenant:
            return 400, {"errors": ["frontegg-tenant-id header is required"]}
        count = _int(qs, "count", 0)
        if not 1 <= count <= 200:
            return 400, {"errors": ["count must be between 1 and 200"]}
        offset = _int(qs, "offset", 0)
        start = _qs_first(qs, "created_from")
        end = _qs_first(qs, "created_to")
        rows = [a for a in self.mock.ds.audits if a["tenantId"] == tenant
                and (not start or a["createdAt"] >= start) and (not end or a["createdAt"] <= end)]
        if _qs_first(qs, "sortDirection") == "desc":
            rows = list(reversed(rows))
        return 200, {"data": rows[offset:offset + count], "total": len(rows)}


ROUTES = {
    "/identity/resources/users/v3": _Handler.r_users,
    "/identity/resources/users/v3/roles": _Handler.r_user_roles,
    "/identity/resources/roles/v1": _Handler.r_roles,
    "/identity/resources/permissions/v1": _Handler.r_permissions,
    "/tenants/resources/tenants/v2": _Handler.r_tenants,
    "/tenants/resources/hierarchy/v1/tree": _Handler.r_tree,
    "/entitlements/resources/plans/v1/enriched": _Handler.r_plans_enriched,
    "/entitlements/resources/plans/v1": _Handler.r_plans,
    "/entitlements/resources/features/v1": _Handler.r_features,
    "/entitlements/resources/feature-flags/v1": _Handler.r_flags,
    "/entitlements/resources/entitlements/v2": _Handler.r_entitlements,
    "/audits/resources/audits/v2": _Handler.r_audits,
}
