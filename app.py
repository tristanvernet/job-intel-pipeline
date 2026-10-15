from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from pathlib import Path
from typing import Optional

from config import SETTINGS
from db import JobDB, VALID_STATUSES, is_busy
from prep import get_prep_provider
import matcher

DB = JobDB(SETTINGS.db_path)
PROFILE_FILE = matcher.ProfileFile(SETTINGS.profile_path)


# Columns the list view actually renders; raw_description (KBs per row) is
# deliberately excluded from the bulk payload. Full rows still come from the
# per-job endpoints.
_JOB_LIST_COLS = (
    "id, company, title, url, location, is_remote, track, term, domain, "
    "source, date_posted, date_discovered, status, applied_at, summary, "
    "tailored_bullets"
)
_JOB_LIST_LIMIT = 500

TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    DB.init()
    yield


app = FastAPI(title="Job Intel Hub", lifespan=lifespan)


def get_db():
    """FastAPI dependency: yields a connection and ALWAYS closes it.

    `with conn:` only commits/rolls back -- it never closes. Relying on GC to
    release the handle leaks a file descriptor per request under uvicorn.
    """
    conn = DB.connect()
    try:
        yield conn
    finally:
        conn.close()


class StatusUpdate(BaseModel):
    status: str


@app.get("/api/jobs")
def list_jobs(
    track: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = "new",
    search: Optional[str] = None,
    sort: Optional[str] = "score_desc",
    source: Optional[str] = None,
    limit: int = Query(default=_JOB_LIST_LIMIT, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    conn=Depends(get_db),
):
    query = f"SELECT {_JOB_LIST_COLS} FROM jobs WHERE 1=1"
    params = []

    if track and track != "all":
        query += " AND track = ?"
        params.append(track)
    if domain and domain != "all":
        query += " AND domain = ?"
        params.append(domain)
    if status and status != "all":
        query += " AND status = ?"
        params.append(status)
    if source and source != "all":
        query += " AND source = ?"
        params.append(source)
    if search:
        like = f"%{search.strip()}%"
        query += " AND (company LIKE ? OR title LIKE ? OR location LIKE ?)"
        params.extend([like, like, like])

    query += " ORDER BY date_discovered DESC, id ASC"

    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    # The fetch is date-descending, so the stable score sort keeps newest-first within ties.
    ranked = matcher.rank(rows, PROFILE_FILE.current(), by_score=sort != "date_desc")
    return ranked[offset:offset + limit]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, conn=Depends(get_db)):
    """Full single-job payload: raw_description plus a match breakdown.

    `match.matched_terms` are profile terms that hit; `missing_terms` are the
    remaining weighted profile terms, so the UI can render fit vs. gap chips.
    """
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return matcher.with_match(dict(row), PROFILE_FILE.current())


@app.get("/api/stats")
def stats(conn=Depends(get_db)):
    def count(where="", args=()):
        return conn.execute(f"SELECT COUNT(*) FROM jobs{where}", args).fetchone()[0]

    return {
            "total": count(),
            "internship": count(" WHERE track = ?", ("internship",)),
            "full_time": count(" WHERE track = ?", ("full_time",)),
            "swe": count(" WHERE domain = ?", ("SWE",)),
            "systems": count(" WHERE domain = ?", ("Systems",)),
            "aiml": count(" WHERE domain = ?", ("AI/ML",)),
            "applied": count(" WHERE status = ?", ("applied",)),
        "saved": count(" WHERE status = ?", ("saved",)),
    }


def scraper_running():
    """Probe the worker's kernel lock; file existence alone is not liveness."""
    import worker
    if worker.fcntl is None:
        return None
    try:
        with open(SETTINGS.lock_path, "r") as lock:
            try:
                worker.fcntl.flock(lock, worker.fcntl.LOCK_SH | worker.fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            worker.fcntl.flock(lock, worker.fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return None


@app.get("/api/analytics")
def analytics(conn=Depends(get_db)):
    """Application funnel: total counts per pipeline stage plus recent momentum.

    `inbox` maps to the internal `new` status. `applied_last_7_days` counts
    roles whose applied_at timestamp falls within the trailing 7 days.
    """
    def count(where="", args=()):
        return conn.execute(f"SELECT COUNT(*) FROM jobs{where}", args).fetchone()[0]

    applied_last_7_days = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE status = 'applied' "
        "AND applied_at IS NOT NULL "
        "AND applied_at >= datetime('now', '-7 days')"
    ).fetchone()[0]

    return {
        "scraper_running": scraper_running(),
        "inbox": count(" WHERE status = ?", ("new",)),
        "applied": count(" WHERE status = ?", ("applied",)),
        "saved": count(" WHERE status = ?", ("saved",)),
        "archived": count(" WHERE status = ?", ("archived",)),
        "applied_last_7_days": applied_last_7_days,
        # Pipeline liveness: newest discovery timestamp across all rows
        # (NULL when the DB is empty -- the UI renders 'never scraped').
        "last_scraped": conn.execute(
            "SELECT MAX(date_discovered) FROM jobs"
        ).fetchone()[0],
    }


def write_mutation(operation):
    try:
        return DB.write(operation)
    except sqlite3.OperationalError as exc:
        if is_busy(exc):
            raise HTTPException(503, "Database busy; retry shortly", headers={"Retry-After": "1"}) from exc
        raise


@app.post("/api/jobs/{job_id}/status")
def update_status(job_id: str, payload: StatusUpdate):
    if payload.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")

    def update(conn):
        cur = conn.execute(
            "UPDATE jobs SET status = ?, "
            "applied_at = CASE WHEN ? = 'applied' THEN CURRENT_TIMESTAMP ELSE NULL END "
            "WHERE id = ?", (payload.status, payload.status, job_id),
        )
        try:
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Job not found")
        finally:
            cur.close()
        return {"ok": True, "job_id": job_id, "status": payload.status}
    return write_mutation(update)


@app.post("/api/jobs/{job_id}/prep")
def generate_prep(job_id: str, conn=Depends(get_db)):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(row)

    result = get_prep_provider().generate(job)
    def persist(write_conn):
        cur = write_conn.execute(
            "UPDATE jobs SET summary = ?, tailored_bullets = ? WHERE id = ?",
            (result.summary, result.bullets_text(), job_id),
        )
        try:
            if cur.rowcount == 0:
                raise HTTPException(404, "Job not found")
        finally:
            cur.close()
    write_mutation(persist)

    return {
        "ok": True,
        "job_id": job_id,
        "summary": result.summary,
        "talking_points": result.talking_points,
        "bullets": result.bullets,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=SETTINGS.host, port=SETTINGS.port, reload=True)
