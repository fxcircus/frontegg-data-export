"""Orchestrates one export run: the steps for the chosen sections, the run
status, and the outputs. The CLI, the scheduler and the local app all come
through here."""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .client import ApiError, AuthError, FronteggClient
from .config import DOTENV_PATH, default_output_dir, load_config, parse_rate
from .fetch import (
    PAGE_SIZE_TENANTS,
    PAGE_SIZE_USERS,
    PERMISSIONS_PATH,
    ROLES_PATH,
    TENANTS_PATH,
    USERS_PATH,
    pull_entitlements,
    pull_feature_flags,
    pull_features,
    pull_hierarchy,
    pull_pages_by_pageindex,
    pull_plans,
    pull_user_role_assignments,
    walk_tree,
)
from .logs import RunLog
from .progress import Reporter
from .sections import STEP_LABELS, Selection, resolve
from .status import EXIT_CODES, EXIT_INTERRUPTED, FAILED, PARTIAL, SUCCEEDED, CoreSectionFailed, Failure, overall_status
from .store import DEFAULT_KEEP, Busy, Store, atomic_open, atomic_write_json


class Run:
    def __init__(self, selection: Selection, base_url: str, client_id: str, secret: str, *,
                 rate: float, reporter: Reporter, store: Store, keep: int = DEFAULT_KEEP,
                 trigger: str = "manual", use_as_baseline: bool = False,
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
        self.reporter.step_done("roles", f"Role assignments: {len(rows)}")
        return len(rows)

    def _step_entitlements(self) -> int:
        self.data["entitlements"] = self._core("entitlements",
                                               lambda: pull_entitlements(self.client, self.reporter))
        self.reporter.step_done("entitlements", f"Plan assignments: {len(self.data['entitlements'])}")
        return len(self.data["entitlements"])

    # ---- outputs ----------------------------------------------------------
    def _vendor_id(self) -> str | None:
        for key in ("roles", "tenants", "users"):
            for item in self.data.get(key) or []:
                if item.get("vendorId"):
                    return item["vendorId"]
        return None

    def _write_outputs(self) -> None:
        c = self.client
        output = {
            "schemaVersion": "1.0",
            "exportRun": {
                "status": self.status,
                "preset": self.selection.preset,
                "sections": list(self.selection.sections),
                "failures": [f.to_dict() for f in self.failures],
                "failedRoleLookupTenants": self.failed_role_tenants,
                "failedHierarchyRoots": self.failed_roots,
                "startedAt": self.started_at.isoformat(),
                "baseUrl": self.base_url,
                "vendorId": self._vendor_id(),
                "apiCalls": c.calls,
                "errors": c.errors,
                "retries": c.retries,
                "rateLimit429s": c.h429,
                "rateLimitHeadersSeen": c.rate_limit_headers_seen,
                "lastTraceId": c.last_trace_id,
                "steps": self.step_stats,
            },
            "counts": {k: len(v) for k, v in self.data.items()},
            **self.data,
        }
        self._write_json("snapshot.json", output)
        size = (self.run_dir / "snapshot.json").stat().st_size / 1_048_576
        self.reporter.step_done("write", f"Wrote snapshot.json ({size:.1f} MB)")

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
            "counts": {k: len(v) for k, v in self.data.items()} if self.status != FAILED else {},
            "steps": self.step_stats,
            "failures": [f.to_dict() for f in self.failures],
            "warnings": self.reporter.warnings,
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


def main(rate: str | float | None = None, preset: str | None = None, sections: list[str] | None = None,
         roles: bool | None = None, progress: str = "console", out_dir: str | Path | None = None,
         keep: int = DEFAULT_KEEP, trigger: str = "manual", use_as_baseline: bool = False) -> int:
    env = load_config(DOTENV_PATH)
    selection = resolve(preset, sections, roles=roles)
    reporter = Reporter(progress)
    store = Store(Path(out_dir) if out_dir else default_output_dir())
    run = Run(selection, env["FRONTEGG_BASE_URL"], env["FRONTEGG_CLIENT_ID"], env["FRONTEGG_CLIENT_SECRET"],
              rate=parse_rate(rate), reporter=reporter, store=store, keep=keep, trigger=trigger,
              use_as_baseline=use_as_baseline, client_factory=FronteggClient)
    return run.execute()
