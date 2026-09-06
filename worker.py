"""Background runner for the job-intel pipeline.

Executes the two collectors (``fetch_github`` then ``scout``) in sequence,
counts only the roles that were genuinely inserted during this specific run by
diffing the set of job ids before and after, and fires a native macOS
notification with the result.

Usage:
    python worker.py                 # run the pipeline, then notify
    python worker.py --test          # fire a test notification immediately
    python worker.py --interval daily
    python worker.py --no-notify     # run but stay silent
    python worker.py --skip-scout    # only run the GitHub collector
"""
from __future__ import annotations

import argparse
import json
from collector_status import failure_status, summarize
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

try:
    import fcntl  # POSIX-only; absent on Windows
except ImportError:  # pragma: no cover - platform guard
    fcntl = None

LOCK_PATH = Path.home() / ".jobintel.lock"

import db
import notify
import schedule as schedule_mod


def _run_step(name: str, fn: Callable) -> dict:
    """Isolate sources and preserve their failure/count information."""
    try:
        result = fn()
        if isinstance(result, list):
            result = summarize(result)
        elif result is None:  # legacy collectors without a report
            result = summarize([])
    except Exception as exc:
        result = {"status": failure_status(exc), "fetched": 0, "inserted": 0,
                  "rejected": 0, "error": str(exc)}
    result = {"source": name, **result}
    print(json.dumps(result, sort_keys=True))
    return result


def run_pipeline(skip_github: bool = False, skip_scout: bool = False) -> Dict[str, object]:
    """Run the collectors and return a summary of newly inserted roles.

    Returns a dict with ``new_count``, the sorted list of ``new_ids``, and the
    total ``elapsed`` seconds.
    """
    # Overlap guard: a long scout run (5-15 min of polite sleeps) must never
    # race a second launchd fire. Non-blocking flock -> exit cleanly if busy.
    lock_fd = None
    if fcntl is not None:
        lock_fd = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("[worker] another run already in progress; exiting.", file=sys.stderr)
            lock_fd.close()
            return {"new_count": 0, "new_ids": [], "elapsed": 0.0, "skipped": True}

    try:
        db.init_db()
        before = db.all_job_ids()
        started = time.time()

        results = []
        if not skip_github:
            import fetch_github
            results.append(_run_step("fetch_github", fetch_github.run_github_fetch))
        if not skip_scout:
            import scout
            results.append(_run_step("scout", scout.run_scout))

        after = db.all_job_ids()
        new_ids = sorted(after - before)
        elapsed = time.time() - started
        return {**summarize(results), "new_count": len(new_ids), "new_ids": new_ids, "elapsed": elapsed}
    finally:
        if lock_fd is not None:
            lock_fd.close()  # closing the fd releases the flock


def _window_label(schedule: Optional[Dict[str, object]]) -> str:
    """Pick the notification time-window phrase from the active schedule."""
    if not schedule:
        return "this run"
    mode = schedule.get("schedule")
    if mode == "weekly":
        return "this week"
    if mode == "daily":
        return "today"
    return "this run"


def run(interval: Optional[str] = None, notify_enabled: bool = True,
        skip_github: bool = False, skip_scout: bool = False) -> Dict[str, object]:
    """Full worker run: execute the pipeline and notify about new roles."""
    config = schedule_mod.load_config()
    schedule = None
    try:
        schedule = schedule_mod.resolve_schedule(config, interval)
        print(f"[worker] schedule: {schedule_mod.describe(schedule)}")
    except Exception as e:  # noqa: BLE001
        print(f"[worker] could not resolve schedule ({e}); continuing")

    if notify_enabled and not config.get("notify", True):
        notify_enabled = False

    summary = run_pipeline(skip_github=skip_github, skip_scout=skip_scout)
    count = summary["new_count"]
    print(f"\n[worker] {count} genuinely new role(s) inserted "
          f"in {summary['elapsed']:.1f}s")

    if notify_enabled and not summary.get("skipped"):
        if summary.get("status", "ok") == "ok":
            notify.notify_new_roles(count, window=_window_label(schedule))
        else:
            notify.send_notification(
                f"Collection {summary['status']}: {count} new roles. Check worker logs for source errors."
            )
    return summary


def run_test_notification() -> bool:
    """Fire an immediate test notification (used by --test / dry run)."""
    print("[worker] sending test notification...")
    return notify.send_notification(
        notify.compose_new_roles_message(24, "this week"),
        subtitle="Test notification",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Job-intel background runner")
    p.add_argument("--test", action="store_true",
                   help="Fire a test notification immediately and exit (dry run)")
    p.add_argument("--interval",
                   help="Override schedule: daily | weekly[:weekday][@HH:MM] | Nh")
    p.add_argument("--no-notify", action="store_true",
                   help="Run the pipeline but do not send a notification")
    p.add_argument("--skip-github", action="store_true",
                   help="Skip the GitHub collector")
    p.add_argument("--skip-scout", action="store_true",
                   help="Skip the job-board scout collector")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.test:
        run_test_notification()
        return 0
    result = run(
        interval=args.interval,
        notify_enabled=not args.no_notify,
        skip_github=args.skip_github,
        skip_scout=args.skip_scout,
    )
    return 0 if result.get("status", "ok") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
