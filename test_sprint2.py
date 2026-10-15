"""Fault-injection tests use temporary databases and mocked remote collectors."""
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

import db
import fetch_github
import scout
import worker


@pytest.fixture
def database(store):
    store.init()


def job(id="one"):
    return dict(id=id, company="Example", title="Software Engineer",
                url="https://example.com/job", track="full_time", domain="SWE")


def test_helpers_close_connections(database, monkeypatch, store):
    original = store.connect
    opened = []

    def connect():
        conn = original()
        opened.append(conn)
        return conn

    monkeypatch.setattr(store, "connect", connect)
    store.init()
    assert store.insert_job(job())
    assert not store.insert_job(job())
    assert store.job_ids() == {"one"}
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_connection_setup_failure_closes_handle(monkeypatch, store):
    class BrokenConnection:
        closed = False
        def execute(self, sql):
            raise KeyboardInterrupt()
        def close(self):
            self.closed = True
    conn = BrokenConnection()
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **kw: conn)
    with pytest.raises(KeyboardInterrupt):
        store.connect()
    assert conn.closed


def test_busy_retries_roll_back_whole_transaction(database, monkeypatch, store):
    attempts = []
    delays = []
    monkeypatch.setattr(db.time, "sleep", delays.append)

    def write(conn):
        attempts.append(conn)
        conn.execute("INSERT INTO jobs(id, company, title, url, track) VALUES('retry','C','T','u','full_time')")
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "committed"

    assert store.write(write) == "committed"
    assert len(attempts) == 3
    assert len(delays) == 2
    assert store.job_ids() == {"retry"}
    assert len({id(conn) for conn in attempts}) == 3


def test_nonbusy_error_is_not_retried(database, store):
    attempts = []
    def invalid(conn):
        attempts.append(conn)
        conn.execute("SELECT * FROM nonexistent_table")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store.write(invalid)
    assert len(attempts) == 1


def test_real_wal_contention_retries_until_lock_released(database, monkeypatch, store):
    held = store.connect()
    held.execute("BEGIN IMMEDIATE")
    original_sleep = time.sleep
    delays = []
    def sleep(delay):
        delays.append(delay)
        original_sleep(delay)
    monkeypatch.setattr(db.time, "sleep", sleep)
    timer = threading.Timer(0.25, held.rollback)
    timer.start()
    try:
        assert store.insert_job(job())
        assert delays
    finally:
        timer.join()
        held.close()


def test_migration_failure_rolls_back_schema_and_normalization(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    store = db.JobDB(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, track TEXT, status TEXT)")
        conn.execute("INSERT INTO jobs VALUES('old','C','T','u','full_time','legacy')")
    def fail(conn):
        raise RuntimeError("injected migration failure")
    monkeypatch.setattr(db, "_migrate_applied_at", fail)
    with pytest.raises(RuntimeError):
        store.init()
    with closing(store.connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "legacy"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobs_legacy'").fetchone()


def test_two_processes_migrate_legacy_database_atomically(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, track TEXT, status TEXT)")
        conn.execute("INSERT INTO jobs VALUES('old','C','T','u','full_time','legacy')")
    script = "import db,sys; from pathlib import Path; db.JobDB(Path(sys.argv[1])).init()"
    processes = [subprocess.Popen([sys.executable, "-c", script, str(path)],
                 cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr.decode()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT id,status,applied_at FROM jobs").fetchone() == ("old", "new", None)


@pytest.mark.parametrize("missing", [float("nan"), pd.NA, pd.NaT])
def test_missing_scalars_do_not_become_strings_or_true(missing):
    payload = scout.row_to_payload(dict(title="Software Engineer", job_url=missing,
        is_remote=missing, company=missing, description=missing), "full_time")
    assert payload["url"] == ""
    assert payload["is_remote"] is False
    assert payload["raw_description"] == ""
    with pytest.raises(ValueError):
        scout.validate_payload(payload)


def test_ingestion_rejects_bad_rows_but_keeps_following_valid_rows(database, store):
    frame = pd.DataFrame([
        dict(title="Software Engineer", job_url=pd.NA),
        dict(title="Software Engineer", job_url="https://example.com/a", is_remote=pd.NA),
        dict(title="Software Engineer", job_url="javascript:alert(1)"),
    ])
    counters = {}
    scout._ingest(frame, "full_time", counters, store)
    assert counters["fetched"] == 3
    assert counters["rejected"] == 2
    assert counters["added"] == 1


def test_busy_detection_accepts_extended_sqlite_code():
    exc = sqlite3.OperationalError("busy snapshot")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
    assert db.is_busy(exc)


def test_worker_exit_status_reflects_failed_collection(monkeypatch):
    monkeypatch.setattr(worker, "run", lambda settings, **kw: {"status": "blocked"})
    assert worker.main(["--no-notify"]) == 1


def test_blocked_board_is_not_retried_and_other_sources_continue(monkeypatch, store):
    monkeypatch.setattr(scout, "SEARCH_QUERIES", [{"query": q, "default_track": "full_time"} for q in ("one", "two")])
    monkeypatch.setattr(scout, "SITES", ["indeed"])
    monkeypatch.setattr(scout.time, "sleep", lambda _: None)
    calls = []
    def scrape(**kwargs):
        site = kwargs["site_name"][0]
        calls.append(site)
        if site == "indeed":
            raise RuntimeError("HTTP 429")
        return pd.DataFrame()
    monkeypatch.setattr(scout, "scrape_jobs", scrape)
    report = scout.run_scout(store)
    assert report["status"] == "partial"
    assert calls.count("indeed") == 1
    assert calls.count("linkedin") == 2
    assert report["sources"][0]["status"] == "blocked"


def test_github_block_is_visible_in_worker_and_notifies_failure(database, monkeypatch, settings):
    class Response:
        status_code = 403
    monkeypatch.setattr(fetch_github.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(worker.schedule_mod, "load_config", lambda path: {})
    messages = []
    monkeypatch.setattr(worker.notify, "send_notification", lambda msg: messages.append(msg))
    monkeypatch.setattr(worker.notify, "notify_new_roles", lambda *a, **kw: pytest.fail("false success notification"))
    report = worker.run(settings, skip_scout=True)
    assert report["status"] == "blocked"
    assert report["sources"][0]["sources"][0]["status"] == "blocked"
    assert messages and "blocked" in messages[0]


def test_jobspy_options_never_send_remote_as_location():
    opts = scout._jobspy_options("glassdoor", '"Junior Software Engineer"', results_wanted=5)
    assert opts["location"] == "United States"
    assert opts["location"].lower() != "remote"
    assert opts["is_remote"] is False
    assert opts["country_indeed"] == "USA"


def test_jobspy_options_remote_flag_keeps_country_anchor():
    opts = scout._jobspy_options("glassdoor", '"Junior Software Engineer"',
                                 results_wanted=5, is_remote=True)
    assert opts["location"] == "United States"
    assert opts["is_remote"] is True
