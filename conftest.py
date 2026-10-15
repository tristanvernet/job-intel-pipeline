import ipaddress
import os
import socket

import pytest

# The path globals read os.environ at import, so an exported JOBINTEL_* must be
# gone before any test module imports them.
for _name in [n for n in os.environ if n.startswith("JOBINTEL_")]:
    del os.environ[_name]

import app  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import matcher  # noqa: E402
import schedule  # noqa: E402
import worker  # noqa: E402

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _allowed(sock, address):
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return True
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard(sock, address):
    if not _allowed(sock, address):
        raise RuntimeError(f"network access blocked in tests: {address[0]}:{address[1]}")


def _connect(self, address):
    _guard(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _guard(self, address)
    return _real_connect_ex(self, address)


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    env = {
        "JOBINTEL_DB_PATH": str(state / "jobs.db"),
        "JOBINTEL_PROFILE_PATH": str(state / "profile.json"),
        "JOBINTEL_LOCK_PATH": str(state / "worker.lock"),
        "JOBINTEL_SCHEDULE_CONFIG_PATH": str(state / "schedule_config.json"),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    current = config.Settings.from_env(env)
    monkeypatch.setattr(config, "SETTINGS", current)
    monkeypatch.setattr(db, "DB_PATH", current.db_path)
    monkeypatch.setattr(matcher, "PROFILE_PATH", current.profile_path)
    monkeypatch.setattr(app, "PROFILE_PATH", current.profile_path)
    monkeypatch.setattr(app, "_PROFILE_CACHE", None)
    monkeypatch.setattr(worker, "LOCK_PATH", current.lock_path)
    monkeypatch.setattr(schedule, "CONFIG_PATH", current.schedule_config_path)
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    return current
