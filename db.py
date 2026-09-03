import sqlite3
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any

DB_PATH = Path(__file__).parent / "jobs.db"

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Canonical status values. `new` surfaces in the Inbox tab.
VALID_STATUSES = ("new", "applied", "saved", "archived")

_CREATE_JOBS_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT,
    is_remote INTEGER DEFAULT 0,
    track TEXT CHECK(track IN ('full_time', 'internship', 'unclear')) NOT NULL,
    term TEXT,
    domain TEXT CHECK(domain IN ('SWE', 'Systems', 'AI/ML', 'General')),
    source TEXT,
    date_posted TEXT,
    date_discovered TEXT DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'applied', 'saved', 'archived')),
    applied_at TEXT,
    raw_description TEXT,
    summary TEXT,
    tailored_bullets TEXT,
    outreach_note TEXT
)
"""


def _status_check_present(conn: sqlite3.Connection) -> bool:
    """True if the current jobs table already constrains status via CHECK."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not row or not row[0]:
        return False
    schema = row[0].lower().replace(" ", "")
    return "check(statusin(" in schema


def _migrate_status_check(conn: sqlite3.Connection):
    """Additive, safe rebuild adding the status CHECK constraint.

    Existing rows are preserved. Any legacy status not in VALID_STATUSES is
    normalized to 'new' so the new constraint never rejects live data.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not table_exists or _status_check_present(conn):
        return

    placeholders = ",".join("?" for _ in VALID_STATUSES)
    conn.execute(
        f"UPDATE jobs SET status='new' WHERE status IS NULL OR status NOT IN ({placeholders})",
        VALID_STATUSES,
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    col_list = ", ".join(cols)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
    conn.execute(_CREATE_JOBS_SQL)
    conn.execute(f"INSERT INTO jobs ({col_list}) SELECT {col_list} FROM jobs_legacy")
    conn.execute("DROP TABLE jobs_legacy")
    conn.execute("PRAGMA foreign_keys=ON")


def _migrate_applied_at(conn: sqlite3.Connection):
    """Additive migration: add the applied_at timestamp column if missing.

    Used by the analytics funnel to know when a role entered the APPLIED
    state. Existing rows keep a NULL applied_at until re-applied.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not table_exists:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "applied_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")


def init_db():
    with get_connection() as conn:
        conn.execute(_CREATE_JOBS_SQL)
        _migrate_status_check(conn)
        _migrate_applied_at(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_track_status ON jobs(track, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_domain ON jobs(domain)")

def all_job_ids() -> set:
    """Return the set of every job id currently stored.

    Used by the background worker to snapshot state before/after a run so it can
    count only the genuinely new roles inserted during that specific run.
    """
    with get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT id FROM jobs").fetchall()}


def generate_job_id(company: str, title: str) -> str:
    """Dedup primarily on normalized company + normalized title."""
    clean_company = re_clean(company)
    clean_title = re_clean(title)
    raw = f"{clean_company}|{clean_title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def re_clean(text: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]", "", text or "").lower()

def insert_job(job_data: Dict[str, Any]) -> bool:
    company = str(job_data.get("company") or "").strip()
    if not company or company.lower() == "nan":
        company = "Unknown Company"

    job_id = job_data.get("id") or generate_job_id(company, job_data["title"])
    
    with get_connection() as conn:
        try:
            conn.execute("""
            INSERT INTO jobs (
                id, company, title, url, location, is_remote,
                track, term, domain, source, date_posted, raw_description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                company,
                job_data["title"],
                job_data["url"],
                job_data.get("location"),
                1 if job_data.get("is_remote") else 0,
                job_data.get("track", "unclear"),
                job_data.get("term"),
                job_data.get("domain", "General"),
                job_data.get("source", "scout"),
                job_data.get("date_posted"),
                job_data.get("raw_description", "")
            ))
            return True
        except sqlite3.IntegrityError:
            return False

if __name__ == "__main__":
    init_db()
