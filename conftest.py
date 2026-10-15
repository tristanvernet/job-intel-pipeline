import ipaddress
import os
import socket

import pytest

# Settings read os.environ at import, before per-test fixtures can replace them.
for _name in [n for n in os.environ if n.startswith("JOBINTEL_")]:
    del os.environ[_name]

import app  # noqa: E402
import config  # noqa: E402

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_getaddrinfo = socket.getaddrinfo


def _is_loopback(host):
    if host is None:
        return True
    host = str(host).split("%", 1)[0].rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
        raise RuntimeError(f"network access blocked in tests: {address[0]}:{address[1]}")


def _connect(self, address):
    _guard(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _guard(self, address)
    return _real_connect_ex(self, address)


def _sendto(self, data, *args):
    _guard(self, args[-1])
    return _real_sendto(self, data, *args)


def _getaddrinfo(host, *args, **kwargs):
    if host is not None and not _is_loopback(host):
        try:
            ipaddress.ip_address(str(host).split("%", 1)[0])
        except ValueError:
            raise RuntimeError(f"network access blocked in tests: DNS lookup of {host}") from None
    return _real_getaddrinfo(host, *args, **kwargs)


# Installed at import so collection, session fixtures, and plugins are covered too.
socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.socket.sendto = _sendto
socket.getaddrinfo = _getaddrinfo


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
    monkeypatch.setattr(app, "app", app.create_app(current))
    return current


@pytest.fixture
def store(settings):
    return app.app.state.store
