import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parent
ENV_PREFIX = "JOBINTEL_"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: Path
    profile_path: Path
    schedule_config_path: Path
    lock_path: Path
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def env_names(cls) -> tuple:
        return tuple(ENV_PREFIX + f.name.upper() for f in fields(cls))

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        values = {
            "db_path": ROOT / "jobs.db",
            "profile_path": ROOT / "profile.json",
            "schedule_config_path": ROOT / "schedule_config.json",
            "lock_path": Path.home() / ".jobintel.lock",
        }
        for field in fields(cls):
            name = ENV_PREFIX + field.name.upper()
            raw = env.get(name, "")
            if not raw:
                continue
            if field.type is Path:
                values[field.name] = ROOT / Path(raw).expanduser()
            elif field.type is int:
                values[field.name] = _port(name, raw)
            else:
                values[field.name] = raw
        return cls(**values)


def _port(name: str, raw: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} must be an integer port in 1..65535, got {raw!r}")
    return port
