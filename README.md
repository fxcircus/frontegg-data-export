# Frontegg Account Backup

A single-file, read-only script that exports your complete Frontegg environment state into one JSON document.

Useful for business continuity, compliance/audit snapshots, analytics pipelines, disaster-recovery rehearsal, and migration testing.

- **Zero dependencies** beyond Python 3.10+ stdlib (no `pip install` step)
- **Single output file** — one self-contained JSON that holds everything
- **Read-only** — never issues a write of any kind against your Frontegg environment
- **Battle-tested** against production Frontegg environments at scale (tens of thousands of users / tenants)
- **Encodes a dozen non-obvious Frontegg API quirks** so you don't trip over them yourself (see [Frontegg API quirks](#frontegg-api-quirks-the-script-handles))

## Quick start

```bash
git clone https://github.com/fxcircus/frontegg-account-backup.git
cd frontegg-account-backup
cp .env.example .env
# edit .env and fill in your FRONTEGG_CLIENT_ID, FRONTEGG_CLIENT_SECRET, FRONTEGG_BASE_URL
python3 export.py
```

That's it. The script prints clear per-step progress and writes:

- `frontegg_account_backup_<YYYYMMDDTHHMMSSZ>.json` — your backup
- `export.log` — per-call audit trail

## What gets backed up

A single JSON document with these top-level sections:

| Key | What it contains |
|---|---|
| `schemaVersion` | `"1.0"` — version of this export format |
| `exportRun` | Run metadata: start/end timestamps, duration, baseUrl, vendorId, API-call count, error count, 429 count, rate-limit-header observations, last `frontegg-trace-id` |
| `counts` | Quick-glance record counts per resource |
| `tenants` | Every tenant / account in the environment, with metadata, timestamps, `isReseller`, etc. |
| `users` | Every user, with `tenantIds[]`, `tenants[]` (memberships), `metadata`, `vendorMetadata` inline |
| `roles` | Role catalog — each role's `permissions[]` is included |
| `permissions` | Permission catalog — each permission's reverse `roleIds[]` is included |
| `plans` | Enriched plans: `assignedTenantsCount`, `assignedUsersCount`, `featuresCount` per plan |
| `features` | Plan features |
| `featureFlags` | Feature flags |
| `hierarchyTrees` | Nested account-hierarchy trees, one per reseller-root tenant |
| `userRoleAssignments` | `{vendorId, tenantId, userId, roleIds[]}` per `(user, tenant)` pair — join with `roles` for names + permissions |
| `entitlements` | `{id, planId, tenantId, userId, expirationDate, createdAt, updatedAt}` per assignment — `userId` is null for tenant-level entitlements |

## Configuration

Three values, all in `.env` (or as environment variables — the script falls back to `os.environ` if a value is missing from the file):

```ini
FRONTEGG_CLIENT_ID=
FRONTEGG_CLIENT_SECRET=
FRONTEGG_BASE_URL=https://api.frontegg.com
```

### Where to find the credentials

In the **Frontegg Portal**: **Settings → API Keys → Vendor Key**. That page shows the `clientId` and `secret` to use.

### Regional base URLs

| Region | Base URL |
|---|---|
| EU (default) | `https://api.frontegg.com` |
| US | `https://api.us.frontegg.com` |
| Canada | `https://api.ca.frontegg.com` |
| Australia | `https://api.au.frontegg.com` |

If unsure, your environment's correct host is shown in the Frontegg Portal API docs page for that environment.

## What the terminal looks like

```
========================================================================
 Frontegg Account Backup
 started 2026-06-05T10:00:00Z  base=https://api.frontegg.com
========================================================================
      Output dir: /path/to/frontegg-account-backup
      Log file  : export.log
      Read-only — GET requests only (plus one POST /auth/vendor/ for the token).

[1/8] Authenticate with vendor endpoint
      ✓ Authenticated. Token valid ~24h.

[2/8] Pull catalogs (roles, permissions, plans, features, feature_flags)
      ✓ Roles: 40  (each role includes its permissions[])
      ✓ Permissions: 182  (each includes the reverse roleIds[])
      ✓ Plans (enriched): 17
      ✓ Features: 19
      ✓ Feature flags: 1

[3/8] Pull tenants (200/page, page-index `_offset`)
      tenants total reported: 5308 across 27 pages
      page 5 done; tenants so far: 1000/5308
      ...
      ✓ Tenants: 5308

[4/8] Pull hierarchy trees (one per `isReseller: true` tenant)
      ✓ Trees: 18  |  unique tenants in any hierarchy: 30

[5/8] Pull users (200/page, vendor-wide, no tenant header)
      ✓ Users: 5247

[6/8] Pull user-role assignments (batched per tenant)
      tenants with users to query: 2959
      tenants 100/2959  assignments=180  elapsed=37s  eta≈1063s
      ...
      ✓ Role assignment records: 5272

[7/8] Pull entitlements (limit=10/page until empty — Frontegg silently caps)
      ✓ Entitlements: 5345

[8/8] Assemble + write single backup JSON
      ✓ Wrote frontegg_account_backup_20260605T100000Z.json (28.4 MB)

========================================================================
 Done
 duration=1248s  api_calls=3577  errors=0  429s=0  rate_limit_headers_seen=0
========================================================================
```

## Expected runtime

The script is dominated by two long loops:

- **Per-tenant role-assignment pull** — one batched call for each tenant that has users (up to 100 user IDs per call)
- **Entitlements pagination** — Frontegg caps this endpoint at 10 records per page

Rough sizing observed in the field:

| Env scale | Calls | Runtime |
|---|---|---|
| Small (~1K users, ~1K tenants) | ~1,500 | ~5–10 min |
| Medium (~5K users, ~5K tenants) | ~3,500 | ~20–25 min |
| Large (~28K users, ~17K tenants) | ~16,000 | ~90–100 min |

Sustained pace is ~3 req/sec (bound by Frontegg's network latency). Throttle ceiling is set to ~12 req/sec to stay well inside Frontegg's documented 1000 req/min/IP default for Scale & Enterprise tiers. No 429s observed in long live runs.

## Frontegg API quirks the script handles

Several Frontegg API behaviours don't match the public docs cleanly. These are encoded in the script — if you write your own tooling, save yourself the time:

1. **`_offset` on `/tenants/v2` and `/users/v3` is a PAGE INDEX**, not item offset. `_limit=200&_offset=200` returns empty (page 200 doesn't exist), not items 200+. Valid range: `_offset=0..(totalPages-1)`.
2. **Parameter naming is inconsistent across endpoints.** Some use `_limit/_offset` (underscored), others use `limit/offset` (no underscore). Using the wrong one returns a stale-cached small response, no error:
   - `_limit/_offset`: `/tenants/v2`, `/users/v3`, `/feature-flags/v1`
   - `limit/offset`: `/plans/v1`, `/plans/v1/enriched`, `/features/v1`, `/entitlements/v2`
3. **`/entitlements/v2` silently caps `limit` at 10.** `limit=11` returns 0 items. The script pages with `limit=10` until the response is empty.
4. **`/plans/v1` (non-enriched) returns 10 records maximum**, with no indication more exist. Use `/plans/v1/enriched` for the complete catalog (also gives you per-plan `assignedTenantsCount`).
5. **`/features/v1` caps `limit` at 100** (returns a 400 error above that).
6. **`/tenants/hierarchy/v1*` endpoints require a `frontegg-tenant-id` header** even with a vendor token. Without it: 403 "Tenant ID is not specified". They answer relative to that tenant.
7. **Only tenants with `isReseller: true` have non-trivial hierarchy trees** — other tenants are flat. Full-hierarchy capture is O(reseller count), not O(tenant count). Tenants don't carry a `parentTenantId` field, so the hierarchy cannot be derived from the tenant list alone.
8. **`/identity/resources/users/v3/roles` is a batched LOOKUP, not a listing.** Takes `ids=<csv>` of up to 250 user IDs per call, tenant-scoped via header. Cross-tenant header returns empty `[]`.
9. **Roles are NOT embedded in the bulk `/users/v3` response** — `tenants[]` only carries `{tenantId, isDisabled, temporaryExpirationDate}`. You still need per-tenant role-batch calls. (As a bonus, `/identity/resources/vendor-only/users/v1/{userId}` *does* return a single user with `tenants[].roles[]` inflated — useful for spot-checks but too slow for bulk export.)
10. **`x-rate-limit-*` headers appear only on some `/identity/*` endpoints**, and not consistently. The script counts whichever it sees but falls back to a static throttle since most calls don't expose a live remaining-quota.
11. **Vendor token TTL is `expiresIn: 86400` (24 h).** Long-running exports do not need to re-auth mid-run; the script does so anyway as a defensive measure if you customise it for very long runs.
12. **A URL with 250 user UUIDs (~9 KB) can trigger HTTP 414 "URI Too Large"** on tenants with many users. The script chunks at 100 IDs per call to stay safely under any reasonable web-server URL limit.

## Safety

- This script issues only `GET` requests, plus a single `POST /auth/vendor/` to mint the token. It NEVER creates, updates, or deletes any Frontegg resource.
- The vendor token is held in memory only — never written to disk.
- `.env` is excluded from version control via the shipped `.gitignore`.
- Output files are written only into the script's directory.

## Troubleshooting

**`Missing required configuration: ...`**
Check that `FRONTEGG_CLIENT_ID`, `FRONTEGG_CLIENT_SECRET`, and `FRONTEGG_BASE_URL` are all set in `.env`, or exported as environment variables.

**`Vendor authentication failed: HTTP 401`**
Invalid credentials, or wrong region. Re-check the values from Frontegg Portal → Settings → API Keys → Vendor Key, and confirm `FRONTEGG_BASE_URL` matches your environment's region.

**Script aborts mid-run (network, transient 5xx)**
It is safe to re-run from scratch — each run produces a fresh timestamped JSON. The append-only `export.log` will have a new `=== run start ===` separator for the new attempt.

**Backup is missing data you expected**
Compare the `counts` block in the JSON against what you see in the Frontegg Portal. If a section is short, raise an issue with the `frontegg-trace-id` from `export.log` and the count delta.

**Output file is very large (hundreds of MB)**
Expected at very large environment scale. The JSON is pretty-printed (`indent=2`) for readability — strip whitespace post-hoc with `jq -c . input.json > compact.json` to shrink by ~3×.

## Versioning

The export's top-level `schemaVersion` field starts at `"1.0"`. If the structure of the JSON ever changes in a non-additive way, this version will bump.

## Contributing

Issues and PRs welcome — especially:

- New Frontegg API quirks observed in the wild
- Resource types not yet covered (groups, applications, audits, M2M tokens, etc.)
- Incremental / resumable run support for very large environments
