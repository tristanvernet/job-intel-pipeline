# Job Intel Hub

An autonomous, end-to-end job intelligence pipeline and keyboard-driven triage workstation for technical roles.

## System overview

```
  Ingestion Feeds            Match & Intel              Triage Workstation
 ┌─────────────────┐        ┌─────────────────┐        ┌──────────────────┐
 │ GitHub early-   │        │ profile.json    │        │ 44px dense       │
 │ career indexes  │        │ de-saturated    │        │ stream           │
 │ ─────────────── │  WAL   │ scoring (6.0)   │  API   │ ──────────────── │
 │ board scrapers  │ ─────▶ │ explicit chips  │ ─────▶ │ persistent       │
 │ payload clean-  │ dedupe │ matched/missing │        │ inspector        │
 │ ing & normalize │        │ prep synthesis  │        │ a/s/x/z triage   │
 └─────────────────┘        └─────────────────┘        └──────────────────┘
```

The pipeline runs continuously: ingestion feeds pull early-career roles from curated GitHub lists and broad board scrapers, dedupe them against the local store, and the workstation presents them in a single-pane triage view built for clearing hundreds of roles without losing your place.

## Core pillars

### Ingestion and sourcing

Continuous early-career aggregation from curated early-career indexes and general board scrapers. Every payload is normalized on the way in: markdown artifacts stripped, junk URL parameters removed, and unified into a single schema with deterministic IDs (`company|title|location` SHA-256, symbols preserved so `C++ Engineer` and `C# Engineer` never collide).

The store is SQLite in WAL mode (`PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=15000`) with `check_same_thread=False`. Background scrapers write while the FastAPI service reads, without lock contention or threadpool errors.

### Intelligence and matching

Matching is driven by a local `profile.json` — a weighted keyword map of languages, frameworks, skills, and domains. Scoring is deliberately de-saturated (6.0 target) so entry-level roles do not cluster at an artificial `100%`; the score carries signal across the whole pool instead of shouting.

Every score is explainable: the inspector renders matched keywords as green chips and missing profile keywords as muted chips, capped at six with a `+N more` disclosure. Interview prep gets its own synthesis pass per role, producing a summary and tailored alignment bullets.

### Ergonomic workstation

Linear-style split pane: a 44px dense stream on the left and a persistent inspector on the right that never shifts the stream. Mutations are optimistic — status changes land locally before the network, the cursor clamps to the nearest row instead of snapping to the top, and a three-second undo stack (`z`) recovers any action. Failed background syncs revert state and surface an auto-dismissing error banner.

Presented on a WCAG-compliant dark zinc palette (`zinc-950` canvas) with high-contrast cursor states and a strict single-accent (sky) discipline.

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

## Technical stack

- **FastAPI**, sync route handlers with a threadpool-safe generator dependency that yields one connection per request and closes it in `finally`.
- **SQLite** in WAL mode (`busy_timeout=15000`, `check_same_thread=False`), schema-migrated via transactional rebuilds with rollback-restore.
- **Tailwind CSS** served as a single static template — zero npm dependencies, zero build steps.
- **Pytest** covering scoring determinism, ingestion resilience, status transitions, migration safety, and lockfile overlap.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the suite:

```bash
pytest -q
```

Launch:

```bash
uvicorn app:app --reload --port 8000
```

Open `http://127.0.0.1:8000`. The Inbox tab loads first; `/` focuses search.
