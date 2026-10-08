"""Orchestrates one export run: the steps for the chosen sections, the run
status, and the outputs. The CLI, the scheduler and the local app all come
through here."""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, csvout
from .client import ApiError, AuthError, FronteggClient
from .config import ConfigError, Credentials, load_credentials, load_settings, output_dir, parse_rate
from .diff import CHANGES_HEADER, diff_models, first_run_summary, summary_lines
from .fetch import (
    PAGE_SIZE_TENANTS,
    PAGE_SIZE_USERS,
    PERMISSIONS_PATH,
    PLANS_PATH,
    ROLES_PATH,
    TENANTS_PATH,
    USERS_PATH,
    pull_entitlements,
    pull_feature_flags,
    pull_features,
    pull_hierarchy,
    pull_pages_by_pageindex,
    pull_plans,
    SectionUnavailable,
    pull_account_level_roles,
    pull_login_events,
    pull_user_role_assignments,
    users_by_tenant,
    walk_tree,
)
from .loginevents import DEFAULT_FAILURE_TERMS, DEFAULT_LOGIN_TERMS, DEFAULT_MAX_DAYS, classify, date_range
from .logs import RunLog
from .model import build_model, iso_z, parse_ts
from .progress import Reporter
from .sections import STEP_LABELS, Counts, Estimate, Selection, estimate, resolve
from .status import EXIT_CODES, EXIT_INTERRUPTED, FAILED, PARTIAL, SUCCEEDED, CoreSectionFailed, Failure, overall_status
from .snapshot import build_snapshot, counts_for
from .store import DEFAULT_KEEP, Busy, Store, atomic_write_json, read_json


class Run:
    def __init__(self, selection: Selection, base_url: str, client_id: str, secret: str, *,
                 rate: float, reporter: Reporter, store: Store, keep: int = DEFAULT_KEEP,
                 trigger: str = "manual", use_as_baseline: bool = False,
                 formats: tuple[str, ...] = ("csv", "json"), since: datetime | None = None,
                 login_events_max_days: int = DEFAULT_MAX_DAYS,
                 login_terms: tuple[str, ...] = DEFAULT_LOGIN_TERMS,
                 failure_terms: tuple[str, ...] = DEFAULT_FAILURE_TERMS,
                 client_factory: Callable[..., FronteggClient] = FronteggClient) -> None:
        self.selection = selection
        self.reporter = reporter
        self.store = store
        self.keep = keep
        self.trigger = trigger
        self.use_as_baseline = use_as_baseline
        self.run_id = ""
        self.run_dir: Path | None = None
        self.files: list[str] = []
        self.formats = tuple(formats)
        self.rows: dict[str, int] = {}
        self.notes: list[str] = []
        self.since = since
        self.login_events_max_days = login_events_max_days
        self.login_terms = login_terms
        self.failure_terms = failure_terms
        self.client = client_factory(base_url, client_id, secret, rate=rate)
        reporter.api_calls = lambda: self.client.calls
        self.base_url = self.client.base_url
        self.rate = rate
        self.started_at = datetime.now(timezone.utc)
        self.failures: list[Failure] = []
        self.data: dict[str, Any] = {}
        self.step_stats: dict[str, dict] = {}
        self.failed_role_tenants: list[str] = []
        self.failed_roots: list[str] = []
        self.status = ""
        self.section_notes: dict[str, dict] = {}
        self.model: dict | None = None
        self.changes: dict | None = None

    # ---- driving ----------------------------------------------------------
    def execute(self) -> int:
        try:
            with self.store.lock():
                return self._execute_locked()
        except Busy as e:
            self.reporter.error(str(e))
            self.reporter.run_finished(FAILED, "Export not started", str(e), exitCode=EXIT_CODES[FAILED], busy=True)
            return EXIT_CODES[FAILED]

    def _execute_locked(self) -> int:
        self.run_id, self.run_dir = self.store.create_run_dir(self.started_at)
        log = RunLog(self.run_dir / "run.log")
        self.reporter.set_log(log)
        self.client.log = log
        try:
            return self._execute()
        except KeyboardInterrupt:
            self.failures.append(Failure("run", "Stopped before the export finished."))
            self.status = FAILED
            self._finish_failed(interrupted=True)
            return EXIT_INTERRUPTED
        except Exception as e:  # never leave a run without a summary
            log("Unexpected error:\n" + traceback.format_exc(), "ERROR")
            self.failures.append(Failure("run", f"Unexpected error: {e!r}",
                                         hint="Run the export again. If it keeps happening, report it "
                                              "with this run's run.log."))
            self.status = FAILED
            return self._finish_failed()
        finally:
            log.close()

    def _execute(self) -> int:
        r = self.reporter
        sel = self.selection
        r.run_started(
            "Frontegg Data Export",
            f"started {self.started_at.isoformat(timespec='seconds')}  base={self.base_url}",
            runId=self.run_id, preset=sel.preset, sections=list(sel.sections), rate=self.rate,
            baseUrl=self.base_url, outputDir=str(self.run_dir))
        r.info(f"Exporting: {sel.label} ({', '.join(sel.sections)})")
        r.info(f"Saving to: {self.run_dir}")
        r.info("Read-only: this tool only reads from Frontegg and changes nothing there.")
        steps = [s for s in sel.steps if s != "write"]
        try:
            for i, step in enumerate(steps, 1):
                r.step(step, STEP_LABELS[step], i, len(sel.steps))
                calls0, t0 = self.client.calls, datetime.now(timezone.utc)
                count = getattr(self, f"_step_{step}")()
                self.step_stats[step] = {
                    "calls": self.client.calls - calls0,
                    "seconds": round((datetime.now(timezone.utc) - t0).total_seconds(), 2),
                    "count": count,
                }
        except CoreSectionFailed as e:
            self.failures.append(e.failure)
            self.status = FAILED
            return self._finish_failed()
        self.status = overall_status(self.failures, core_failed=False)
        r.step("write", STEP_LABELS["write"], len(sel.steps), len(sel.steps))
        self._write_outputs()
        self._compare_with_baseline()
        return self._finish()

    def _core(self, section: str, fn: Callable[[], Any]) -> Any:
        """A core list fetch; any API failure fails the whole run."""
        try:
            return fn()
        except ApiError as e:
            raise CoreSectionFailed(Failure.from_error(section, e)) from None

    # ---- steps (each returns a record count) ------------------------------
    def _step_auth(self) -> int:
        try:
            self.client.authenticate()
        except AuthError as e:
            raise CoreSectionFailed(Failure.from_error("auth", e)) from None
        self.reporter.step_done("auth", "Got an access token (valid about 24 hours).")
        return 0

    def _step_role_catalog(self) -> int:
        roles = self._core("roles", lambda: self.client.get(ROLES_PATH))
        self.data["roles"] = roles or []
        self.reporter.step_done("role_catalog", f"Roles: {len(self.data['roles'])}")
        return len(self.data["roles"])

    def _step_catalog(self) -> int:
        self.data["permissions"] = self._core("permissions", lambda: self.client.get(PERMISSIONS_PATH)) or []
        self.data["features"] = self._core("features", lambda: pull_features(self.client))
        self.data["featureFlags"] = self._core("featureFlags", lambda: pull_feature_flags(self.client))
        self.reporter.step_done("catalog", f"Permissions: {len(self.data['permissions'])}, features: "
                                f"{len(self.data['features'])}, feature flags: {len(self.data['featureFlags'])}")
        return len(self.data["permissions"]) + len(self.data["features"]) + len(self.data["featureFlags"])

    def _step_plan_catalog(self) -> int:
        self.data["plans"] = self._core("plans", lambda: pull_plans(self.client))
        self.reporter.step_done("plan_catalog", f"Plans: {len(self.data['plans'])}")
        return len(self.data["plans"])

    def _step_accounts(self) -> int:
        self.data["tenants"] = self._core("accounts", lambda: pull_pages_by_pageindex(
            self.client, TENANTS_PATH, PAGE_SIZE_TENANTS, "accounts", self.reporter, "accounts"))
        self.reporter.step_done("accounts", f"Accounts: {len(self.data['tenants'])}")
        return len(self.data["tenants"])

    def _step_hierarchy(self) -> int:
        trees, self.failed_roots = pull_hierarchy(self.client, self.data["tenants"], self.failures, self.reporter)
        self.data["hierarchyTrees"] = trees
        in_tree = {n.get("tenantId") for t in trees for n in walk_tree(t) if n.get("tenantId")}
        self.reporter.step_done("hierarchy", f"Hierarchy trees: {len(trees)}, accounts in a hierarchy: {len(in_tree)}")
        return len(trees)

    def _step_users(self) -> int:
        self.data["users"] = self._core("users", lambda: pull_pages_by_pageindex(
            self.client, USERS_PATH, PAGE_SIZE_USERS, "users", self.reporter, "users"))
        self.reporter.step_done("users", f"Users: {len(self.data['users'])}")
        return len(self.data["users"])

    def _step_roles(self) -> int:
        rows, self.failed_role_tenants = pull_user_role_assignments(
            self.client, self.data["users"], self.failures, self.reporter)
        self.data["userRoleAssignments"] = rows
        known = {r["id"] for r in self.data.get("roles") or [] if r.get("id")}
        extra = pull_account_level_roles(self.client, rows, known, self.reporter)
        self.data.setdefault("roles", []).extend(extra)
        more = f", plus {len(extra)} role(s) defined by individual accounts" if extra else ""
        self.reporter.step_done("roles", f"Role assignments: {len(rows)}{more}")
        return len(rows)

    def _step_entitlements(self) -> int:
        self.data["entitlements"] = self._core("entitlements",
                                               lambda: pull_entitlements(self.client, self.reporter))
        self.reporter.step_done("entitlements", f"Plan assignments: {len(self.data['entitlements'])}")
        return len(self.data["entitlements"])

    def _last_success_started(self) -> datetime | None:
        ok = [r for r in self.store.load_history()["runs"] if r.get("status") == SUCCEEDED]
        return parse_ts(ok[-1].get("startedAt")) if ok else None

    def _step_login_events(self) -> int:
        start, end, capped = date_range(self.started_at, self._last_success_started(), self.since,
                                        self.login_events_max_days)
        accounts = sorted(users_by_tenant(self.data["users"]))
        window = f"{iso_z(start)} to {iso_z(end)}"
        if capped:
            self.reporter.info(f"Login events: limited to the last {self.login_events_max_days} days ({window}).")
        else:
            self.reporter.info(f"Login events: {window}, for {len(accounts)} account(s) with users.")
        try:
            events, stats = pull_login_events(
                self.client, accounts, start, end, self.failures, self.reporter,
                lambda row: classify(row, self.login_terms, self.failure_terms))
        except SectionUnavailable as e:
            self.section_notes["login_events"] = {"status": "unavailable", "reason": e.reason,
                                                  "httpStatus": e.error.status, "traceId": e.error.trace_id}
            self.reporter.warn(f"Login events are unavailable: {e.reason} The rest of the export continues.",
                               step="login_events", traceId=e.error.trace_id)
            return 0
        self.data["loginEvents"] = events
        self.section_notes["login_events"] = {
            "status": "partial" if stats["failedAccounts"] else "ok",
            "from": iso_z(start), "to": iso_z(end), "capped": capped, **stats}
        self.reporter.step_done("login_events", f"Login events: {stats['success']} successful, "
                                f"{stats['failure']} failed ({stats['rowsScanned']} audit rows read)")
        return len(events)

    # ---- outputs ----------------------------------------------------------
    def _vendor_id(self) -> str | None:
        for key in ("roles", "tenants", "users"):
            for item in self.data.get(key) or []:
                if item.get("vendorId"):
                    return item["vendorId"]
        return None

    def _section_status(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for sec in self.selection.sections:
            entry: dict[str, Any] = {"status": "ok"}
            if sec == "roles" and self.failed_role_tenants:
                entry = {"status": "partial", "failedAccounts": len(self.failed_role_tenants)}
            if sec == "hierarchy" and self.failed_roots:
                entry = {"status": "partial", "failedTrees": len(self.failed_roots)}
            entry.update(self.section_notes.get(sec, {}))
            out[sec] = entry
        return out

    def _run_meta(self) -> dict:
        c = self.client
        ended_at = datetime.now(timezone.utc)
        return {
            "id": self.run_id,
            "status": self.status,
            "trigger": self.trigger,
            "preset": self.selection.preset,
            "sections": list(self.selection.sections),
            "rolesIncluded": self.selection.has("roles"),
            "startedAt": self.started_at.isoformat(timespec="seconds"),
            "endedAt": ended_at.isoformat(timespec="seconds"),
            "durationSeconds": int((ended_at - self.started_at).total_seconds()),
            "baseUrl": self.base_url,
            "vendorId": self._vendor_id(),
            "rateLimitPerSecond": self.rate,
            "apiCalls": c.calls,
            "retries": c.retries,
            "errors": c.errors,
            "rateLimit429s": c.h429,
            "rateLimitHeadersSeen": c.rate_limit_headers_seen,
            "lastTraceId": c.last_trace_id,
            "failedRoleLookupTenants": self.failed_role_tenants,
            "failedHierarchyRoots": self.failed_roots,
            "steps": self.step_stats,
        }

    def _write_outputs(self) -> None:
        self.model = build_model(self.data, sections=self.selection.sections, run_started_at=self.started_at,
                                 failed_role_tenants=self.failed_role_tenants, failed_roots=self.failed_roots)
        atomic_write_json(self.run_dir / "normalized.json", self.model, indent=None)
        self.files.append("normalized.json")
        if "csv" in self.formats:
            self.rows = csvout.write_all(self.run_dir, self.model, self.selection.sections,
                                         login_events=self.data.get("loginEvents"))
            self.files.extend(self.rows)
            self.reporter.step_done("write", "Wrote " + ", ".join(f"{n} ({r} rows)" for n, r in self.rows.items()))
        if "json" in self.formats:
            snapshot = build_snapshot(self._run_meta(), self._section_status(),
                                      [f.to_dict() for f in self.failures], self.data, self.failed_role_tenants)
            self._write_json("snapshot.json", snapshot)
            size = (self.run_dir / "snapshot.json").stat().st_size / 1_048_576
            self.reporter.step_done("write", f"Wrote snapshot.json ({size:.1f} MB)")
        if self.model["has"]["accounts"]:
            orphans = [(uid, tid) for uid, u in self.model["users"].items() for tid in u["memberships"]
                       if tid not in self.model["accounts"]]
            if orphans:
                note = (f"{len(orphans)} membership(s) point at {len({t for _, t in orphans})} account(s) that "
                        "Frontegg's account list doesn't return (probably deleted accounts). They're listed "
                        f"with the account name {csvout.ACCOUNT_NOT_FOUND}.")
                self.notes.append(note)
                self.reporter.info(note)
        rule_plans = sorted(p["name"] for p in self.model["plans"].values() if p["usesRules"])
        if rule_plans and self.selection.has("plans"):
            note = (f"{len(rule_plans)} plan(s) can also grant access through targeting rules or a default "
                    f"treatment ({', '.join(rule_plans[:5])}{', ...' if len(rule_plans) > 5 else ''}). That access "
                    "has no assignment record, so those users may appear in users_without_plan.csv.")
            self.notes.append(note)
            self.reporter.info(note)

    def _compare_with_baseline(self) -> None:
        """Diff this run against the baseline (the last succeeded run, or a
        partial run the user accepted) and write changes.csv."""
        baseline_id = self.store.baseline_id()
        old = read_json(self.store.run_dir(baseline_id) / "normalized.json") if baseline_id else None
        if old is None:
            changes, summary = [], first_run_summary()
            baseline_id = None
        else:
            changes, summary = diff_models(old, self.model)
        self.changes = {"comparedWith": baseline_id, **summary}
        if "csv" in self.formats:
            self.rows["changes.csv"] = csvout.write_csv(self.run_dir / "changes.csv", CHANGES_HEADER,
                                                        (c.row() for c in changes))
            self.files.append("changes.csv")
        if baseline_id is None:
            self.reporter.info(summary["message"])
        else:
            self.reporter.info(f"Compared with {baseline_id}: {summary['total']} change(s).")
            for line in summary_lines(summary):
                self.reporter.info("  " + line)
            for note in summary["notCompared"]:
                self.reporter.info(f"  Not compared: {note}")

    def _write_json(self, name: str, data: Any) -> None:
        atomic_write_json(self.run_dir / name, data)
        self.files.append(name)

    # ---- summary, history, baseline, retention ----------------------------
    def _summary(self, headline: str) -> dict:
        ended_at = datetime.now(timezone.utc)
        c = self.client
        usable = self.status == SUCCEEDED or (self.status == PARTIAL and self.use_as_baseline)
        return {
            "app": {"name": "Frontegg Data Export", "version": __version__},
            "runId": self.run_id,
            "status": self.status,
            "headline": headline,
            "trigger": self.trigger,
            "startedAt": self.started_at.isoformat(timespec="seconds"),
            "endedAt": ended_at.isoformat(timespec="seconds"),
            "durationSeconds": int((ended_at - self.started_at).total_seconds()),
            "preset": self.selection.preset,
            "presetLabel": self.selection.label,
            "sections": list(self.selection.sections),
            "rateLimitPerSecond": self.rate,
            "baseUrl": self.base_url,
            "apiCalls": c.calls,
            "retries": c.retries,
            "errors": c.errors,
            "rateLimit429s": c.h429,
            "rateLimitHeadersSeen": c.rate_limit_headers_seen,
            "lastTraceId": c.last_trace_id,
            "counts": counts_for(self.data) if self.status != FAILED else {},
            "sectionStatus": self._section_status() if self.status != FAILED else {},
            "steps": self.step_stats,
            "failures": [f.to_dict() for f in self.failures],
            "warnings": self.reporter.warnings,
            "notes": self.notes,
            "rows": self.rows,
            "changes": self.changes,
            "files": sorted(set(self.files + ["summary.json", "run.log"])),
            "usableAsBaseline": usable,
        }

    def _record(self, headline: str) -> dict:
        summary = self._summary(headline)
        atomic_write_json(self.run_dir / "summary.json", summary)
        entry = {k: summary[k] for k in ("runId", "status", "trigger", "startedAt", "endedAt", "durationSeconds",
                                          "preset", "presetLabel", "sections", "apiCalls", "counts",
                                          "usableAsBaseline")}
        self.store.record_run(entry, make_baseline=summary["usableAsBaseline"])
        deleted, warnings = self.store.apply_retention(self.keep, protect={self.run_id})
        if deleted:
            self.reporter.info(f"Removed {len(deleted)} old run(s), keeping the last {self.keep}.")
        for w in warnings:
            self.reporter.warn(w)
        return summary

    def _report_failures(self) -> None:
        for f in self.failures:
            where = f" (account {f.tenant_id})" if f.tenant_id else ""
            self.reporter.warn(f"{f.section}{where}: {f.message}", step=f.section, tenantId=f.tenant_id,
                               traceId=f.trace_id)
            if f.hint:
                self.reporter.info(f"  What to do: {f.hint}")

    def _finish(self) -> int:
        c = self.client
        self._report_failures()
        headline = "Export finished" if self.status != PARTIAL else "Export finished, but some data is missing"
        summary = self._record(headline)
        if self.status == PARTIAL and not summary["usableAsBaseline"]:
            self.reporter.info("This partial run won't be used as the comparison baseline for the next run.")
        self.reporter.run_finished(
            self.status, headline,
            f"status={self.status}  duration={summary['durationSeconds']}s  api_calls={c.calls}  "
            f"retries={c.retries}  errors={c.errors}  429s={c.h429}  "
            f"rate_limit_headers_seen={c.rate_limit_headers_seen}",
            exitCode=EXIT_CODES[self.status], runId=self.run_id, outputDir=str(self.run_dir),
            failures=[f.to_dict() for f in self.failures])
        return EXIT_CODES[self.status]

    def _finish_failed(self, interrupted: bool = False) -> int:
        for f in self.failures:
            trace = f" (trace ID {f.trace_id})" if f.trace_id else ""
            self.reporter.error(f"{f.section}: {f.message}{trace}", step=f.section, traceId=f.trace_id)
            if f.hint and f.section != "auth":
                self.reporter.info(f"  What to do: {f.hint}")
        headline = "Export stopped" if interrupted else "Export failed"
        self.files = []                     # a failed run keeps only its summary and log
        summary = self._record(headline)
        self.reporter.run_finished(FAILED, headline,
                                   f"status=failed  duration={summary['durationSeconds']}s  "
                                   "no export files were written",
                                   exitCode=EXIT_CODES[FAILED], runId=self.run_id, outputDir=str(self.run_dir),
                                   failures=[f.to_dict() for f in self.failures])
        return EXIT_CODES[FAILED]


def parse_since(value: str | None) -> datetime | None:
    """`--since`: an ISO 8601 date or date-time (UTC if no zone), or "last"
    (the default: since the previous succeeded run)."""
    if value in (None, "", "last"):
        return None
    dt = parse_ts(value if "T" in value else f"{value}T00:00:00")
    if dt is None:
        raise ValueError(f"--since must be an ISO 8601 date like 2026-10-01 or 2026-10-01T08:00:00Z, or 'last' "
                         f"(got {value!r})")
    return dt


def main(*, preset: str | None = None, sections: list[str] | None = None, roles: bool | None = None,
         login_events: bool | None = None, since: str | None = None, login_events_max_days: int | None = None,
         rate: str | float | None = None, progress: str = "console", out_dir: str | Path | None = None,
         keep: int | None = None, trigger: str = "manual", use_as_baseline: bool = False,
         formats: tuple[str, ...] = ("csv", "json"), credentials: Credentials | None = None) -> int:
    """Run one export. Options left as None come from settings.json."""
    reporter = Reporter(progress)
    try:
        settings = load_settings()
        creds = credentials or load_credentials(settings=settings)
        selection = resolve(None if sections else (preset or settings["preset"]), sections,
                            roles=settings["roles"] if roles is None else roles,
                            login_events=(settings["loginEvents"] if (login_events is None and not sections)
                                          else login_events))
        rate_value = parse_rate(rate if rate is not None else settings["rate"])
        since_dt = parse_since(since)
        max_days = int(login_events_max_days or settings["loginEventsMaxDays"] or DEFAULT_MAX_DAYS)
    except (ConfigError, ValueError) as e:
        reporter.error(str(e))
        return EXIT_CODES[FAILED]
    store = Store(output_dir(out_dir, settings))
    run = Run(selection, creds.base_url, creds.client_id, creds.secret,
              rate=rate_value, reporter=reporter, store=store,
              keep=int(keep if keep is not None else settings["keepRuns"]), trigger=trigger,
              use_as_baseline=use_as_baseline, formats=formats, since=since_dt, login_events_max_days=max_days,
              login_terms=tuple(settings.get("loginEventTerms") or DEFAULT_LOGIN_TERMS),
              failure_terms=tuple(settings.get("loginFailureTerms") or DEFAULT_FAILURE_TERMS),
              client_factory=FronteggClient)
    return run.execute()


# --------------------------------------------------------------------------- #
# Shared helpers for the CLI and the local app
# --------------------------------------------------------------------------- #
def _counts_from_summary(summary: dict) -> Counts:
    c = summary.get("counts") or {}
    steps = summary.get("steps") or {}
    return Counts(users=c.get("users"), accounts=c.get("accounts"), entitlements=c.get("planAssignments"),
                  resellers=c.get("resellerAccounts"), plans=c.get("plans"), features=c.get("features"),
                  accounts_with_users=(steps.get("roles") or {}).get("calls"))


def probe_counts(client: FronteggClient) -> Counts:
    """Three cheap reads that size an environment before its first export."""
    users = client.get(USERS_PATH, {"_limit": 1, "_offset": 0}) or {}
    tenants = client.get(TENANTS_PATH, {"_limit": 1, "_offset": 0}) or {}
    resp = client.get(PLANS_PATH, {"limit": 200, "offset": 0}) or {}
    plans = resp.get("items", []) if isinstance(resp, dict) else []
    return Counts(
        users=((users.get("_metadata") or {}).get("totalItems")),
        accounts=((tenants.get("_metadata") or {}).get("totalItems")),
        entitlements=sum(int(p.get("assignedTenantsCount") or 0) + int(p.get("assignedUsersCount") or 0)
                         for p in plans),
        plans=len(plans))


def estimate_for(selection: Selection, rate: float, store: Store, *, probe: bool = False,
                 credentials: Credentials | None = None) -> Estimate:
    """From the last completed run when there is one; otherwise from record
    counts (cached from an earlier probe, or probed now if allowed)."""
    history = store.load_history()
    done = [r for r in history["runs"] if r.get("status") != FAILED]
    prev_steps, pace, counts = None, None, None
    if done:
        last = read_json(store.run_dir(done[-1]["runId"]) / "summary.json") or {}
        prev_steps = last.get("steps")
        if last.get("durationSeconds") and last.get("apiCalls"):
            pace = max(0.5, last["apiCalls"] / max(1, last["durationSeconds"]))
        counts = _counts_from_summary(last)
    elif history.get("probe"):
        counts = Counts(**history["probe"]["counts"])
    elif probe and credentials:
        client = FronteggClient(credentials.base_url, credentials.client_id, credentials.secret, rate=rate)
        client.authenticate()
        counts = probe_counts(client)
        history["probe"] = {"counts": counts.__dict__, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        store.save_history(history)
    return estimate(selection, rate, counts, previous_steps=prev_steps, previous_pace=pace)


def test_connection(credentials: Credentials,
                    client_factory: Callable[..., FronteggClient] = FronteggClient) -> dict:
    """Get a token, then make one cheap read. Returns a plain-language result."""
    client = client_factory(credentials.base_url, credentials.client_id, credentials.secret, rate=4.0)
    try:
        client.authenticate()
    except AuthError as e:
        return {"ok": False, "step": "token", "message": e.message, "httpStatus": e.status,
                "traceId": e.trace_id, "baseUrl": credentials.base_url}
    try:
        resp = client.get(USERS_PATH, {"_limit": 1, "_offset": 0}) or {}
    except ApiError as e:
        if e.status in (401, 403):
            msg = ("The key was accepted, but it isn't allowed to read users. Use the environment's API key "
                   "from Keys & domains in the Frontegg Portal.")
        else:
            msg = f"Got a token, but reading users failed: {e.message}"
        return {"ok": False, "step": "read", "message": msg, "httpStatus": e.status, "traceId": e.trace_id,
                "baseUrl": credentials.base_url}
    users = (resp.get("_metadata") or {}).get("totalItems")
    found = f" The environment has {users:,} users." if isinstance(users, int) else ""
    return {"ok": True, "step": "done", "message": f"Connected to {credentials.base_url}.{found}",
            "users": users, "traceId": client.last_trace_id, "baseUrl": credentials.base_url}
