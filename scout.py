import random
import time
import sqlite3
from urllib.parse import urlsplit

import pandas as pd
from pandas.api.types import is_scalar
from collector_status import failure_status, summarize

from jobspy import scrape_jobs
from db import insert_job
from classifier import is_unwanted_role, classify_track, classify_domain

# Primary boards that tolerate steady scraping.
SITES = ["indeed", "glassdoor", "zip_recruiter"]
# LinkedIn is aggressive about rate limits, so it is fetched separately with a
# smaller cap and random back-off, and isolated in try/except.
LINKEDIN_LIMIT = 15

# Expanded query variations across tracks and domains.
SEARCH_QUERIES = [
    # Track 1: Full-Time Entry Level — SWE
    {"query": '"Entry Level Software Engineer"', "default_track": "full_time"},
    {"query": '"Junior Software Engineer"', "default_track": "full_time"},
    {"query": '"Associate Software Engineer"', "default_track": "full_time"},
    {"query": '"New Grad Software Engineer" 2027', "default_track": "full_time"},
    {"query": '"Software Engineer I"', "default_track": "full_time"},
    {"query": '"Graduate Software Developer"', "default_track": "full_time"},
    # Track 1: Full-Time Entry Level — Systems / AI-ML
    {"query": '"Junior Systems Engineer"', "default_track": "full_time"},
    {"query": '"Entry Level Platform Engineer"', "default_track": "full_time"},
    {"query": '"Associate Machine Learning Engineer"', "default_track": "full_time"},
    {"query": '"New Grad Data Engineer"', "default_track": "full_time"},
    # Track 2: Internships — SWE
    {"query": '"Software Engineer Intern" 2027', "default_track": "internship"},
    {"query": '"Software Engineering Intern"', "default_track": "internship"},
    {"query": '"Backend Engineering Intern"', "default_track": "internship"},
    {"query": '"Full Stack Intern"', "default_track": "internship"},
    # Track 2: Internships — Systems / AI-ML
    {"query": '"Machine Learning Intern"', "default_track": "internship"},
    {"query": '"AI Research Intern"', "default_track": "internship"},
    {"query": '"Systems Intern"', "default_track": "internship"},
    {"query": '"Infrastructure Engineering Intern"', "default_track": "internship"},
]


def row_to_payload(row: dict, default_track: str = "unclear"):
    """Convert one jobspy result row into a job payload, or None if filtered.

    Pure function: no network, no DB. `row` is a plain dict (e.g. a DataFrame
    row converted via `.to_dict()`), so it is trivially testable with fixtures.
    """
    clean = {}
    for key, value in row.items():
        if not is_scalar(value):
            raise ValueError(f"Non-scalar field: {key}")
        clean[key] = None if pd.isna(value) else value
    row = clean
    title = str(row.get("title") or "").strip()
    if not title:
        raise ValueError("Missing title")

    # Gate 1: excluded seniority / non-tech disciplines
    if is_unwanted_role(title):
        return None

    # Gate 2: must match a known domain
    domain = classify_domain(title)
    if not domain:
        return None

    description = str(row.get("description") or "")
    track, term = classify_track(title, description)
    if track == "unclear":
        # Trust the query's intended track before discarding.
        if default_track in ("full_time", "internship"):
            track = default_track
        else:
            return None

    url = str(row.get("job_url") or "").strip()
    remote = row.get("is_remote")
    if remote is None:
        remote = False
    elif isinstance(remote, str):
        if remote.lower() not in ("true", "false"):
            raise ValueError("Invalid remote flag")
        remote = remote.lower() == "true"
    elif remote not in (True, False):
        raise ValueError("Invalid remote flag")

    return {
        "company": str(row.get("company") or "Unknown").strip() or "Unknown",
        "title": title,
        "url": url,
        "location": str(row.get("location") or "USA").strip(),
        "is_remote": bool(remote),
        "track": track,
        "term": term,
        "domain": domain,
        "source": str(row.get("site") or "jobspy"),
        "date_posted": str(row.get("date_posted") or ""),
        "raw_description": description,
    }


def validate_payload(payload):
    """Validate at the ingestion boundary; conversion remains independently usable."""
    url = payload["url"]
    parsed = urlsplit(url)
    if (not url or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in url)
            or parsed.scheme not in ("http", "https") or not parsed.hostname):
        raise ValueError("Missing or invalid job URL")
    _ = parsed.port  # reject malformed ports as well
    if payload["track"] not in ("full_time", "internship", "unclear"):
        raise ValueError("Invalid track")
    if payload["domain"] not in ("SWE", "Systems", "AI/ML", "General"):
        raise ValueError("Invalid domain")


def _ingest(jobs_df, default_track: str, counters: dict):
    """Convert and insert a jobspy result frame; update shared counters."""
    if jobs_df is None or jobs_df.empty:
        return
    for key in ("fetched", "added", "skipped", "duplicates", "rejected"):
        counters.setdefault(key, 0)
    counters["fetched"] += len(jobs_df)
    for _, row in jobs_df.iterrows():
        try:
            payload = row_to_payload(row.to_dict(), default_track)
            if payload is None:
                counters["skipped"] += 1
                continue
            validate_payload(payload)
            if insert_job(payload):
                counters["added"] += 1
            else:
                counters["duplicates"] += 1
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            counters["rejected"] += 1
            print(f"  Rejected row: {exc}")
        # Infrastructure failures (including exhausted BUSY) escape to the
        # source boundary; they are not mislabeled as malformed rows.


def run_scout(results_per_query: int = 10):
    outcomes = []
    blocked_sites = set()
    for item in SEARCH_QUERIES:
        for site in [*SITES, "linkedin"]:
            if site in blocked_sites:
                continue
            counters = dict.fromkeys(("fetched", "added", "skipped", "duplicates", "rejected"), 0)
            result = {"source": site, "query": item["query"], "status": "ok", "error": None}
            try:
                options = dict(site_name=[site], search_term=item["query"],
                               location="United States", country_indeed="USA",
                               results_wanted=LINKEDIN_LIMIT if site == "linkedin" else results_per_query,
                               hours_old=168 if site == "linkedin" else 72)
                if site == "linkedin":
                    time.sleep(random.uniform(2.0, 5.0))
                    options["linkedin_fetch_description"] = False
                jobs_df = scrape_jobs(**options)
                _ingest(jobs_df, item["default_track"], counters)
                if counters["rejected"]:
                    result["status"] = "partial"
            except Exception as exc:
                result.update(status=failure_status(exc), error=str(exc))
                if result["status"] == "blocked":
                    blocked_sites.add(site)  # no repeated attempts after 403/429
                elif counters["added"]:
                    result["status"] = "partial"
                print(f"  [{site}] {result['status']}: {exc}")
            result.update(counters, inserted=counters["added"])
            outcomes.append(result)
            time.sleep(random.uniform(1.0, 3.0))
    report = summarize(outcomes)
    print(f"Scout complete: {report}")
    return report


if __name__ == "__main__":
    run_scout(results_per_query=6)
