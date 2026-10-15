"""HTTP contract pins: exact status codes, headers, and JSON bodies, key order included.

Tests reach the app only through HTTP and seed rows with plain sqlite3 after
startup has created the schema, so they survive changes behind the routes.
"""
import fcntl
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app

PROFILE = {
    "languages": {"python": 1.0},
    "frameworks": {"fastapi": 0.5},
    "skills": {"systems": 2.0},
    "keywords": {"software": 3.0, "engineer": 1.0},
    "score_saturation": 4.0,
}


def days_ago(days):
    # Fixed timestamps would drift across the trailing 7-day analytics window.
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


RECENT = days_ago(1)
STALE = days_ago(8)

LISTED = {
    "a1": {"id": "a1", "company": "Acme", "title": "Software Engineer", "url": "https://acme.example/a1", "location": "New York, NY", "is_remote": 0, "track": "full_time", "term": "Entry", "domain": "SWE", "source": "scout", "date_posted": "2026-09-30", "date_discovered": "2026-10-01 10:00:00", "status": "new", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 100},
    "b2": {"id": "b2", "company": "Globex", "title": "Systems Engineer", "url": "https://globex.example/b2", "location": "Remote", "is_remote": 1, "track": "internship", "term": "Summer 2027", "domain": "Systems", "source": "github", "date_posted": None, "date_discovered": "2026-10-03 10:00:00", "status": "new", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 75},
    "c3": {"id": "c3", "company": "Initech", "title": "Data Analyst", "url": "https://initech.example/c3", "location": "Austin, TX", "is_remote": 0, "track": "full_time", "term": None, "domain": "General", "source": "scout", "date_posted": None, "date_discovered": "2026-10-02 10:00:00", "status": "new", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 0},
    "d4": {"id": "d4", "company": "Hooli", "title": "Software Engineer Intern", "url": "https://hooli.example/d4", "location": "Seattle, WA", "is_remote": 0, "track": "internship", "term": "Summer 2027", "domain": "SWE", "source": "github", "date_posted": None, "date_discovered": "2026-10-04 10:00:00", "status": "new", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 100},
    "g7": {"id": "g7", "company": "Stark", "title": "Software Engineer", "url": "https://stark.example/g7", "location": "Boston, MA", "is_remote": 0, "track": "full_time", "term": None, "domain": "SWE", "source": "curated", "date_posted": None, "date_discovered": "2026-10-04 10:00:00", "status": "new", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 100},
    "e5": {"id": "e5", "company": "Pied Piper", "title": "Software Developer", "url": "https://piedpiper.example/e5", "location": "Remote", "is_remote": 1, "track": "full_time", "term": None, "domain": "SWE", "source": "scout", "date_posted": None, "date_discovered": "2026-09-20 10:00:00", "status": "applied", "applied_at": RECENT, "summary": None, "tailored_bullets": None, "match_score": 75},
    "f6": {"id": "f6", "company": "Umbrella", "title": "ML Engineer", "url": "https://umbrella.example/f6", "location": "Denver, CO", "is_remote": 0, "track": "full_time", "term": None, "domain": "AI/ML", "source": "github", "date_posted": None, "date_discovered": "2026-09-15 10:00:00", "status": "applied", "applied_at": STALE, "summary": None, "tailored_bullets": None, "match_score": 25},
    "h8": {"id": "h8", "company": "Wayne", "title": "Backend Engineer", "url": "https://wayne.example/h8", "location": "Chicago, IL", "is_remote": 0, "track": "full_time", "term": None, "domain": "Systems", "source": "scout", "date_posted": None, "date_discovered": "2026-09-10 10:00:00", "status": "saved", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 75},
    "i9": {"id": "i9", "company": "Wonka", "title": "Python Developer", "url": "https://wonka.example/i9", "location": "Remote", "is_remote": 1, "track": "internship", "term": "Fall 2027", "domain": "AI/ML", "source": "scout", "date_posted": None, "date_discovered": "2026-09-05 10:00:00", "status": "archived", "applied_at": None, "summary": None, "tailored_bullets": None, "match_score": 25},
}

SEED_COLUMNS = (
    "id", "company", "title", "url", "location", "is_remote", "track", "term", "domain",
    "source", "date_posted", "date_discovered", "status", "applied_at", "summary",
    "tailored_bullets", "raw_description",
)

A1_DETAIL = {
    "id": "a1", "company": "Acme", "title": "Software Engineer", "url": "https://acme.example/a1",
    "location": "New York, NY", "is_remote": 0, "track": "full_time", "term": "Entry", "domain": "SWE",
    "source": "scout", "date_posted": "2026-09-30", "date_discovered": "2026-10-01 10:00:00",
    "status": "new", "applied_at": None, "raw_description": "Posting a1", "summary": None,
    "tailored_bullets": None, "outreach_note": None, "match_score": 100,
    "match": {
        "matched_terms": {"software": 3.0, "engineer": 1.0},
        "missing_terms": {"python": 1.0, "fastapi": 0.5, "systems": 2.0},
        "matched_weight": 4.0,
    },
}

A1_SUMMARY = (
    "Software Engineer at Acme is a software engineering full-time role based in New York, NY. "
    "Focus your prep on clean API design and testing discipline."
)
A1_TALKING_POINTS = [
    "Why Acme: connect one concrete thing about the company or product to your goals.",
    "Walk through a feature you built end-to-end, from design doc to production.",
    "Describe how you keep code maintainable: tests, reviews, and small PRs.",
    "Ask a sharp question about how the software engineering team measures success.",
]
A1_BULLETS = [
    "Delivered clean API design on a project relevant to software engineering, with measurable impact.",
    "Demonstrated code review while collaborating across a team.",
    "Applied shipping features end-to-end to ship reliable, production-ready work.",
]


@pytest.fixture
def client(settings):
    settings.profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
    with TestClient(app.app) as client:
        rows = [
            [{**job, "raw_description": f"Posting {job['id']}"}[c] for c in SEED_COLUMNS]
            for job in LISTED.values()
        ]
        with closing(sqlite3.connect(settings.db_path)) as conn, conn:
            conn.executemany(
                f"INSERT INTO jobs ({', '.join(SEED_COLUMNS)}) VALUES ({', '.join('?' * len(SEED_COLUMNS))})",
                rows,
            )
        yield client


def assert_body(response, status, expected):
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.json() == expected
    assert json.dumps(response.json()) == json.dumps(expected), "key order differs"


def listed(*ids):
    return [LISTED[i] for i in ids]


def utc_age(timestamp):
    stamped = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamped


def test_list_defaults_to_new_jobs_by_score_then_newest_then_id(client):
    assert_body(client.get("/api/jobs"), 200, listed("d4", "g7", "a1", "b2", "c3"))


def test_list_sort_date_desc_keeps_fetch_order(client):
    assert_body(
        client.get("/api/jobs?sort=date_desc&status=all"), 200,
        listed("d4", "g7", "b2", "c3", "a1", "e5", "f6", "h8", "i9"),
    )


def test_list_all_values_disable_every_filter(client):
    assert_body(
        client.get("/api/jobs?track=all&domain=all&status=all&source=all"), 200,
        listed("d4", "g7", "a1", "b2", "e5", "h8", "f6", "i9", "c3"),
    )


@pytest.mark.parametrize("query,ids", [
    ("track=internship", ["d4", "b2"]),
    ("track=internship&status=all", ["d4", "b2", "i9"]),
    ("domain=Systems", ["b2"]),
    ("domain=AI/ML&status=all", ["f6", "i9"]),
    ("status=applied", ["e5", "f6"]),
    ("status=saved", ["h8"]),
    ("status=archived", ["i9"]),
    ("source=github", ["d4", "b2"]),
    ("source=curated", ["g7"]),
    ("search=hooli", ["d4"]),
    ("search=ANALYST", ["c3"]),
    ("search=%20remote%20&status=all", ["b2", "e5", "i9"]),
    ("search=nowhere", []),
    ("track=full_time&domain=SWE&source=scout&status=all", ["a1", "e5"]),
])
def test_list_filters(client, query, ids):
    assert_body(client.get(f"/api/jobs?{query}"), 200, listed(*ids))


def test_list_pages_after_ranking(client):
    assert_body(client.get("/api/jobs?status=all&limit=3&offset=2"), 200, listed("a1", "b2", "e5"))
    assert_body(client.get("/api/jobs?status=all&limit=2&offset=8"), 200, listed("c3"))


def test_detail_has_full_row_and_match_breakdown(client):
    assert_body(client.get("/api/jobs/a1"), 200, A1_DETAIL)


def test_detail_unknown_job_is_404(client):
    assert_body(client.get("/api/jobs/ghost"), 404, {"detail": "Job not found"})


def test_stats(client):
    assert_body(client.get("/api/stats"), 200, {
        "total": 9, "internship": 3, "full_time": 6, "swe": 4,
        "systems": 2, "aiml": 2, "applied": 2, "saved": 1,
    })


def test_analytics_with_no_lock_file_reports_scraper_not_running(client):
    assert_body(client.get("/api/analytics"), 200, {
        "scraper_running": False, "inbox": 5, "applied": 2, "saved": 1, "archived": 1,
        "applied_last_7_days": 1, "last_scraped": "2026-10-04 10:00:00",
    })


def test_analytics_while_worker_holds_lock_reports_scraper_running(client, settings):
    with open(settings.lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.get("/api/analytics").json()["scraper_running"] is True
    assert client.get("/api/analytics").json()["scraper_running"] is False


@pytest.mark.parametrize("query,loc,kind,msg,value,ctx", [
    ("limit=0", "limit", "greater_than_equal", "Input should be greater than or equal to 1", "0", {"ge": 1}),
    ("offset=-1", "offset", "greater_than_equal", "Input should be greater than or equal to 0", "-1", {"ge": 0}),
    ("limit=5001", "limit", "less_than_equal", "Input should be less than or equal to 5000", "5001", {"le": 5000}),
])
def test_list_rejects_out_of_range_paging(client, query, loc, kind, msg, value, ctx):
    assert_body(client.get("/api/jobs?" + query), 422, {"detail": [
        {"type": kind, "loc": ["query", loc], "msg": msg, "input": value, "ctx": ctx},
    ]})


def test_list_unknown_sort_falls_back_to_score_order(client):
    assert_body(client.get("/api/jobs?sort=bogus"), 200, listed("d4", "g7", "a1", "b2", "c3"))


def test_status_update_requires_status_field(client):
    assert_body(client.post("/api/jobs/a1/status", json={}), 422, {"detail": [
        {"type": "missing", "loc": ["body", "status"], "msg": "Field required", "input": {}},
    ]})


def test_status_update_body(client):
    assert_body(
        client.post("/api/jobs/c3/status", json={"status": "saved"}), 200,
        {"ok": True, "job_id": "c3", "status": "saved"},
    )
    assert_body(client.get("/api/jobs?status=saved"), 200, listed("h8") + [{**LISTED["c3"], "status": "saved"}])


def test_applying_stamps_applied_at_and_other_statuses_clear_it(client):
    client.post("/api/jobs/a1/status", json={"status": "applied"})
    applied = client.get("/api/jobs/a1").json()
    assert applied["status"] == "applied"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", applied["applied_at"])
    assert timedelta(0) <= utc_age(applied["applied_at"]) < timedelta(minutes=1)
    assert client.get("/api/analytics").json()["applied_last_7_days"] == 2

    client.post("/api/jobs/a1/status", json={"status": "saved"})
    assert client.get("/api/jobs/a1").json()["applied_at"] is None
    client.post("/api/jobs/e5/status", json={"status": "archived"})
    assert client.get("/api/jobs/e5").json()["applied_at"] is None
    assert client.get("/api/analytics").json()["applied_last_7_days"] == 0


def test_status_update_rejects_unknown_status(client):
    assert_body(
        client.post("/api/jobs/a1/status", json={"status": "nope"}), 400,
        {"detail": "Invalid status: nope"},
    )
    assert client.get("/api/jobs/a1").json()["status"] == "new"


def test_status_update_unknown_job_is_404(client):
    assert_body(
        client.post("/api/jobs/ghost/status", json={"status": "applied"}), 404,
        {"detail": "Job not found"},
    )


@pytest.mark.parametrize("path,body", [
    ("/api/jobs/a1/status", {"status": "saved"}),
    ("/api/jobs/a1/prep", None),
])
def test_locked_database_returns_retryable_503(client, settings, path, body):
    with closing(sqlite3.connect(settings.db_path)) as held:
        held.execute("BEGIN IMMEDIATE")
        response = client.post(path, json=body)
        held.rollback()
    assert_body(response, 503, {"detail": "Database busy; retry shortly"})
    assert response.headers["retry-after"] == "1"
    assert_body(client.get("/api/jobs/a1"), 200, A1_DETAIL)


def test_prep_body_and_persistence(client):
    assert_body(client.post("/api/jobs/a1/prep"), 200, {
        "ok": True, "job_id": "a1", "summary": A1_SUMMARY,
        "talking_points": A1_TALKING_POINTS, "bullets": A1_BULLETS,
    })
    detail = client.get("/api/jobs/a1").json()
    assert detail["summary"] == A1_SUMMARY
    assert detail["tailored_bullets"] == (
        "- Delivered clean API design on a project relevant to software engineering, with measurable impact.\n"
        "- Demonstrated code review while collaborating across a team.\n"
        "- Applied shipping features end-to-end to ship reliable, production-ready work."
    )


def test_prep_unknown_job_is_404(client):
    assert_body(client.post("/api/jobs/ghost/prep"), 404, {"detail": "Job not found"})


def test_index_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.text.startswith("<!DOCTYPE html>")
