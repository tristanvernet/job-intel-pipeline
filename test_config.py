from pathlib import Path

import pytest

from config import ROOT, ConfigError, Settings

REPO = Path(__file__).parent


def test_defaults_match_previous_module_paths():
    assert Settings.from_env({}) == Settings(
        db_path=REPO / "jobs.db",
        profile_path=REPO / "profile.json",
        schedule_config_path=REPO / "schedule_config.json",
        lock_path=Path.home() / ".jobintel.lock",
        host="127.0.0.1",
        port=8000,
    )


def test_every_variable_overrides_its_field(tmp_path):
    env = {
        "JOBINTEL_DB_PATH": str(tmp_path / "a.db"),
        "JOBINTEL_PROFILE_PATH": str(tmp_path / "p.json"),
        "JOBINTEL_SCHEDULE_CONFIG_PATH": str(tmp_path / "s.json"),
        "JOBINTEL_LOCK_PATH": str(tmp_path / "w.lock"),
        "JOBINTEL_HOST": "0.0.0.0",
        "JOBINTEL_PORT": "9001",
    }
    assert Settings.from_env(env) == Settings(
        db_path=tmp_path / "a.db",
        profile_path=tmp_path / "p.json",
        schedule_config_path=tmp_path / "s.json",
        lock_path=tmp_path / "w.lock",
        host="0.0.0.0",
        port=9001,
    )


def test_empty_string_means_unset():
    env = {name: "" for name in Settings.env_names()}
    assert Settings.from_env(env) == Settings.from_env({})


def test_relative_paths_resolve_against_repo_root_and_tilde_expands():
    settings = Settings.from_env({"JOBINTEL_DB_PATH": "data/x.db", "JOBINTEL_LOCK_PATH": "~/y.lock"})
    assert settings.db_path == REPO / "data" / "x.db"
    assert settings.lock_path == Path.home() / "y.lock"
    assert ROOT == REPO


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-1", "80.5", "8_000", " 8000"])
def test_bad_port_raises_naming_the_variable(value):
    with pytest.raises(ConfigError, match="JOBINTEL_PORT"):
        Settings.from_env({"JOBINTEL_PORT": value})


def test_env_example_declares_exactly_the_settings_variables():
    keys = set()
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.lstrip("# ").strip()
        if "=" in line:
            keys.add(line.split("=", 1)[0])
    assert keys == set(Settings.env_names())


def test_defaults_need_no_environment():
    assert Settings() == Settings.from_env({})
