import sqlite3
import hashlib
import random
import time
from contextlib import closing
from pathlib import Path
from typing import Optional, Dict, Any

DB_PATH = Path(__file__).parent / "jobs.db"

BUSY_TIMEOUT_MS = 100
WRITE_ATTEMPTS = 5
WRITE_BUDGET_SECONDS = 2.0
SCHEMA_VERSION = 2


def get_connection() -> sqlite3.Connection:
    # Each operation owns its connection; FastAPI may move it between threads.
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}").close()
        conn.execute("PRAGMA foreign_keys=ON").close()
        return conn
    except BaseException:
        conn.close()
        raise


def is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return (code is not None and (code & 0xff) == sqlite3.SQLITE_BUSY) or "database is locked" in str(exc).lower()


def write_transaction(operation, *, initialize_wal=False):
    """Retry an entire DB-only transaction, reopening after every rollback.

    Callbacks must not perform network calls or other irreversible side effects.
    Busy waits plus exponential jitter share a two-second contention budget.
    """
    deadline = time.monotonic() + WRITE_BUDGET_SECONDS
    for attempt in range(WRITE_ATTEMPTS):
        try:
            with closing(get_connection()) as conn:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                conn.execute(f"PRAGMA busy_timeout={min(BUSY_TIMEOUT_MS, remaining_ms)}").close()
                # journal_mode cannot change inside a transaction. Only startup
                # negotiates persistent WAL, under the same bounded retry policy.
                if initialize_wal:
                    with closing(conn.execute("PRAGMA journal_mode=WAL")) as cur:
                        if cur.fetchone()[0].lower() != "wal":
                            raise RuntimeError("Could not enable SQLite WAL mode")
                with conn:
                    conn.execute("BEGIN IMMEDIATE").close()
                    return operation(conn)  # context commits before returning
        except sqlite3.OperationalError as exc:
            remaining = deadline - time.monotonic()
            if not is_busy(exc) or attempt + 1 == WRITE_ATTEMPTS or remaining <= 0:
                raise
            time.sleep(min(0.05 * 2 ** attempt + random.uniform(0, 0.025), remaining))

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
    col_list = ", ".join('"' + col.replace('"', '""') + '"' for col in cols)
    # Caller owns BEGIN IMMEDIATE and rollback, including normalization above.
    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='jobs' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
    ).fetchall()
    conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
    conn.execute(_CREATE_JOBS_SQL)
    conn.execute(f"INSERT INTO jobs ({col_list}) SELECT {col_list} FROM jobs_legacy")
    conn.execute("DROP TABLE jobs_legacy")
    for obj in objects:
        conn.execute(obj[0])


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
    def migrate(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
        if version < 1:
            conn.execute(_CREATE_JOBS_SQL)
            _migrate_status_check(conn)
        if version < 2:
            _migrate_applied_at(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_track_status ON jobs(track, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_domain ON jobs(domain)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered ON jobs(date_discovered DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_applied_at ON jobs(applied_at)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    write_transaction(migrate, initialize_wal=True)

def all_job_ids() -> set:
    """Return the set of every job id currently stored.

    Used by the background worker to snapshot state before/after a run so it can
    count only the genuinely new roles inserted during that specific run.
    """
    with closing(get_connection()) as conn:
        return {row[0] for row in conn.execute("SELECT id FROM jobs").fetchall()}


def generate_job_id(company: str, title: str, location: str = "") -> str:
    """Dedup on normalized company + title + location.

    Normalization only lowercases and collapses whitespace: symbols like '+'
    and '#' are preserved so 'C++ Engineer' and 'C# Engineer' never collide,
    and the location component keeps same-title roles in different cities
    (NYC vs SF) as distinct rows.
    """
    raw = "|".join([re_clean(company), re_clean(title), re_clean(location)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def re_clean(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", (text or "").lower().strip())

def insert_job(job_data: Dict[str, Any]) -> bool:
    company = str(job_data.get("company") or "").strip()
    if not company or company.lower() == "nan":
        company = "Unknown Company"

    title = str(job_data.get("title") or "").strip()
    url = str(job_data.get("url") or "").strip()
    if not title or not url:
        return False

    job_id = job_data.get("id") or generate_job_id(
        company, title, str(job_data.get("location") or "")
    )
    
    def insert(conn):
        with closing(conn.execute("""
            INSERT INTO jobs (
                id, company, title, url, location, is_remote,
                track, term, domain, source, date_posted, raw_description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """, (
                job_id,
                company,
                title,
                url,
                job_data.get("location"),
                1 if job_data.get("is_remote") else 0,
                job_data.get("track", "unclear"),
                job_data.get("term"),
                job_data.get("domain", "General"),
                job_data.get("source", "scout"),
                job_data.get("date_posted"),
                job_data.get("raw_description", "")
            ))) as cur:
            return cur.rowcount == 1
    return write_transaction(insert)

if __name__ == "__main__":
    init_db()
