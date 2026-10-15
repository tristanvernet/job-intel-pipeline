"""Regression coverage for profile publication and browser state coordination."""
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
import matcher
import sync_profile


def test_cold_start_without_profile_serves_jobs(settings, store):
    with TestClient(app.app) as client:
        assert client.get("/").status_code == 200
        store.insert_job({"company": "Example", "title": "Software Engineer",
                       "url": "https://example.com/job", "track": "full_time"})
        response = client.get("/api/jobs")
        assert response.status_code == 200
        assert isinstance(response.json()[0]["match_score"], int)
        assert client.get("/api/jobs/" + response.json()[0]["id"]).status_code == 200
    assert not settings.profile_path.exists()
    assert app.PROFILE_FILE.current() == matcher.default_profile()


def test_corrupt_reload_retains_snapshot_then_recovers(settings, caplog):
    source = matcher.ProfileFile(settings.profile_path)
    sync_profile.save_profile({"languages": {"python": 2}}, settings.profile_path)
    original = source.current()
    snapshot = source._snapshot
    settings.profile_path.write_text('{"languages":', encoding="utf-8")
    assert source.current() is original
    assert source._snapshot is snapshot
    assert "retaining last good profile" in caplog.text
    sync_profile.save_profile({"languages": {"rust": 3}}, settings.profile_path)
    assert source.current() == {"languages": {"rust": 3}}
    assert original == {"languages": {"python": 2}}


def test_cold_start_corrupt_profile_uses_default(settings):
    source = matcher.ProfileFile(settings.profile_path)
    settings.profile_path.write_text("[]", encoding="utf-8")
    assert source.current() == matcher.default_profile()
    sync_profile.save_profile({"keywords": {"engineer": 1}}, settings.profile_path)
    assert source.current() == {"keywords": {"engineer": 1}}


def test_concurrent_cache_readers_publish_one_complete_snapshot(settings, monkeypatch):
    sync_profile.save_profile({"languages": {"python": 2}}, settings.profile_path)
    original = matcher.load_profile
    calls = []

    def load(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(matcher, "load_profile", load)
    source = matcher.ProfileFile(settings.profile_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = list(executor.map(lambda _: source.current(), range(40)))
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


def test_frontend_state_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend state regression harness")
    result = subprocess.run(
        [node, "--test", "test_ui_state.cjs"],
        cwd=Path(__file__).parent, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
