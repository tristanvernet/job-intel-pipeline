"""Schedule parsing for the job-intel background runner.

Pure, network-free helpers that turn either a ``schedule_config.json`` file or a
``--interval`` CLI string into a normalized schedule, and then into the launchd
key/value pairs that make macOS run the worker automatically.

Supported interval forms (CLI ``--interval`` or config ``schedule``):
  * ``daily``                 -> every day at HH:MM (default 09:00)
  * ``weekly``                -> every <weekday> at HH:MM (default Monday 09:00)
  * ``weekly:friday@18:30``   -> every Friday at 18:30
  * ``daily@7``               -> every day at 07:00
  * ``6h`` / ``6`` / ``12h``  -> custom fixed interval, every N hours
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path(__file__).parent / "schedule_config.json"

# launchd Weekday: 0 and 7 are both Sunday, 1 = Monday ... 6 = Saturday.
_WEEKDAYS = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4, "thur": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}
_WEEKDAY_NAMES = {
    0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
    4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday",
}

DEFAULT_HOUR = 9
DEFAULT_MINUTE = 0
DEFAULT_WEEKDAY = 1  # Monday


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load schedule_config.json, returning {} when it does not exist."""
    path = Path(path) if path else CONFIG_PATH
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _coerce_weekday(value: Any) -> int:
    if value is None:
        return DEFAULT_WEEKDAY
    if isinstance(value, int):
        return value % 7
    key = str(value).strip().lower()
    if key not in _WEEKDAYS:
        raise ValueError(f"Unknown weekday: {value!r}")
    return _WEEKDAYS[key]


def _clamp_hour(value: Any) -> int:
    hour = int(value)
    if not 0 <= hour <= 23:
        raise ValueError(f"Hour must be 0-23, got {hour}")
    return hour


def _clamp_minute(value: Any) -> int:
    minute = int(value)
    if not 0 <= minute <= 59:
        raise ValueError(f"Minute must be 0-59, got {minute}")
    return minute


def parse_interval(interval: str) -> Dict[str, Any]:
    """Parse a ``--interval`` CLI string into a normalized schedule dict.

    Returns a dict shaped like the config file, e.g.::

        {"schedule": "weekly", "weekday": "friday", "hour": 18, "minute": 30}
        {"schedule": "daily", "hour": 7, "minute": 0}
        {"schedule": "custom", "interval_hours": 6}
    """
    raw = str(interval).strip().lower()
    if not raw:
        raise ValueError("Empty --interval value")

    # Pure "every N hours": "6h", "12h", or bare "6".
    m = re.fullmatch(r"(\d+)\s*h(?:ours?)?", raw) or re.fullmatch(r"(\d+)", raw)
    if m:
        hours = int(m.group(1))
        if hours < 1:
            raise ValueError("Custom interval must be >= 1 hour")
        return {"schedule": "custom", "interval_hours": hours}

    # Split off an optional "@HH" or "@HH:MM" time suffix.
    time_part = None
    if "@" in raw:
        raw, time_part = raw.split("@", 1)

    hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
    if time_part:
        if ":" in time_part:
            h, mnt = time_part.split(":", 1)
            hour, minute = _clamp_hour(h), _clamp_minute(mnt)
        else:
            hour = _clamp_hour(time_part)

    # "daily" or "weekly[:weekday]".
    if raw == "daily":
        return {"schedule": "daily", "hour": hour, "minute": minute}
    if raw == "weekly" or raw.startswith("weekly:"):
        weekday = "monday"
        if ":" in raw:
            weekday = raw.split(":", 1)[1]
        return {
            "schedule": "weekly",
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
        }

    raise ValueError(f"Unrecognized --interval value: {interval!r}")


def resolve_schedule(
    config: Optional[Dict[str, Any]] = None,
    interval: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge config + optional CLI --interval into one normalized schedule.

    ``--interval`` always wins over the config file. The returned dict has a
    ``schedule`` of ``daily``, ``weekly`` or ``custom`` with fully-resolved
    numeric fields.
    """
    base: Dict[str, Any] = dict(config or {})
    if interval:
        base.update(parse_interval(interval))

    mode = str(base.get("schedule", "weekly")).lower()

    if mode == "custom":
        hours = int(base.get("interval_hours", 6))
        if hours < 1:
            raise ValueError("interval_hours must be >= 1")
        return {"schedule": "custom", "interval_hours": hours}

    hour = _clamp_hour(base.get("hour", DEFAULT_HOUR))
    minute = _clamp_minute(base.get("minute", DEFAULT_MINUTE))

    if mode == "daily":
        return {"schedule": "daily", "hour": hour, "minute": minute}

    if mode == "weekly":
        weekday = _coerce_weekday(base.get("weekday", DEFAULT_WEEKDAY))
        return {
            "schedule": "weekly",
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
        }

    raise ValueError(f"Unknown schedule mode: {mode!r}")


def launchd_start_interval(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """Return the launchd scheduling payload for a normalized schedule.

    For ``custom`` this is ``{"StartInterval": <seconds>}``; for calendar
    schedules it is ``{"StartCalendarInterval": {...}}``.
    """
    mode = schedule["schedule"]
    if mode == "custom":
        return {"StartInterval": int(schedule["interval_hours"]) * 3600}

    cal: Dict[str, int] = {
        "Hour": int(schedule["hour"]),
        "Minute": int(schedule["minute"]),
    }
    if mode == "weekly":
        cal["Weekday"] = int(schedule["weekday"])
    return {"StartCalendarInterval": cal}


def describe(schedule: Dict[str, Any]) -> str:
    """Human-readable one-line description of a normalized schedule."""
    mode = schedule["schedule"]
    if mode == "custom":
        h = schedule["interval_hours"]
        return f"every {h} hour{'s' if h != 1 else ''}"
    time_str = f"{schedule['hour']:02d}:{schedule['minute']:02d}"
    if mode == "daily":
        return f"daily at {time_str}"
    day = _WEEKDAY_NAMES.get(int(schedule["weekday"]), "Monday")
    return f"weekly on {day} at {time_str}"
