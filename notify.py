"""Native macOS desktop notifications via AppleScript (osascript).

Keeps the AppleScript-building logic pure and testable (string escaping,
message composition) and isolates the actual ``osascript`` subprocess call so it
can be skipped or mocked off macOS.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional

APP_TITLE = "Job Intel"


def _escape(text: str) -> str:
    """Escape a Python string for safe embedding inside an AppleScript literal."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def build_applescript(message: str, title: str = APP_TITLE,
                      subtitle: Optional[str] = None) -> str:
    """Build the AppleScript command for a notification banner."""
    parts = [f'display notification "{_escape(message)}"',
             f'with title "{_escape(title)}"']
    if subtitle:
        parts.append(f'subtitle "{_escape(subtitle)}"')
    return " ".join(parts)


def compose_new_roles_message(count: int, window: str = "this week") -> str:
    """Compose the banner text, e.g. 'Found 24 new roles this week'."""
    if count <= 0:
        return f"No new roles found {window}."
    noun = "role" if count == 1 else "roles"
    return f"Found {count} new {noun} {window}."


def is_macos() -> bool:
    return sys.platform == "darwin"


def send_notification(message: str, title: str = APP_TITLE,
                      subtitle: Optional[str] = None) -> bool:
    """Pop a native macOS notification banner.

    Returns True if the banner was dispatched, False if osascript is missing or
    the platform is not macOS (so callers/tests never crash off-Mac).
    """
    script = build_applescript(message, title=title, subtitle=subtitle)
    osascript = shutil.which("osascript")
    if not is_macos() or not osascript:
        print(f"[notify] (no osascript) {title}: {message}")
        return False
    try:
        subprocess.run([osascript, "-e", script], check=True,
                       capture_output=True, timeout=15)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[notify] failed to display notification: {e}")
        return False


def notify_new_roles(count: int, window: str = "this week") -> bool:
    """Convenience wrapper: notify about newly discovered roles."""
    return send_notification(compose_new_roles_message(count, window))


if __name__ == "__main__":
    # Manual smoke test: `python notify.py "custom message"`
    msg = sys.argv[1] if len(sys.argv) > 1 else compose_new_roles_message(24)
    send_notification(msg)
