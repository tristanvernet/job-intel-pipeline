import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).parent
ENV_PREFIX = "JOBINTEL_"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / "jobs.db"
    profile_path: Path = ROOT / "profile.json"
    schedule_config_path: Path = ROOT / "schedule_config.json"
    lock_path: Path = field(default_factory=lambda: Path.home() / ".jobintel.lock")
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def env_names(cls) -> tuple:
        return tuple(ENV_PREFIX + name.upper() for name in cls.__dataclass_fields__)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def value(name):
            return env.get(ENV_PREFIX + name.upper(), "")

        overrides = {}
        for name in ("db_path", "profile_path", "schedule_config_path", "lock_path"):
            if value(name):
                overrides[name] = ROOT / Path(value(name)).expanduser()
        if value("host"):
            overrides["host"] = value("host")
        if value("port"):
            overrides["port"] = _port(value("port"))
        return cls(**overrides)


def _port(raw: str) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"{ENV_PREFIX}PORT must be an integer port in 1..65535, got {raw!r}")
    return int(raw)


SETTINGS = Settings.from_env()
