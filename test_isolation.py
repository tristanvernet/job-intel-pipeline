"""The root conftest keeps every test off the network and off the working-copy state."""
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing

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


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
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


def test_db_writes_land_in_tmp_state_not_repo(settings, tmp_path):
    repo_db = config.ROOT / "jobs.db"
    before = repo_db.stat().st_mtime_ns if repo_db.exists() else None

    db.init_db()
    assert db.insert_job({"company": "Acme", "title": "Software Engineer",
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
        [sys.executable, "-c", "import conftest, db; print(db.DB_PATH)"],
        cwd=config.ROOT, env=env, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    assert out == str(config.ROOT / "jobs.db")
