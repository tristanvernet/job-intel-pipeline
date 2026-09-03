"""Deterministic, offline test suite for the job-intel pipeline.

Covers: markdown/parser logic, classifier rules, scout payload mapping, and the
FastAPI endpoints (against a throwaway temp DB). No network is ever touched.
"""
import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from classifier import is_unwanted_role, classify_track, classify_domain
from fetch_github import parse_markdown_table
from scout import row_to_payload


# --------------------------------------------------------------------------- #
# Classifier rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Software Engineering Manager",
    "Principal Backend Engineer",
    "Mechanical Engineer",
    "Software Engineer II",
    "Marketing Associate",
])
def test_is_unwanted_role_rejects(title):
    assert is_unwanted_role(title) is True


@pytest.mark.parametrize("title", [
    "Software Engineer Intern",
    "Junior Software Engineer",
    "Entry Level Backend Developer",
])
def test_is_unwanted_role_accepts_early_career(title):
    assert is_unwanted_role(title) is False


def test_classify_track_internship_with_term():
    track, term = classify_track("Software Engineer Intern", "Summer 2027 internship")
    assert track == "internship"
    assert term == "Summer 2027"


def test_classify_track_full_time_entry():
    track, term = classify_track("Junior Software Engineer")
    assert track == "full_time"
    assert term == "New Grad / Entry Level"


def test_classify_track_unclear():
    track, term = classify_track("Software Engineer")
    assert track == "unclear"
    assert term is None


@pytest.mark.parametrize("title,expected", [
    ("Machine Learning Intern", "AI/ML"),
    ("Platform Engineer", "Systems"),
    ("Backend Software Engineer", "SWE"),
    ("Barista", None),
])
def test_classify_domain(title, expected):
    assert classify_domain(title) == expected


# --------------------------------------------------------------------------- #
# GitHub markdown parser
# --------------------------------------------------------------------------- #
SAMPLE_MD = """
Some intro text.

| Company | Role | Location | Application | Age |
| ------- | ---- | -------- | ----------- | --- |
| **[Acme 🚀](https://acme.com)** | Software Engineer Intern | 🌆 New York, NY | <a href="https://apply.acme.com/swe"><img src="x.png"></a> | 1d |
| ↳ | Backend Engineering Intern 🛂 | Remote | <a href="https://apply.acme.com/be">Apply</a> | 1d |
| **[Globex](https://globex.com)** | Senior Software Engineer | Boston, MA | [Apply](https://globex.com/sr) | 2d |
| **[Initech](https://initech.com)** | Machine Learning Intern | Austin, TX | 🔒 | 3d |
| **[Umbrella](https://umbrella.com)** | Mechanical Engineer | Denver, CO | [Apply](https://umbrella.com/me) | 4d |
| **[Hooli](https://hooli.com)** | New Grad Software Engineer | Palo Alto, CA | [Apply](https://hooli.com/ng) | 5d |
"""


def test_parse_markdown_basic_and_links():
    payloads, skipped = parse_markdown_table(SAMPLE_MD, "internship", "Summer 2027")
    by_title = {p["title"]: p for p in payloads}

    # Acme intern parsed, emoji stripped from company + location
    acme = by_title["Software Engineer Intern"]
    assert acme["company"] == "Acme"
    assert acme["url"] == "https://apply.acme.com/swe"
    assert "🚀" not in acme["company"]
    assert acme["track"] == "internship"


def test_parse_markdown_sublisting_inherits_company():
    payloads, _ = parse_markdown_table(SAMPLE_MD, "internship", "Summer 2027")
    subs = [p for p in payloads if p["title"] == "Backend Engineering Intern"]
    assert len(subs) == 1
    assert subs[0]["company"] == "Acme"            # inherited via ↳
    assert subs[0]["is_remote"] is True
    assert "🛂" not in subs[0]["title"]


def test_parse_markdown_skips_senior_locked_and_offtrack():
    payloads, skipped = parse_markdown_table(SAMPLE_MD, "internship", "Summer 2027")
    companies = {p["company"] for p in payloads}
    assert "Globex" not in companies      # senior -> excluded
    assert "Initech" not in companies     # locked/closed -> skipped
    assert "Umbrella" not in companies    # mechanical -> excluded
    assert "Hooli" in companies           # new grad SWE -> kept
    assert skipped >= 3


# --------------------------------------------------------------------------- #
# Scout payload mapping
# --------------------------------------------------------------------------- #
def test_row_to_payload_valid():
    row = {
        "title": "Software Engineer Intern",
        "company": "TestCorp",
        "job_url": "https://x.com/1",
        "location": "Remote",
        "is_remote": True,
        "site": "indeed",
        "description": "Summer 2027 internship",
    }
    p = row_to_payload(row, "internship")
    assert p is not None
    assert p["domain"] == "SWE"
    assert p["track"] == "internship"
    assert p["source"] == "indeed"


def test_row_to_payload_filters_senior():
    assert row_to_payload({"title": "Senior Software Engineer"}, "full_time") is None


def test_row_to_payload_uses_default_track_when_unclear():
    row = {"title": "Software Engineer", "company": "C", "job_url": "u"}
    p = row_to_payload(row, "full_time")
    assert p is not None and p["track"] == "full_time"


def test_row_to_payload_rejects_unknown_domain():
    assert row_to_payload({"title": "Barista"}, "full_time") is None


# --------------------------------------------------------------------------- #
# FastAPI endpoints (temp DB)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_jobs.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)

    import app as app_module
    importlib.reload(app_module)          # rebind get_connection under patched path
    monkeypatch.setattr(app_module, "DB_PATH", test_db)

    db.init_db()
    db.insert_job({
        "id": "job1", "company": "Acme", "title": "Software Engineer Intern",
        "url": "https://acme.com/1", "location": "New York, NY", "is_remote": False,
        "track": "internship", "term": "Summer 2027", "domain": "SWE",
        "source": "test", "raw_description": "desc",
    })
    db.insert_job({
        "id": "job2", "company": "Globex", "title": "Junior Systems Engineer",
        "url": "https://globex.com/2", "location": "Remote", "is_remote": True,
        "track": "full_time", "term": "Entry", "domain": "Systems",
        "source": "test", "raw_description": "desc",
    })
    with TestClient(app_module.app) as c:
        yield c


def test_migration_adds_status_check(tmp_path, monkeypatch):
    """A legacy table with no status CHECK is safely upgraded, rows preserved."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("""CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
        location TEXT, is_remote INTEGER, track TEXT NOT NULL, term TEXT, domain TEXT,
        source TEXT, date_posted TEXT, date_discovered TEXT, status TEXT, raw_description TEXT,
        summary TEXT, tailored_bullets TEXT, outreach_note TEXT)""")
    conn.execute("INSERT INTO jobs (id,company,title,url,track,status) VALUES (?,?,?,?,?,?)",
                 ("a", "C", "T", "u", "internship", "weird_legacy_status"))
    conn.commit(); conn.close()

    monkeypatch.setattr(db, "DB_PATH", legacy)
    db.init_db()
    with db.get_connection() as c:
        schema = c.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
        assert "check(statusin(" in schema.lower().replace(" ", "")
        # legacy unknown status normalized to 'new'
        assert c.execute("SELECT status FROM jobs WHERE id='a'").fetchone()[0] == "new"


def test_list_jobs(client):
    jobs = client.get("/api/jobs?status=new").json()
    assert len(jobs) == 2


def test_list_jobs_filter_track_and_domain(client):
    assert len(client.get("/api/jobs?track=internship&status=new").json()) == 1
    assert len(client.get("/api/jobs?domain=Systems&status=new").json()) == 1


def test_list_jobs_search_param(client):
    res = client.get("/api/jobs?status=new&search=globex").json()
    assert len(res) == 1 and res[0]["company"] == "Globex"


def test_stats(client):
    s = client.get("/api/stats").json()
    assert s["total"] == 2 and s["internship"] == 1 and s["swe"] == 1 and s["systems"] == 1


def test_update_status_and_tabs(client):
    r = client.post("/api/jobs/job1/status", json={"status": "saved"})
    assert r.status_code == 200
    assert len(client.get("/api/jobs?status=saved").json()) == 1
    assert len(client.get("/api/jobs?status=new").json()) == 1


def test_update_status_invalid(client):
    assert client.post("/api/jobs/job1/status", json={"status": "nope"}).status_code == 400


def test_update_status_missing_job(client):
    assert client.post("/api/jobs/ghost/status", json={"status": "applied"}).status_code == 404


def test_prep_endpoint_persists(client):
    r = client.post("/api/jobs/job1/prep")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] and body["bullets"] and body["talking_points"]
    # persisted to summary + tailored_bullets
    saved = client.get("/api/jobs?status=new&search=Acme").json()[0]
    assert saved["summary"] == body["summary"]
    assert saved["tailored_bullets"]


def test_prep_missing_job(client):
    assert client.post("/api/jobs/ghost/prep").status_code == 404
