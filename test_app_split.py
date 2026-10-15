from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import re
import sqlite3

from fastapi.testclient import TestClient
import pytest

import app
import db
import worker


def persistence_violations(source):
    return re.findall(r"\.execute\s*\(|\bimport\s+sqlite3\b|\bfrom\s+sqlite3\b", source)


def test_sql_stays_in_db_module():
    root = Path(__file__).parent
    violations = {
        str(path.relative_to(root)): persistence_violations(path.read_text(encoding="utf-8"))
        for path in root.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
        and path.name != "db.py"
        and path.name != "conftest.py"
        and not path.name.startswith("test_")
    }
    assert {path: hits for path, hits in violations.items() if hits} == {}


@pytest.mark.parametrize("source", [
    'conn.execute("SELECT 1")',
    'conn.execute ("SELECT 1")',
    'import sqlite3',
    'from sqlite3 import connect',
])
def test_sql_guard_catches_sample_source(source):
    assert persistence_violations(source)


def test_filters_are_frozen():
    filters = db.JobFilters()
    assert filters.status == "new"
    with pytest.raises(FrozenInstanceError):
        filters.status = "all"


def test_exhausted_busy_write_raises_domain_error_and_rolls_back(store, monkeypatch):
    store.init()
    monkeypatch.setattr(db.time, "sleep", lambda _: None)
    attempts = []
    error = sqlite3.OperationalError("database is locked")

    def fail(conn):
        attempts.append(conn)
        conn.execute("INSERT INTO jobs(id, company, title, url, track) VALUES('one','C','T','u','full_time')")
        raise error

    with pytest.raises(db.DatabaseBusy) as caught:
        store.write(fail)
    assert caught.value.__cause__ is error
    assert len(attempts) == db.WRITE_ATTEMPTS
    assert store.job_ids() == set()


def test_missing_job_mutations_return_false(store):
    store.init()
    assert store.get_job("missing") is None
    assert store.set_status("missing", "saved") is False
    assert store.save_prep("missing", "summary", "bullets") is False


def test_factory_keeps_app_state_separate(settings, tmp_path):
    first = app.create_app(settings)
    second = app.create_app(replace(
        settings, db_path=tmp_path / "second.db", profile_path=tmp_path / "second.json",
        lock_path=tmp_path / "second.lock",
    ))
    assert first.state.profiles is not second.state.profiles
    with TestClient(first) as first_client, TestClient(second) as second_client:
        first.state.store.insert_job(dict(id="one", company="C", title="T", url="u", track="full_time"))
        assert first_client.get("/api/stats").json()["total"] == 1
        assert second_client.get("/api/stats").json()["total"] == 0


def test_invalid_status_does_not_touch_store(monkeypatch):
    def fail(*args):
        pytest.fail("invalid status reached the store")

    monkeypatch.setattr(app.app.state.store, "set_status", fail)
    with TestClient(app.app) as client:
        response = client.post("/api/jobs/one/status", json={"status": "invalid"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid status: invalid"}


def test_lock_probe_reports_unknown_without_fcntl(settings, monkeypatch):
    monkeypatch.setattr(worker, "fcntl", None)
    assert worker.lock_held(settings.lock_path) is None
