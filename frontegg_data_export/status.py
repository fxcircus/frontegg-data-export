"""Run status, the failure list, and exit codes.

A run is:
- "failed"    when we couldn't authenticate, or a core list (users, accounts,
              plans, plan assignments) couldn't be read completely. A partial
              core list would look like mass deletions in the diff, so it is
              never treated as usable data.
- "partial"   when every core list is complete but some per-account calls
              failed (role lookups, hierarchy trees, login events).
- "succeeded" otherwise. A section that is unavailable to this environment
              (for example, login events not included in its Frontegg plan)
              doesn't make a run partial; it's reported as unavailable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .client import ApiError

SUCCEEDED, PARTIAL, FAILED = "succeeded", "partial", "failed"
EXIT_CODES = {SUCCEEDED: 0, FAILED: 1, PARTIAL: 2}
EXIT_USAGE = 64
EXIT_INTERRUPTED = 130


@dataclass
class Failure:
    section: str
    message: str
    tenant_id: str | None = None
    trace_id: str = ""
    http_status: int | None = None
    hint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return {"section": d["section"], "tenantId": d["tenant_id"], "traceId": d["trace_id"],
                "httpStatus": d["http_status"], "message": d["message"], "hint": d["hint"]}

    @classmethod
    def from_error(cls, section: str, err: ApiError, tenant_id: str | None = None) -> "Failure":
        return cls(section=section, message=err.message, tenant_id=tenant_id, trace_id=err.trace_id,
                   http_status=err.status, hint=hint_for(section, err))


class CoreSectionFailed(Exception):
    """A core list couldn't be read; the run is failed."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def overall_status(failures: list[Failure], core_failed: bool) -> str:
    if core_failed:
        return FAILED
    return PARTIAL if failures else SUCCEEDED


def hint_for(section: str, err: ApiError) -> str:
    """What the person running the export can do about it."""
    s = err.status
    trace = f" (trace ID {err.trace_id})" if err.trace_id else ""
    if s is None:
        return "Check this computer's internet connection, then run the export again."
    if s in (401, 403):
        return ("The API key was refused for this request. Check that it's the environment's API key from "
                "Keys & domains in the Frontegg Portal, then run the export again.")
    if s == 400 and section == "hierarchy":
        return ("Frontegg couldn't build this account's hierarchy (it may contain a loop). Check the account's "
                "sub-accounts in the Frontegg Portal, or contact Frontegg support" + trace + ".")
    if s == 429:
        return "Frontegg kept rate-limiting the export. Run it again with a gentler speed, or off-peak."
    if 500 <= s <= 599:
        return ("Frontegg had a server error and retries didn't help. Run the export again later. If it keeps "
                "happening, contact Frontegg support" + trace + ".")
    return "Run the export again. If it keeps happening, contact Frontegg support" + trace + "."
