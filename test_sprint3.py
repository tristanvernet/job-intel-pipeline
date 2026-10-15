import json

from fastapi.testclient import TestClient

import app
import worker


def test_rank_before_pagination_and_source_filter(settings, store):
    settings.profile_path.write_text(json.dumps({"keywords": {"wizard": 1}}), encoding="utf-8")
    with TestClient(app.app) as client:
        def seed(conn):
            conn.executemany(
                "INSERT INTO jobs(id,company,title,url,track,source,date_discovered) VALUES(?,?,?,?,?,?,?)",
                [(f"low{i:03}", "C", "Engineer", "https://example.com", "full_time", "board", "2026-09-06") for i in range(501)]
                + [("best", "C", "Wizard Engineer", "https://example.com", "full_time", "curated", "2000-01-01")],
            )
        store.write(seed)
        ranked = client.get("/api/jobs").json()
        assert len(ranked) == 500
        assert ranked[0]["id"] == "best"
        page = client.get("/api/jobs?limit=1&offset=1").json()
        assert page[0]["id"] == "low000"
        assert client.get("/api/jobs?source=curated").json()[0]["id"] == "best"
        assert client.get("/api/jobs?source=curated&search=absent").json() == []
        assert client.get("/api/jobs?offset=-1").status_code == 422
        assert client.get("/api/jobs?limit=0").status_code == 422


def test_scraper_liveness_uses_lock_ownership(settings):
    assert worker.lock_held(settings.lock_path) is False
    with open(settings.lock_path, "w") as lock:
        assert worker.lock_held(settings.lock_path) is False
        worker.fcntl.flock(lock, worker.fcntl.LOCK_EX | worker.fcntl.LOCK_NB)
        assert worker.lock_held(settings.lock_path) is True
    assert worker.lock_held(settings.lock_path) is False
