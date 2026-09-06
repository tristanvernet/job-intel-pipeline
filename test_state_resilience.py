"""Regression coverage for profile publication and browser state coordination."""
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
import db
import matcher
import sync_profile


@pytest.fixture
def profile_path(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    monkeypatch.setattr(app, "PROFILE_PATH", path)
    monkeypatch.setattr(app, "_PROFILE_CACHE", None)
    return path


def test_cold_start_without_profile_serves_jobs(profile_path, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    with TestClient(app.app) as client:
        assert client.get("/").status_code == 200
        db.insert_job({"company": "Example", "title": "Software Engineer",
                       "url": "https://example.com/job", "track": "full_time"})
        response = client.get("/api/jobs")
        assert response.status_code == 200
        assert isinstance(response.json()[0]["match_score"], int)
        assert client.get("/api/jobs/" + response.json()[0]["id"]).status_code == 200
    assert not profile_path.exists()
    assert app.cached_profile() == matcher.default_profile()


def test_corrupt_reload_retains_snapshot_then_recovers(profile_path, caplog):
    sync_profile.save_profile({"languages": {"python": 2}}, profile_path)
    original = app.cached_profile()
    snapshot = app._PROFILE_CACHE
    profile_path.write_text('{"languages":', encoding="utf-8")
    assert app.cached_profile() is original
    assert app._PROFILE_CACHE is snapshot
    assert "retaining last good profile" in caplog.text
    sync_profile.save_profile({"languages": {"rust": 3}}, profile_path)
    assert app.cached_profile() == {"languages": {"rust": 3}}
    assert original == {"languages": {"python": 2}}


def test_cold_start_corrupt_profile_uses_default(profile_path):
    profile_path.write_text("[]", encoding="utf-8")
    assert app.cached_profile() == matcher.default_profile()
    sync_profile.save_profile({"keywords": {"engineer": 1}}, profile_path)
    assert app.cached_profile() == {"keywords": {"engineer": 1}}


def test_concurrent_cache_readers_publish_one_complete_snapshot(profile_path, monkeypatch):
    sync_profile.save_profile({"languages": {"python": 2}}, profile_path)
    original = matcher.load_profile
    calls = []

    def load(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(app, "load_profile", load)
    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = list(executor.map(lambda _: app.cached_profile(), range(40)))
    assert len(calls) == 1
    assert all(profile is profiles[0] for profile in profiles)
    assert profiles[0] == {"languages": {"python": 2}}


def test_atomic_profile_write_keeps_old_file_until_replace(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text('{"old": true}', encoding="utf-8")
    original_replace = sync_profile.os.replace
    original_fsync = sync_profile.os.fsync
    flushed = []

    def fsync(fd):
        original_fsync(fd)
        flushed.append(True)

    def replace(source, destination):
        assert flushed
        assert source == tmp_path / "profile.json.tmp"
        assert json.loads(path.read_text()) == {"old": True}
        assert json.loads(source.read_text()) == {"new": True}
        original_replace(source, destination)

    monkeypatch.setattr(sync_profile.os, "fsync", fsync)
    monkeypatch.setattr(sync_profile.os, "replace", replace)
    sync_profile.save_profile({"new": True}, path)
    assert json.loads(path.read_text()) == {"new": True}
    assert not path.with_name("profile.json.tmp").exists()


def test_failed_profile_serialization_preserves_destination(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text('{"old": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        sync_profile.save_profile({"bad": object()}, path)
    assert json.loads(path.read_text()) == {"old": True}
    assert not path.with_name("profile.json.tmp").exists()


def test_profile_writer_does_not_truncate_another_writers_temporary(tmp_path):
    path = tmp_path / "profile.json"
    temporary = path.with_name("profile.json.tmp")
    temporary.write_text("another writer", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sync_profile.save_profile({"new": True}, path)
    assert temporary.read_text() == "another writer"
