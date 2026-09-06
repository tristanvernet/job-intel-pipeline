"""Shared, JSON-serializable collector outcome vocabulary."""
import re


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
