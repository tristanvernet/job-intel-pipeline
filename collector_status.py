"""Shared, JSON-serializable collector outcome vocabulary."""
import re
from typing import Protocol, TypedDict


class JobPayload(TypedDict, total=False):
    id: str
    company: str
    title: str
    url: str
    location: str
    is_remote: bool
    track: str
    term: str | None
    domain: str
    source: str
    date_posted: str
    raw_description: str


class RejectedJob(ValueError):
    """A row that violates a storage rule; the collector counts it and continues."""


class JobSink(Protocol):
    def insert_job(self, job: JobPayload) -> bool:
        """True for a new row, False for a duplicate or a row without title or url.

        Raises RejectedJob for integrity violations. Any other exception is an
        infrastructure failure that ends the source.
        """


def failure_status(exc):
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return "blocked" if code in (403, 429) or re.search(r"\b(?:403|429)\b", str(exc)) else "failed"


def summarize(outcomes):
    states = [item["status"] for item in outcomes]
    if not states or all(state == "ok" for state in states):
        status = "ok"
    elif any(state in ("ok", "partial") for state in states):
        status = "partial"
    elif "blocked" in states:
        status = "blocked"
    else:
        status = "failed"
    return {
        "status": status,
        **{key: sum(item.get(key, 0) for item in outcomes)
           for key in ("fetched", "inserted", "rejected")},
        "sources": outcomes,
    }
