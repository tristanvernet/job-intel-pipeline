"""Install / uninstall a macOS launchd agent that runs the job-intel worker.

Quick start:
    python setup_schedule.py install                 # use schedule_config.json
    python setup_schedule.py install --interval daily@8
    python setup_schedule.py install --interval 6h   # every 6 hours
    python setup_schedule.py status
    python setup_schedule.py uninstall

The agent runs ``worker.py`` on the configured schedule (daily / weekly / custom
hour interval) in the background via launchd.
"""
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import schedule as schedule_mod

LABEL = "com.jobintel.scheduler"
PROJECT_DIR = Path(__file__).resolve().parent
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = PROJECT_DIR / "logs"


def _python_executable() -> str:
    """Prefer the project venv python, falling back to the current interpreter."""
    for candidate in (PROJECT_DIR / ".venv" / "bin" / "python",
                      PROJECT_DIR / ".venv" / "bin" / "python3"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def build_plist(schedule: Dict[str, Any],
                python_exe: Optional[str] = None,
                project_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Build the launchd property-list dict for the worker agent."""
    project_dir = Path(project_dir) if project_dir else PROJECT_DIR
    python_exe = python_exe or _python_executable()
    log_dir = project_dir / "logs"

    plist: Dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [python_exe, str(project_dir / "worker.py")],
        "WorkingDirectory": str(project_dir),
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "worker.out.log"),
        "StandardErrorPath": str(log_dir / "worker.err.log"),
    }
    plist.update(schedule_mod.launchd_start_interval(schedule))
    return plist


def write_plist(schedule: Dict[str, Any], path: Path = PLIST_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(schedule), fh)
    return path


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(schedule: Dict[str, Any]) -> None:
    path = write_plist(schedule)
    print(f"[setup] wrote {path}")
    print(f"[setup] schedule: {schedule_mod.describe(schedule)}")
    if sys.platform != "darwin":
        print("[setup] not macOS - plist written but launchctl skipped.")
        return
    _launchctl("unload", str(path))  # ignore errors if not loaded
    res = _launchctl("load", str(path))
    if res.returncode == 0:
        print(f"[setup] loaded launchd agent '{LABEL}'.")
    else:
        print(f"[setup] launchctl load failed: {res.stderr.strip()}")


def uninstall() -> None:
    if sys.platform == "darwin" and PLIST_PATH.exists():
        _launchctl("unload", str(PLIST_PATH))
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"[setup] removed {PLIST_PATH}")
    else:
        print("[setup] nothing to remove.")


def status() -> None:
    print(f"[setup] plist: {PLIST_PATH} ({'present' if PLIST_PATH.exists() else 'absent'})")
    if sys.platform == "darwin":
        res = _launchctl("list")
        loaded = any(LABEL in line for line in res.stdout.splitlines())
        print(f"[setup] launchd loaded: {loaded}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Install the job-intel launchd agent")
    sub = p.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install", help="Write + load the launchd agent")
    inst.add_argument("--interval",
                      help="Override schedule: daily | weekly[:weekday][@HH:MM] | Nh")
    sub.add_parser("uninstall", help="Unload + remove the launchd agent")
    sub.add_parser("status", help="Show whether the agent is installed/loaded")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install":
        config = schedule_mod.load_config()
        schedule = schedule_mod.resolve_schedule(config, args.interval)
        install(schedule)
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "status":
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
