# Job Intel Hub

A high-velocity, keyboard-first job triage workstation built for clearing hundreds of tech roles without browser tab sprawl.

```
┌─────────────────────────────────────────────────────────────┐
│ Inbox 312  ·  Applied 24  ·  Saved 18  ·  Archived 489      │
├───────────────────────────────┬─────────────────────────────┤
│ ● Stripe   SWE Intern · SF    │  Stripe — SWE Intern        │
│ ◐ Ramp     Backend Int · NYC  │  Match 87% █████████░       │
│ ○ Acme     SWE I · Remote     │  ✓ python  ✓ fastapi        │
│ ○ Plaid    SWE Intern · NY    │  ✗ docker  ✗ terraform      │
│  ← stream                     │  Description · Prep notes   │
│                               │  → inspector                │
└───────────────────────────────┴─────────────────────────────┘
```

## The problem

Card walls collapse at inbox scale. Rendering 1,000 roles as a grid of cards means roughly eight visual regions per item and a hard scroll cap on what you can review before fatigue sets in. Most attempts fix this with keyword scoring, which creates the opposite problem: every entry-level role saturates to `100%` and the ranking carries no signal.

High-throughput triage is a different discipline. It needs one glance line per item, one keystroke per decision, and mutations that land instantly instead of round-tripping through a server on every action. That is what this workstation is built around.

## Workstation features

**Ergonomic triage.** A Linear-style split pane: a scrollable 44px dense stream on the left and a sticky inspection drawer on the right. The list never reflows while you inspect.

**Explainable match engine.** Scores are relative tiers (Emerald 90+, Sky 75+, Zinc below) instead of saturated percentages. The inspector renders the exact breakdown: matched keywords as green chips, missing profile keywords as muted chips, capped at six with a `+N more` toggle.

**Zero-latency mutations.** Status updates apply locally first and sync in the background. A three-second undo stack (`z`) recovers any action, and the cursor clamps to the nearest row instead of resetting to the top. If the background call fails, state reverts and an error banner explains what happened.

**Telemetry and hygiene.** A header strip shows scrape freshness and indexed role count from `SELECT MAX(date_discovered) FROM jobs`, and every status tab carries a live count. Scraped descriptions and summaries are escaped before rendering.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `j` / `k` | Navigate the stream |
| `a` | Mark applied |
| `s` | Save |
| `x` | Dismiss |
| `z` | Undo last action |
| `p` | Generate prep |
| `Enter` | Open posting in a new tab |
| `/` | Focus search |
| `Esc` | Blur search or clear the inspector |

## Architecture

The backend is FastAPI with sync route handlers and a generator dependency that yields a dedicated connection per request and always closes it in `finally`. Cross-thread safety comes from `check_same_thread=False` on the sqlite connection: Starlette's threadpool can hand the generator between threads, and each request still owns an exclusive handle.

Storage is SQLite in WAL mode (`PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=15000`), which lets the launchd-driven background scraper write while the API serves reads without lock contention.

The frontend is a single `templates/index.html` with Tailwind, using a strict dark zinc palette (`zinc-950` canvas) and WCAG-compliant text contrast. No frameworks, no build step.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the test suite:

```bash
pytest -q
```

Launch the workstation:

```bash
uvicorn app:app --reload --port 8000
```

Open `http://127.0.0.1:8000`. The Inbox tab loads on first render; press `/` to search.
