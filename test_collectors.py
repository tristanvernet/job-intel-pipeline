"""Collectors write through a JobSink; these tests drive them with an in-memory one."""
import ast
from pathlib import Path

import pandas as pd
import pytest

import db
import fetch_github
import scout
from collector_status import RejectedJob


class MemorySink:
    def __init__(self, reject=(), fail=(), error=RuntimeError("disk I/O error")):
        self.reject, self.fail, self.error = set(reject), set(fail), error
        self.rows = {}
        self.attempts = 0

    def insert_job(self, job):
        self.attempts += 1
        if job["company"] in self.fail:
            raise self.error
        if job["company"] in self.reject:
            raise RejectedJob("CHECK constraint failed")
        if job["url"] in self.rows:
            return False
        self.rows[job["url"]] = job
        return True


def scout_row(company, title="Software Engineer", url="https://example.com/a"):
    return dict(title=title, company=company, job_url=url, site="indeed")


def test_scout_ingest_counts_each_row_outcome():
    frame = pd.DataFrame([
        scout_row("Acme"),
        scout_row("Acme"),
        scout_row("Bad", url="https://example.com/b"),
        scout_row("Acme", title="Senior Software Engineer", url="https://example.com/c"),
        scout_row("Acme", url="javascript:alert(1)"),
    ])
    sink = MemorySink(reject={"Bad"})
    counters = {}
    scout._ingest(frame, "full_time", counters, sink)
    assert counters == {"fetched": 5, "added": 1, "skipped": 1, "duplicates": 1, "rejected": 2}
    assert list(sink.rows) == ["https://example.com/a"]
    assert sink.rows["https://example.com/a"]["track"] == "full_time"


@pytest.mark.parametrize("error", [RuntimeError("disk I/O error"), ValueError("disk I/O error"), TypeError("disk I/O error")])
def test_scout_sink_failure_ends_the_source(monkeypatch, error):
    monkeypatch.setattr(scout, "SEARCH_QUERIES", [{"query": "q", "default_track": "full_time"}])
    monkeypatch.setattr(scout, "SITES", ["indeed"])
    monkeypatch.setattr(scout.time, "sleep", lambda _: None)
    frame = pd.DataFrame([scout_row("Broken"), scout_row("Acme", url="https://example.com/b")])
    monkeypatch.setattr(scout, "scrape_jobs", lambda **kw: frame)
    sink = MemorySink(fail={"Broken"}, error=error)
    report = scout.run_scout(sink)
    assert report["status"] == "failed"
    assert [(s["source"], s["status"], s["error"], s["inserted"]) for s in report["sources"]] == [
        ("indeed", "failed", "disk I/O error", 0),
        ("linkedin", "failed", "disk I/O error", 0),
    ]
    assert sink.attempts == 2
    assert sink.rows == {}


GITHUB_TABLE = "\n".join([
    "| Company | Role | Location | Application |",
    "| --- | --- | --- | --- |",
    '| Acme | Software Engineer Intern | Remote | <a href="https://acme.example/1">Apply</a> |',
    '| Acme | Software Engineer Intern | Remote | <a href="https://acme.example/1">Apply</a> |',
    '| Bad | Software Engineer Intern | Remote | <a href="https://bad.example/1">Apply</a> |',
    '| Acme | Marketing Intern | Remote | <a href="https://acme.example/2">Apply</a> |',
    "| Acme | Software Engineer Intern | Remote | no link |",
])


@pytest.fixture
def github_source(monkeypatch):
    class Response:
        status_code = 200
        text = GITHUB_TABLE
    monkeypatch.setattr(fetch_github, "SOURCES", [
        {"url": "https://example.com/list.md", "default_track": "internship", "term_hint": "Summer 2027"},
    ])
    monkeypatch.setattr(fetch_github.requests, "get", lambda url, timeout: Response())


def test_github_fetch_counts_each_row_outcome(github_source):
    sink = MemorySink(reject={"Bad"})
    assert fetch_github.run_github_fetch(sink) == [{
        "source": "https://example.com/list.md", "ok": False, "status": "partial",
        "fetched": 5, "inserted": 1, "added": 1, "rejected": 1, "skipped": 2,
        "error": "CHECK constraint failed",
    }]
    assert list(sink.rows) == ["https://acme.example/1"]


@pytest.mark.parametrize("error", [RuntimeError("disk I/O error"), ValueError("disk I/O error"), TypeError("disk I/O error")])
def test_github_sink_failure_ends_the_source(github_source, error):
    sink = MemorySink(fail={"Acme"}, error=error)
    assert fetch_github.run_github_fetch(sink) == [{
        "source": "https://example.com/list.md", "ok": False, "status": "failed",
        "fetched": 5, "inserted": 0, "added": 0, "rejected": 0, "skipped": 2,
        "error": "disk I/O error",
    }]
    assert sink.attempts == 1


def test_github_fetch_through_the_real_db_counts_a_check_violation_as_rejected(github_source, monkeypatch, store):
    def payloads(content, default_track, term_hint, stats=None):
        rows = [
            {"company": "Acme", "title": "Software Engineer Intern", "url": "https://acme.example/1",
             "track": "internship", "domain": "SWE"},
            {"company": "Bad", "title": "Software Engineer Intern", "url": "https://bad.example/1",
             "track": "internship", "domain": "Marketing"},
            {"company": "Globex", "title": "Software Engineer Intern", "url": "https://globex.example/1",
             "track": "internship", "domain": "SWE"},
        ]
        stats.update(total_rows=3, kept=3, skipped=0, reasons={})
        return rows, 0
    monkeypatch.setattr(fetch_github, "parse_markdown_table", payloads)
    store.init()
    [outcome] = fetch_github.run_github_fetch(store)
    assert (outcome["status"], outcome["inserted"], outcome["rejected"]) == ("partial", 2, 1)
    assert "CHECK constraint failed" in outcome["error"]
    assert len(store.job_ids()) == 2


@pytest.mark.parametrize("field", ["track", "domain"])
def test_db_check_violation_raises_rejected_job(field, store):
    store.init()
    job = dict(id="one", company="Example", title="Software Engineer",
               url="https://example.com/job", track="full_time", domain="SWE")
    with pytest.raises(RejectedJob):
        store.insert_job({**job, field: "invalid"})
    assert store.insert_job(job) is True


def _is_main_guard(node):
    test = getattr(node, "test", None)
    return (isinstance(node, ast.If) and isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "__main__")


def imported_modules(path):
    tree = ast.parse(Path(__file__).with_name(path).read_text())
    # The `__main__` block is the script's composition root; only the collector code must stay storage-free.
    body = [node for node in tree.body if not _is_main_guard(node)]
    names = set()
    for node in (child for top in body for child in ast.walk(top)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("path", ["scout.py", "fetch_github.py"])
def test_collectors_do_not_import_storage(path):
    modules = imported_modules(path)
    assert "collector_status" in modules
    assert modules.isdisjoint({"db", "sqlite3"})
