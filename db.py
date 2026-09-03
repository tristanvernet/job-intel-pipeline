import sqlite3
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any

DB_PATH = Path(__file__).parent / "jobs.db"

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        conn.execute("""
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
            status TEXT DEFAULT 'new',
            raw_description TEXT,
            summary TEXT,
            tailored_bullets TEXT,
            outreach_note TEXT
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_track_status ON jobs(track, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_domain ON jobs(domain)")

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
