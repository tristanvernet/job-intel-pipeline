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
import matcher
import sync_profile


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


@pytest.mark.parametrize("title", [
    "Internal Software Engineer", "International Operations", "Internet Services",
])
def test_internship_word_boundaries(title):
    assert classify_track(title)[0] != "internship"


@pytest.mark.parametrize("title", [
    "Software Engineer Intern", "Software Engineering Interns", "Software Internship",
    "Software Internships", "Software Co-op", "Software Co op", "Software Coop",
])
def test_undated_internship_has_no_invented_term(title):
    assert classify_track(title) == ("internship", None)


@pytest.mark.parametrize("description,term", [
    ("Summer 2028", "Summer 2028"), ("Spring '27", "Spring 2027"),
    ("Fall 2026", "Fall 2026"), ("Winter ’29", "Winter 2029"),
    ("Summer internship", "Summer"),
    ("Summer opportunities for Summer 2028", "Summer 2028"),
])
def test_internship_term_uses_explicit_evidence(description, term):
    assert classify_track("Software Intern", description) == ("internship", term)


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


def test_closed_parent_sublisting_uses_new_company():
    content = """| Company A | Software Engineer | NY | https://example.com/a |
| Company B | Software Engineer | NY | 🔒 |
| ↳ | Backend Engineer Intern | NY | https://example.com/b |
"""
    jobs, skipped = parse_markdown_table(content, "internship", "Summer 2027")
    assert [(j["company"], j["title"]) for j in jobs] == [
        ("Company A", "Software Engineer"), ("Company B", "Backend Engineer Intern"),
    ]
    assert skipped == 1
    assert jobs[1]["term"] == "Summer 2027"


@pytest.mark.parametrize("separator", ["\n", "\n## Another section\n", "\nIntroductory prose\n"])
def test_markdown_company_context_resets_between_tables(separator):
    content = ("| Company A | Software Engineer | NY | https://example.com/a |\n"
               + separator + "| ↳ | Backend Engineer Intern | NY | https://example.com/b |")
    jobs, skipped = parse_markdown_table(content, "internship", "Summer 2027")
    assert len(jobs) == 1
    assert skipped == 1


def test_html_company_context_and_table_boundaries():
    content = """<table><tr class="job"><td>A</td><td>Software Engineer</td><td>NY</td><td>https://example.com/a</td></tr>
<tr><td>B</td><td>Software Engineer</td><td>NY</td><td>🔒</td></tr>
<tr><td>↳</td><td>Software Intern</td><td>NY</td><td>https://example.com/b</td></tr></table>
<h2>Another section</h2><table><tr><td>↳</td><td>Software Intern</td><td>NY</td><td>https://example.com/c</td></tr></table>"""
    jobs, skipped = parse_markdown_table(content, "internship", "Summer 2027")
    assert [j["company"] for j in jobs] == ["A", "B"]
    assert skipped == 2


def test_explicit_intern_term_overrides_source_hint():
    content = "| A | Software Intern Summer 2028 | NY | https://example.com/a |"
    jobs, _ = parse_markdown_table(content, "internship", "Summer 2027")
    assert jobs[0]["term"] == "Summer 2028"


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


def test_analytics_baseline_counts(client):
    a = client.get("/api/analytics").json()
    # Fixture seeds two 'new' jobs, nothing applied/saved/archived yet.
    assert a["inbox"] == 2
    assert a["applied"] == 0
    assert a["saved"] == 0
    assert a["archived"] == 0
    assert a["applied_last_7_days"] == 0


def test_analytics_reflects_status_changes(client):
    client.post("/api/jobs/job1/status", json={"status": "applied"})
    client.post("/api/jobs/job2/status", json={"status": "saved"})
    a = client.get("/api/analytics").json()
    assert a["inbox"] == 0
    assert a["applied"] == 1
    assert a["saved"] == 1
    assert a["archived"] == 0
    # A fresh application is timestamped now, so it lands in the 7-day window.
    assert a["applied_last_7_days"] == 1


def test_analytics_applied_last_7_days_excludes_old(client):
    """An applied role stamped >7 days ago is counted in totals but not recent."""
    client.post("/api/jobs/job1/status", json={"status": "applied"})
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET applied_at = datetime('now', '-10 days') WHERE id = ?",
            ("job1",),
        )
    a = client.get("/api/analytics").json()
    assert a["applied"] == 1
    assert a["applied_last_7_days"] == 0


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


# --------------------------------------------------------------------------- #
# Match scoring engine (matcher.py)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sample_profile():
    return {
        "languages": {"python": 1.0, "java": 1.0, "c++": 0.6},
        "frameworks": {"fastapi": 0.9, "spring": 0.8},
        "skills": {"backend": 0.9, "systems": 0.8, "machine": 0.7, "learning": 0.7},
        "keywords": {"software": 0.8, "engineer": 0.6, "swe": 0.7, "ml": 0.7, "ai": 0.7},
        "score_saturation": 4.0,
    }


def test_match_score_is_deterministic(sample_profile):
    job = {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"}
    first = matcher.match_score(job, sample_profile)
    for _ in range(25):
        assert matcher.match_score(job, sample_profile) == first


def test_match_score_bounds(sample_profile):
    jobs = [
        {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"},
        {"title": "Barista", "track": "full_time", "domain": "General"},
        {"title": "", "track": "", "domain": ""},
    ]
    for job in jobs:
        s = matcher.match_score(job, sample_profile)
        assert isinstance(s, int)
        assert 0 <= s <= 100


def test_match_score_edge_cases(sample_profile):
    # Empty / None jobs and no-overlap jobs score 0.
    assert matcher.match_score({}, sample_profile) == 0
    assert matcher.match_score(None, sample_profile) == 0
    assert matcher.match_score(
        {"title": "Barista", "track": "full_time", "domain": "General"}, sample_profile
    ) == 0


def test_match_score_relevant_beats_irrelevant(sample_profile):
    relevant = {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"}
    irrelevant = {"title": "Store Clerk", "track": "full_time", "domain": "General"}
    assert matcher.match_score(relevant, sample_profile) > matcher.match_score(irrelevant, sample_profile)


def test_match_score_saturates_at_100(sample_profile):
    # A title dense with high-weight terms cannot exceed 100.
    job = {
        "title": "Python Java Backend Software Engineer FastAPI Systems",
        "track": "full_time",
        "domain": "SWE",
    }
    assert matcher.match_score(job, sample_profile) == 100


def test_match_score_scales_with_saturation(sample_profile):
    job = {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"}
    easy = dict(sample_profile, score_saturation=1.0)
    hard = dict(sample_profile, score_saturation=20.0)
    assert matcher.match_score(job, easy) >= matcher.match_score(job, hard)


def test_match_score_multiword_and_symbol_terms():
    profile = {
        "languages": {"c++": 1.0},
        "frameworks": {},
        "skills": {"machine learning": 1.0},
        "keywords": {},
        "score_saturation": 2.0,
    }
    job = {"title": "C++ Machine Learning Engineer", "track": "full_time", "domain": "AI/ML"}
    assert matcher.match_score(job, profile) == 100
    # A plain 'c' should not match the 'c++' term.
    assert matcher.match_score({"title": "C developer"}, profile) == 0


def test_flatten_terms_takes_max_weight_and_skips_bad():
    profile = {
        "languages": {"python": 0.5},
        "skills": {"python": 0.9, "": 1.0, "bad": "notanumber", "zero": 0},
        "frameworks": {},
        "keywords": {},
    }
    terms = matcher.flatten_terms(profile)
    assert terms["python"] == 0.9        # higher weight wins
    assert "" not in terms and "bad" not in terms and "zero" not in terms


def test_load_profile_missing_returns_default(tmp_path):
    prof = matcher.load_profile(tmp_path / "nope.json")
    assert matcher.match_score(
        {"title": "Python Backend Engineer", "track": "full_time", "domain": "SWE"}, prof
    ) > 0


def test_shipped_profile_json_scores_reasonably():
    prof = matcher.load_profile()  # real profile.json
    strong = {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"}
    weak = {"title": "Store Clerk", "track": "full_time", "domain": "General"}
    # Saturation is intentionally softened (6.0) so entry-level roles spread
    # instead of clustering at 100: a strong role must score clearly high but
    # must NOT saturate, and an irrelevant role must stay at 0.
    strong_score = matcher.match_score(strong, prof)
    assert strong_score >= 50
    assert strong_score < 95
    assert matcher.match_score(weak, prof) == 0


def test_explain_reports_matched_terms(sample_profile):
    job = {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"}
    info = matcher.explain(job, sample_profile)
    assert info["score"] == matcher.match_score(job, sample_profile)
    assert "backend" in info["matched_terms"]
    assert info["matched_weight"] > 0


# --------------------------------------------------------------------------- #
# API integration: Inbox sorts by match score descending
# --------------------------------------------------------------------------- #
def test_api_attaches_match_score(client):
    jobs = client.get("/api/jobs?status=new").json()
    assert all("match_score" in j for j in jobs)
    assert all(0 <= j["match_score"] <= 100 for j in jobs)


def test_api_sorts_by_match_score_desc(client):
    jobs = client.get("/api/jobs?status=new").json()
    scores = [j["match_score"] for j in jobs]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Resume / GitHub profile sync (sync_profile.py) — offline paths only
# --------------------------------------------------------------------------- #
def test_scan_text_for_tech_is_deterministic():
    text = "Built backend services in Python and Java using FastAPI and Spring. "
    a = sync_profile.scan_text_for_tech(text)
    b = sync_profile.scan_text_for_tech(text)
    assert a == b
    assert "python" in a["languages"] and "java" in a["languages"]
    assert "fastapi" in a["frameworks"] and "spring" in a["frameworks"]
    assert "backend" in a["skills"]
    for section in a.values():
        for w in section.values():
            assert 0.0 < w <= 1.0


def test_scan_text_multiword_skill():
    result = sync_profile.scan_text_for_tech("Experience with machine learning pipelines.")
    assert "machine learning" in result["skills"]


def test_merge_sections_keeps_higher_weight():
    base = {"languages": {"python": 0.5}, "frameworks": {}, "skills": {}, "keywords": {}}
    merged = sync_profile.merge_sections(base, {"languages": {"python": 0.9, "java": 0.4}})
    assert merged["languages"]["python"] == 0.9
    assert merged["languages"]["java"] == 0.4
    # Base is not mutated in place.
    assert base["languages"] == {"python": 0.5}


def test_build_profile_from_resume(tmp_path):
    resume = tmp_path / "resume.md"
    resume.write_text("# Resume\nPython and Java backend engineer. FastAPI, Spring, systems.")
    out = tmp_path / "profile.json"
    profile = sync_profile.build_profile(resume=str(resume), profile_path=out)
    assert "python" in profile["languages"]
    assert "java" in profile["languages"]
    sync_profile.save_profile(profile, out)
    assert out.exists()
    # The generated profile scores a matching job above zero.
    assert matcher.match_score(
        {"title": "Backend Software Engineer", "track": "full_time", "domain": "SWE"}, profile
    ) > 0


def test_build_profile_requires_a_source():
    with pytest.raises(ValueError):
        sync_profile.build_profile()


def test_extract_text_missing_file():
    with pytest.raises(FileNotFoundError):
        sync_profile.extract_text_from_resume("/no/such/resume.md")


# --------------------------------------------------------------------------- #
# Regression tests: audit fixes (id collisions, ingestion safety, domain
# word boundaries, worker overlap lock).
# --------------------------------------------------------------------------- #
def test_job_id_distinguishes_symbol_titles():
    """'C++ Engineer' and 'C# Engineer' must not collapse to the same id."""
    id_plus = db.generate_job_id("Acme", "C++ Engineer", "NYC")
    id_sharp = db.generate_job_id("Acme", "C# Engineer", "NYC")
    assert id_plus != id_sharp


def test_job_id_distinguishes_locations():
    """Same title at the same company in different cities = distinct roles."""
    id_nyc = db.generate_job_id("Acme", "Software Engineer", "New York, NY")
    id_sf = db.generate_job_id("Acme", "Software Engineer", "San Francisco, CA")
    assert id_nyc != id_sf


def test_job_id_stable_under_whitespace_and_case():
    a = db.generate_job_id("Acme  Corp ", "Software   Engineer", "Remote")
    b = db.generate_job_id("acme corp", "software engineer", "remote")
    assert a == b


def test_insert_job_missing_title_or_url_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    db.init_db()
    # Must return False cleanly -- never raise KeyError.
    assert db.insert_job({"company": "Acme", "url": "https://x.com/1"}) is False
    assert db.insert_job({"company": "Acme", "title": "SWE"}) is False
    assert db.insert_job({"company": "Acme", "title": "  ", "url": " "}) is False


@pytest.mark.parametrize("title", [
    "Retail Associate",
    "Detail Oriented Coordinator",
    "Html Email Developer",
    "Plaid Software Engineer",
])
def test_classify_domain_no_acronym_false_positives(title):
    assert classify_domain(title) != "AI/ML"


@pytest.mark.parametrize("title", [
    "AI Engineer",
    "Machine Learning Intern",
    "NLP Research Intern",
    "LLM Infrastructure Engineer",
])
def test_classify_domain_true_aiml_hits(title):
    assert classify_domain(title) == "AI/ML"


def test_run_pipeline_exits_gracefully_when_locked(tmp_path, monkeypatch):
    """An active lockfile must stop a second run without scraping anything."""
    import fcntl
    import worker

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(worker, "LOCK_PATH", tmp_path / "test.lock")

    called = []
    monkeypatch.setattr("fetch_github.run_github_fetch",
                        lambda: called.append("github"), raising=False)
    monkeypatch.setattr("scout.run_scout",
                        lambda: called.append("scout"), raising=False)

    with open(tmp_path / "test.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        summary = worker.run_pipeline()

    assert summary["new_count"] == 0
    assert summary.get("skipped") is True
    assert called == []  # no collector ran while the lock was held


# --------------------------------------------------------------------------- #
# Profile sync: monotonicity (manual tuning never downgraded) and idempotence.
# --------------------------------------------------------------------------- #
def _write_resume(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_merge_sections_never_downgrades_existing_weight():
    base = {"languages": {"python": 1.0}, "frameworks": {}, "skills": {},
            "keywords": {}, "score_saturation": 4.0}
    incoming = {"languages": {"python": 0.4, "go": 0.7}}
    merged = sync_profile.merge_sections(base, incoming)
    assert merged["languages"]["python"] == 1.0   # hand-tuned weight kept
    assert merged["languages"]["go"] == 0.7       # new term still added


def test_build_profile_is_idempotent(tmp_path):
    """Re-syncing the same resume must yield the identical profile."""
    resume = _write_resume(
        tmp_path / "resume.txt",
        "Python python python FastAPI backend engineer. Docker and SQL.",
    )
    profile_path = tmp_path / "profile.json"
    first = sync_profile.build_profile(resume=resume, profile_path=profile_path)
    sync_profile.save_profile(first, profile_path)
    second = sync_profile.build_profile(resume=resume, profile_path=profile_path)
    assert first == second


def test_build_profile_preserves_manual_weights(tmp_path):
    """A hand-tuned high weight must survive a sync that detects a lower one."""
    resume = _write_resume(tmp_path / "resume.txt", "Some python experience.")
    profile_path = tmp_path / "profile.json"
    sync_profile.save_profile(
        {"name": "Candidate Profile",
         "languages": {"python": 1.0},
         "frameworks": {}, "skills": {}, "keywords": {},
         "score_saturation": 4.0},
        profile_path,
    )
    updated = sync_profile.build_profile(resume=resume, profile_path=profile_path)
    assert updated["languages"]["python"] == 1.0
