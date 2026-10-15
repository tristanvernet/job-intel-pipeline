"""The root conftest keeps every test off the network and off the working-copy state."""
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import config
import db


def test_public_connect_is_blocked():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        with pytest.raises(RuntimeError, match=r"network access blocked in tests: 203\.0\.113\.7:80"):
            sock.connect(("203.0.113.7", 80))


def test_public_connect_ex_is_blocked():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        with pytest.raises(RuntimeError, match="network access blocked in tests"):
            sock.connect_ex(("203.0.113.7", 80))


def test_dns_lookup_of_public_name_is_blocked():
    with pytest.raises(RuntimeError, match="network access blocked in tests: DNS lookup of github.com"):
        socket.getaddrinfo("github.com", 443)


def test_udp_sendto_public_address_is_blocked():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        with pytest.raises(RuntimeError, match=r"network access blocked in tests: 203\.0\.113\.7:53"):
            sock.sendto(b"x", ("203.0.113.7", 53))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST"])
def test_loopback_connect_is_allowed(host):
    with closing(socket.create_server(("127.0.0.1", 0))) as server:
        port = server.getsockname()[1]
        with closing(socket.create_connection((host, port), timeout=2)) as client:
            client.sendall(b"ping")
            conn, _ = server.accept()
            with closing(conn):
                assert conn.recv(4) == b"ping"


def test_unix_socket_connect_is_allowed(tmp_path):
    path = str(tmp_path / "s")
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as server:
        server.bind(path)
        server.listen(1)
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as client:
            client.connect(path)
            client.sendall(b"ping")
            conn, _ = server.accept()
            with closing(conn):
                assert conn.recv(4) == b"ping"


def test_db_writes_land_in_tmp_state_not_repo(settings, tmp_path, store):
    repo_db = config.ROOT / "jobs.db"
    before = repo_db.stat().st_mtime_ns if repo_db.exists() else None

    store.init()
    assert store.insert_job({"company": "Acme", "title": "Software Engineer",
                          "url": "https://acme.example/1", "track": "full_time"}) is True

    assert settings.db_path.parent == tmp_path / "state"
    with closing(sqlite3.connect(settings.db_path)) as conn:
        assert conn.execute("SELECT company, title FROM jobs").fetchall() == [("Acme", "Software Engineer")]
    after = repo_db.stat().st_mtime_ns if repo_db.exists() else None
    assert after == before


def test_exported_jobintel_variables_do_not_reach_path_globals(tmp_path):
    leaked = tmp_path / "leaked.db"
    env = {**os.environ, "JOBINTEL_DB_PATH": str(leaked)}
    out = subprocess.run(
        [sys.executable, "-c", "import conftest, config; print(config.SETTINGS.db_path)"],
        cwd=config.ROOT, env=env, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    assert out == str(config.ROOT / "jobs.db")


def test_no_loaded_module_still_points_at_working_copy_state():
    defaults = config.Settings()
    real = {defaults.db_path, defaults.profile_path, defaults.schedule_config_path, defaults.lock_path}
    leaks = sorted(
        f"{name}.{attr}"
        for name, module in list(sys.modules.items())
        if str(getattr(module, "__file__", "") or "").startswith(str(config.ROOT))
        and not name.startswith(("test_", "conftest"))
        for attr, value in vars(module).items()
        for path in ([value.path] if isinstance(value, db.JobDB) else [value])
        if isinstance(path, Path) and path in real
    )
    assert leaks == []


@pytest.mark.parametrize("leaked", [config.Settings().db_path, db.JobDB(config.Settings().db_path)])
def test_leak_scan_catches_working_copy_path(monkeypatch, leaked):
    monkeypatch.setattr(db, "leaked", leaked, raising=False)
    with pytest.raises(AssertionError, match="db.leaked"):
        test_no_loaded_module_still_points_at_working_copy_state()
