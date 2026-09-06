from contextlib import closing

from fastapi.testclient import TestClient

import app
import db
import worker


def test_rank_before_pagination_and_source_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(app, "cached_profile", lambda: {})
    monkeypatch.setattr(app, "match_score", lambda row, *a, **kw: 100 if row["id"] == "best" else 0)
    with TestClient(app.app) as client:
        def seed(conn):
            conn.executemany(
                "INSERT INTO jobs(id,company,title,url,track,source,date_discovered) VALUES(?,?,?,?,?,?,?)",
                [(f"low{i:03}", "C", "Engineer", "https://example.com", "full_time", "board", "2026-09-06") for i in range(501)]
                + [("best", "C", "Engineer", "https://example.com", "full_time", "curated", "2000-01-01")],
            )
        db.write_transaction(seed)
        ranked = client.get("/api/jobs").json()
        assert len(ranked) == 500
        assert ranked[0]["id"] == "best"
        page = client.get("/api/jobs?limit=1&offset=1").json()
        assert page[0]["id"] == "low000"
        assert client.get("/api/jobs?source=curated").json()[0]["id"] == "best"
        assert client.get("/api/jobs?source=curated&search=absent").json() == []
        assert client.get("/api/jobs?offset=-1").status_code == 422
        assert client.get("/api/jobs?limit=0").status_code == 422


def test_scraper_liveness_uses_lock_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "LOCK_PATH", tmp_path / "worker.lock")
    assert app.scraper_running() is False
    with open(worker.LOCK_PATH, "w") as lock:
        assert app.scraper_running() is False
        worker.fcntl.flock(lock, worker.fcntl.LOCK_EX | worker.fcntl.LOCK_NB)
        assert app.scraper_running() is True
    assert app.scraper_running() is False
