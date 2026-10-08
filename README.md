# Frontegg Data Export

A read-only export of a Frontegg environment to spreadsheet-friendly CSV files and a full JSON snapshot, with a report of what changed since the previous run.

- **Read-only.** It only ever sends `GET` requests, plus one `POST /auth/vendor/` to get an access token. This is enforced in code and covered by tests.
- **No dependencies.** It needs Python 3.10 or later and nothing else: no `pip install`, no build step, and it works offline apart from the Frontegg API itself.
- **For people and for pipelines.** It writes CSVs (UTF-8, Excel-safe) for people and a versioned JSON snapshot for scripts and diffs.

> **Status:** phase 1 of the build (the core and the command line). The local browser app, scheduling and secret storage are in progress. Full documentation comes with phase 4.

Frontegg Data Export grew out of two earlier single-file scripts, [frontegg-account-backup](https://github.com/fxcircus/frontegg-account-backup) and [frontegg-user-export](https://github.com/fxcircus/frontegg-user-export). The **Users only** preset replaces the users-only script.

## Quick start (command line)

```bash
export FRONTEGG_BASE_URL=https://api.frontegg.com      # or .us / .ca / .au, see Regions
export FRONTEGG_CLIENT_ID=...
export FRONTEGG_CLIENT_SECRET=...                      # the environment's API key

python3 -m frontegg_data_export test-connection
python3 -m frontegg_data_export estimate --probe
python3 -m frontegg_data_export run
```

You can put the same three values in a `.env` file next to the app instead (see `.env.example`). Environment variables win over `.env`.

### Where to find the credentials

In the Frontegg Portal, open **your environment → Keys & domains**. That page shows the **Client ID** and the **API key**. Each environment (for example development and production) has its own pair. The API key is what the API calls `secret`.

### Regions

| Region | API base URL |
|---|---|
| EU | `https://api.frontegg.com` |
| US | `https://api.us.frontegg.com` |
| CA | `https://api.ca.frontegg.com` |
| AU | `https://api.au.frontegg.com` |

Use the region your Frontegg account was created in; your Frontegg Portal's address is a clue (for example, `portal.us.frontegg.com` is US). If the key is rejected and you're sure it's right, the region is the next thing to check.

## What gets exported

Choose a preset, or exact sections with `--sections`:

| Preset | Sections | Replaces |
|---|---|---|
| `users`: Users only | users and their roles per account | frontegg-user-export |
| `standard`: Users, accounts and plans (default) | users, roles, accounts, hierarchy, plans | |
| `full`: Full backup | everything above plus permissions, features and feature flags | frontegg-account-backup |

- Role lookups are the slowest step (about one call per account that has users). `--no-roles` skips them in any preset.
- Login events are optional and off by default: `--login-events` (see [Login events](#login-events)).

## Output

Each run gets its own folder under the output folder (default `exports/` next to the app; change it with `--out`):

```
exports/
  history.json                        every run, and which one is the comparison baseline
  runs/2026-10-08T120000Z/
    users.csv  accounts.csv  plan_assignments.csv  users_without_plan.csv
    changes.csv  [login_events.csv]
    snapshot.json                     full-fidelity JSON (schemaVersion 2.0)
    normalized.json                   compact model used for comparisons
    summary.json                      status, counts, failures, what changed
    run.log                           per-run log with frontegg-trace-id values
```

The last 30 runs are kept (`--keep N`). The current comparison baseline is never deleted.

### CSV files

All CSVs follow the same conventions:

- UTF-8 with a byte-order mark, so Excel shows accented names correctly.
- A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, to stop formula injection.
- Multi-value cells are joined with `; `.
- Dates are ISO 8601 in UTC, and booleans are `yes`/`no`.
- IDs are resolved to names.
- Columns are the same in every preset. A section that wasn't exported leaves its cells blank. A role lookup that failed shows `(lookup failed)`.

| File | One row per | Columns |
|---|---|---|
| `users.csv` | user per account membership | user_id, email, name, account_id, account_name, roles, plans, verified, disabled_in_account, created_at, last_login |
| `accounts.csv` | account | account_id, name, parent_account_id, parent_account_name, is_reseller, created_at, user_count, plans |
| `plan_assignments.csv` | plan assignment | plan_name, plan_id, account_id, account_name, user_id, user_email, created_at, expires_at, expired, level, assignment_id |
| `users_without_plan.csv` | membership with no active plan | user_id, email, name, account_id, account_name, roles, expired_plans, created_at, last_login |
| `changes.csv` | change since the baseline | change_type, entity, id, name_or_email, account_id, account_name, field, before, after |
| `login_events.csv` | login event | timestamp, result, action, user_email, user_id, account_id, account_name, ip, user_agent, severity, description |

`last_login` is a single value per user, not per account. Frontegg doesn't return a per-account last login.

#### The "users without a plan" rule

A membership (user U in account A) is listed when there is **no** plan assignment that meets all of these:

- it belongs to account A;
- it is either account-level (no user) or a user-level assignment for U;
- it has no expiry date, or expires after the run started.

The `expired_plans` column shows the plans that did apply but have expired.

- **Plans are not inherited through the account hierarchy.** Frontegg doesn't document any such inheritance. An assignment on a parent account doesn't cover its sub-accounts.
- **Plans granted by targeting rules aren't visible.** A plan can also grant access through targeting rules or a default treatment, with no assignment record, so people covered only that way appear in this file. The run summary warns when any plan uses rules.

### Run status and exit codes

Every run ends as one of three statuses:

| Status | Exit code | Meaning |
|---|---|---|
| `succeeded` | 0 | Everything was read. |
| `partial` | 2 | Every core list was read, but some per-account calls failed (role lookups, hierarchy trees, login events). The files are written, and `summary.json` lists each failure with its section, account, HTTP status, `frontegg-trace-id` and what to do. A partial run doesn't become the comparison baseline unless you pass `--use-as-baseline`. |
| `failed` | 1 | The run couldn't authenticate, or a core list (users, accounts, plans, plan assignments) couldn't be read completely. No export files are written, because a half-read list would look like mass deletions. Another export already running also counts as failed. |

Usage errors exit with 64.

### What changed

After each run, `changes.csv` and `summary.json` compare it with the baseline: the last succeeded run, or a partial run you accepted. They report:

- users added, removed or changed (email, name, verified);
- account memberships added, removed or disabled, and roles per account;
- accounts added, removed, renamed or moved in the hierarchy;
- plan assignments added or removed, and expiry changes.

Last-login and created/updated timestamps are ignored. "Users who logged in since" is reported as a count only.

The comparison never reports unknown data as a change:

- roles aren't compared for a membership whose lookup failed in either run;
- moves aren't compared under a hierarchy tree that couldn't be read;
- a section exported in only one of the runs is listed as not compared.

Compare any two runs with:

```bash
python3 -m frontegg_data_export diff --from previous --to latest --csv changes.csv
```

### JSON snapshot (schemaVersion 2.0)

`snapshot.json` has these top-level keys:

| Key | Contents |
|---|---|
| `schemaVersion` | `"2.0"` |
| `app` | app name and version |
| `run` | id, status, trigger, preset, sections, timestamps, base URL, vendor ID, rate, API calls, retries, errors, 429s, rate-limit headers seen, last trace ID, per-step stats |
| `sections` | per-section status: `ok`, `partial` or `unavailable`, with a reason |
| `failures` | `{section, tenantId, traceId, httpStatus, message, hint}` |
| `counts` | record counts |
| `data` | the raw API arrays: `tenants`, `users`, `roles`, `permissions`, `plans`, `features`, `featureFlags`, `hierarchyTrees`, `userRoleAssignments`, `entitlements`, `loginEvents` |

Changes from 1.0:

- `exportRun` became `run`;
- the arrays moved under `data`;
- `sections` and `failures` are new;
- each user gains `tenantRoles`, as in the users-only script. A failed lookup gives `roles: null` and `lookupFailed: true`.

## Command line

```
python3 -m frontegg_data_export run [--preset users|standard|full] [--sections A,B]
                                    [--roles | --no-roles] [--login-events] [--since DATE]
                                    [--format csv,json] [--out DIR] [--rate RATE] [--keep N]
                                    [--quiet] [--progress console|jsonl|quiet] [--use-as-baseline]
python3 -m frontegg_data_export estimate [--probe] [--json] [selection options]
python3 -m frontegg_data_export diff [--from RUN] [--to RUN] [--csv FILE] [--json]
python3 -m frontegg_data_export runs [--json]
python3 -m frontegg_data_export test-connection [--json]
```

`RUN` is a run ID, a run folder, or one of `latest`, `previous` or `baseline`.

## Rate limits and speed

Frontegg documents these [rate limits](https://developers.frontegg.com/ciam/guides/env-settings/rate-limits):

- **General limit:** 100 requests per minute per IP on the Launch plan, and 1,000 per minute per IP on Scale and Enterprise.
- **Lower per-endpoint limits**, counted **per vendor** and therefore shared with every other caller using your environment. For example, `GET /identity/resources/users/v3` allows 60/min on Launch, 100 on Scale and 200 on Enterprise.

`--rate` sets requests per second:

| Rate | Per second | Per minute | How it compares |
|---|---|---|---|
| `gentle` | 1.5 | 90 | fits under the Launch plan's limit |
| `normal` (default) | 4 | 240 | about a quarter of the Scale/Enterprise budget |
| `fast` | 12 | 720 | leaves little room for anything else on the same IP |

You can also pass any number from 0.5 to 16.

On top of the rate, the tool keeps the users listing under 50/min and the accounts listing under 30/min. If a rate-limit header says the window is used up, it waits for the reset. It retries 429s and 5xx responses with backoff.

An export shares the per-IP budget with any production traffic from the same IP. Run it off-peak or from a different machine.

Rough call counts for a medium environment (5,000 users, 5,000 accounts, 5,000 plan assignments). Real runs are latency-bound at about 3 requests per second:

| Preset | API calls | Time at `normal` |
|---|---|---|
| Users only, `--no-roles` | about 55 (one per 200 users, plus account names) | about 1 minute |
| Users only | about 3,000 | about 17 minutes |
| Users, accounts and plans | about 3,600 | about 20 minutes |
| Full backup | about 3,610 | about 20 minutes |

`estimate` gives a figure for your own environment. It uses the previous run's figures, or record counts on the first run (`--probe`).

## Login events

`--login-events` reads successful and failed logins from Frontegg's audit log (`GET /audits/resources/audits/v2`).

**Calls and date range**
- The audit API is per account, so this costs about one extra call per account that has users. The estimate includes it.
- By default it reads everything since the previous succeeded run, but never more than 30 days back (`--login-events-max-days`). `--since 2026-10-01` sets the start explicitly.

**How rows are classified**
- Frontegg doesn't document the action names it uses for logins, so rows are classified by matching the action text: `login`, `logged in` or `authenticated` mark a login, and `fail`, `invalid`, `denied` or `locked` mark a failure.
- The raw `action` is kept in the CSV so you can check the classification.
- The terms can be changed with `loginEventTerms` and `loginFailureTerms` in `data/settings.json`.

**When it isn't available**
- Audit logs are listed as an Enterprise feature. If Frontegg refuses the first request, the section is marked unavailable with a plain-language reason, and the rest of the export carries on.

## Frontegg API quirks the tool handles

Several Frontegg API behaviours don't match the public docs cleanly. Each is handled, and commented, next to the code that deals with it in `frontegg_data_export/fetch.py` and `client.py`:

1. **`_offset` on `/tenants/v2` and `/users/v3` is a page index**, not an item offset. `_limit=200&_offset=200` returns empty (page 200 doesn't exist) rather than items 200 onward. The valid range is `_offset=0..(totalPages-1)`.
2. **Parameter naming is inconsistent across endpoints.** Some use `_limit/_offset` (underscored), others `limit/offset`. Using the wrong one returns a stale, cached, small response with no error:
   - `_limit/_offset`: `/tenants/v2`, `/users/v3`, `/feature-flags/v1`
   - `limit/offset`: `/plans/v1`, `/plans/v1/enriched`, `/features/v1`, `/entitlements/v2`
3. **`/entitlements/v2` silently caps `limit` at 10.** `limit=11` returns 0 items. The tool pages by 10 until a page is empty, or until the documented `hasNext` is `false`.
4. **`/plans/v1` (non-enriched) returns 10 records at most**, with no sign that more exist. Use `/plans/v1/enriched` for the complete catalog; it also gives per-plan `assignedTenantsCount` and `assignedUsersCount`.
5. **`/features/v1` caps `limit` at 100** and returns 400 above that.
6. **`/tenants/hierarchy/v1*` endpoints require a `frontegg-tenant-id` header** even with a vendor token. Without it you get 403 "Tenant ID is not specified". They answer relative to that tenant.
7. **Only tenants with `isReseller: true` have non-trivial hierarchy trees**; other tenants are flat. Capturing the full hierarchy is O(reseller count), not O(tenant count). Tenants don't carry a `parentTenantId` field, so the hierarchy can't be derived from the tenant list alone.
8. **`/identity/resources/users/v3/roles` is a batched lookup, not a listing.** It takes `ids=<csv>` of up to 250 user IDs per call and is tenant-scoped via the header. A cross-tenant header returns an empty `[]`.
9. **Roles are not embedded in the bulk `/users/v3` response.** Its `tenants[]` only carries `{tenantId, isDisabled, temporaryExpirationDate}`, even though the API reference lists `roles[]` there, so per-tenant role lookups are still needed. As a bonus, `/identity/resources/vendor-only/users/v1/{userId}` *does* return a single user with `tenants[].roles[]` inflated; that's useful for spot-checks but too slow for bulk export.
10. **`x-rate-limit-*` headers appear only on some `/identity/*` endpoints**, and not consistently. The tool counts the ones it sees, and pauses when one reports the window is used up. Otherwise it relies on its own throttle.
11. **The vendor token TTL is `expiresIn: 86400` (24 h).** The tool re-authenticates before expiry, and once on a 401.
12. **A URL with 250 user UUIDs (about 9 KB) can trigger HTTP 414 "URI Too Large"** on tenants with many users. The tool chunks at 100 IDs per call, and halves a chunk if a 414 still happens.
13. **Per-endpoint rate limits are per vendor, not per IP**, and lower than the general limit. For example, the users listing allows 60–200/min depending on plan.
14. **`/tenants/resources/hierarchy/v1/tree` returns 400 for a circular hierarchy.** That tree is recorded as a failure (the run is partial) instead of aborting the export.
15. **The audit-log API (`/audits/resources/audits/v2`) is tenant-scoped and pages by item.** It uses `count` (max 200) plus an item `offset`, unlike the page-index `_offset` of `/users/v3`. The action names for logins aren't documented.
16. **`expirationDate` can come without a timezone** (`2022-01-01T12:00:00`, as in the API reference). It is treated as UTC.

## Security

- The API key gives full management access to the environment. Frontegg's docs describe the token as having access to all resources in your Frontegg environment. This tool only reads, but store the key carefully and rotate it if it leaks.
- The key and access tokens are never written to logs, outputs or the console. Every log line passes through a redactor, and a test checks the whole output folder for a known fake key.
- Exports contain personal data (names, emails, login history). Keep the output folder somewhere access-controlled, and use retention (`--keep`).

## Development

```bash
python3 -m unittest            # standard library only; runs against a local mock of the Frontegg API
python3 tools/mock_server.py   # run the mock by hand (prints a base URL, Client ID and API key)
```

Tests run in CI on macOS, Windows and Linux with Python 3.10 to 3.13.
