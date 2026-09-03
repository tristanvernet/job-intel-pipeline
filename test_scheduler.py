"""Offline tests for the background runner: schedule parsing, notifications,
the launchd plist builder, and the new-role counting worker. No network, no
real ``osascript`` call, no launchctl."""
import plistlib

import pytest

import db
import notify
import schedule as schedule_mod
import setup_schedule
import worker


# --------------------------------------------------------------------------- #
# schedule parsing
# --------------------------------------------------------------------------- #
def test_parse_interval_custom_hours():
    assert schedule_mod.parse_interval("6h") == {"schedule": "custom", "interval_hours": 6}
    assert schedule_mod.parse_interval("12") == {"schedule": "custom", "interval_hours": 12}


def test_parse_interval_daily_with_time():
    assert schedule_mod.parse_interval("daily@7") == {
        "schedule": "daily", "hour": 7, "minute": 0}


def test_parse_interval_weekly_with_weekday_and_time():
    assert schedule_mod.parse_interval("weekly:friday@18:30") == {
        "schedule": "weekly", "weekday": "friday", "hour": 18, "minute": 30}


def test_parse_interval_invalid():
    with pytest.raises(ValueError):
        schedule_mod.parse_interval("fortnightly")


def test_resolve_schedule_interval_overrides_config():
    config = {"schedule": "weekly", "weekday": "monday", "hour": 9, "minute": 0}
    resolved = schedule_mod.resolve_schedule(config, interval="daily@8")
    assert resolved == {"schedule": "daily", "hour": 8, "minute": 0}


def test_resolve_schedule_weekly_default_monday_9am():
    resolved = schedule_mod.resolve_schedule({"schedule": "weekly"})
    assert resolved == {"schedule": "weekly", "weekday": 1, "hour": 9, "minute": 0}


def test_launchd_calendar_weekly():
    payload = schedule_mod.launchd_start_interval(
        {"schedule": "weekly", "weekday": 1, "hour": 9, "minute": 0})
    assert payload == {"StartCalendarInterval": {"Hour": 9, "Minute": 0, "Weekday": 1}}


def test_launchd_custom_interval_seconds():
    payload = schedule_mod.launchd_start_interval(
        {"schedule": "custom", "interval_hours": 6})
    assert payload == {"StartInterval": 6 * 3600}


def test_describe():
    assert schedule_mod.describe(
        {"schedule": "weekly", "weekday": 1, "hour": 9, "minute": 0}) == \
        "weekly on Monday at 09:00"
    assert schedule_mod.describe(
        {"schedule": "custom", "interval_hours": 1}) == "every 1 hour"


def test_shipped_config_resolves():
    resolved = schedule_mod.resolve_schedule(schedule_mod.load_config())
    assert resolved["schedule"] in ("daily", "weekly", "custom")


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def test_compose_new_roles_message():
    assert notify.compose_new_roles_message(24) == "Found 24 new roles this week."
    assert notify.compose_new_roles_message(1) == "Found 1 new role this week."
    assert notify.compose_new_roles_message(0) == "No new roles found this week."


def test_build_applescript_escapes_quotes():
    script = notify.build_applescript('say "hi"', title='Job "Intel"')
    assert '\\"hi\\"' in script
    assert 'with title "Job \\"Intel\\""' in script
    assert script.startswith("display notification")


def test_send_notification_mocked(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        class R:  # noqa: D401
            pass
        return R()

    monkeypatch.setattr(notify, "is_macos", lambda: True)
    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    assert notify.send_notification("hello") is True
    assert calls["cmd"][0] == "/usr/bin/osascript"
    assert calls["cmd"][1] == "-e"


def test_send_notification_off_mac_returns_false(monkeypatch):
    monkeypatch.setattr(notify, "is_macos", lambda: False)
    assert notify.send_notification("hi") is False


# --------------------------------------------------------------------------- #
# launchd plist builder
# --------------------------------------------------------------------------- #
def test_build_plist_weekly(tmp_path):
    schedule = {"schedule": "weekly", "weekday": 1, "hour": 9, "minute": 0}
    plist = setup_schedule.build_plist(
        schedule, python_exe="/usr/bin/python3", project_dir=tmp_path)
    assert plist["Label"] == setup_schedule.LABEL
    assert plist["ProgramArguments"] == ["/usr/bin/python3", str(tmp_path / "worker.py")]
    assert plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 0, "Weekday": 1}
    # round-trips as a real plist
    assert plistlib.loads(plistlib.dumps(plist))["Label"] == setup_schedule.LABEL


def test_build_plist_custom_interval(tmp_path):
    plist = setup_schedule.build_plist(
        {"schedule": "custom", "interval_hours": 4},
        python_exe="/usr/bin/python3", project_dir=tmp_path)
    assert plist["StartInterval"] == 4 * 3600
    assert "StartCalendarInterval" not in plist


# --------------------------------------------------------------------------- #
# worker: counts only genuinely new roles for this run
# --------------------------------------------------------------------------- #
@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    db.init_db()
    return db


def _sample(idx):
    return {
        "id": f"job{idx}", "company": f"Co{idx}", "title": "Software Engineer Intern",
        "url": f"https://x.com/{idx}", "location": "Remote", "is_remote": True,
        "track": "internship", "term": "Summer 2027", "domain": "SWE",
        "source": "test", "raw_description": "desc",
    }


def test_run_pipeline_counts_only_new(temp_db, monkeypatch):
    # Pre-existing role should NOT be counted this run.
    db.insert_job(_sample(0))

    def fake_github():
        db.insert_job(_sample(1))
        db.insert_job(_sample(2))

    def fake_scout():
        db.insert_job(_sample(2))  # duplicate of github insert -> not new
        db.insert_job(_sample(3))

    monkeypatch.setattr("fetch_github.run_github_fetch", fake_github, raising=False)
    monkeypatch.setattr("scout.run_scout", fake_scout, raising=False)

    summary = worker.run_pipeline()
    assert summary["new_count"] == 3  # job1, job2, job3 (job0 excluded, job2 once)
    assert set(summary["new_ids"]) == {"job1", "job2", "job3"}


def test_run_pipeline_isolates_step_failure(temp_db, monkeypatch):
    def boom():
        raise RuntimeError("network down")

    def fake_scout():
        db.insert_job(_sample(9))

    monkeypatch.setattr("fetch_github.run_github_fetch", boom, raising=False)
    monkeypatch.setattr("scout.run_scout", fake_scout, raising=False)

    summary = worker.run_pipeline()
    assert summary["new_count"] == 1
    assert summary["new_ids"] == ["job9"]


def test_run_notifies_with_count(temp_db, monkeypatch):
    sent = {}

    def fake_github():
        db.insert_job(_sample(1))

    monkeypatch.setattr("fetch_github.run_github_fetch", fake_github, raising=False)
    monkeypatch.setattr("scout.run_scout", lambda: None, raising=False)
    monkeypatch.setattr(worker.schedule_mod, "load_config",
                        lambda: {"schedule": "weekly", "notify": True})
    monkeypatch.setattr(
        worker.notify, "notify_new_roles",
        lambda count, window="this week": sent.update(count=count, window=window) or True)

    worker.run(skip_scout=True)
    assert sent == {"count": 1, "window": "this week"}


def test_run_respects_no_notify(temp_db, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("fetch_github.run_github_fetch", lambda: None, raising=False)
    monkeypatch.setattr("scout.run_scout", lambda: None, raising=False)
    monkeypatch.setattr(worker.schedule_mod, "load_config", lambda: {})
    monkeypatch.setattr(worker.notify, "notify_new_roles",
                        lambda *a, **k: called.update(n=called["n"] + 1))
    worker.run(notify_enabled=False)
    assert called["n"] == 0


def test_test_notification_dry_run(monkeypatch):
    captured = {}
    monkeypatch.setattr(worker.notify, "send_notification",
                        lambda msg, **k: captured.update(msg=msg, kwargs=k) or True)
    assert worker.run_test_notification() is True
    assert "24 new roles" in captured["msg"]
    assert captured["kwargs"].get("subtitle") == "Test notification"


def test_main_test_flag(monkeypatch):
    monkeypatch.setattr(worker.notify, "send_notification", lambda *a, **k: True)
    assert worker.main(["--test"]) == 0
