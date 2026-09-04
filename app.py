from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from pathlib import Path
from typing import Optional

from db import init_db, get_connection, VALID_STATUSES
from prep import get_prep_provider
from matcher import PROFILE_PATH, flatten_terms, load_profile, match_score

# Profile cache keyed on profile.json mtime: avoids re-reading and re-parsing
# the file on every API request while still picking up hand edits instantly.
_PROFILE_CACHE: dict = {}


def cached_profile() -> dict:
    try:
        mtime = PROFILE_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _PROFILE_CACHE.get("mtime") != mtime:
        _PROFILE_CACHE.clear()
        _PROFILE_CACHE.update(data=load_profile(), mtime=mtime)
    return _PROFILE_CACHE["data"]


# Columns the list view actually renders; raw_description (KBs per row) is
# deliberately excluded from the bulk payload. Full rows still come from the
# per-job endpoints.
_JOB_LIST_COLS = (
    "id, company, title, url, location, is_remote, track, term, domain, "
    "source, date_posted, date_discovered, status, applied_at, summary, "
    "tailored_bullets"
)
_JOB_LIST_LIMIT = 500

DB_PATH = Path(__file__).parent / "jobs.db"
TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Job Intel Hub", lifespan=lifespan)


def get_db():
    """FastAPI dependency: yields a connection and ALWAYS closes it.

    `with conn:` only commits/rolls back -- it never closes. Relying on GC to
    release the handle leaks a file descriptor per request under uvicorn.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
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
    if search:
        like = f"%{search.strip()}%"
        query += " AND (company LIKE ? OR title LIKE ? OR location LIKE ?)"
        params.extend([like, like, like])

    query += " ORDER BY date_discovered DESC LIMIT ?"
    params.append(_JOB_LIST_LIMIT)

    rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    # Attach a deterministic 0-100 match score to every job, then sort the
    # Inbox (and every view) by score descending. The fetch above is already
    # date-descending, so this stable sort keeps newest-first within ties.
    profile = cached_profile()
    terms = flatten_terms(profile)
    for row in rows:
        row["match_score"] = match_score(row, profile, _terms=terms)
    rows.sort(key=lambda r: r["match_score"], reverse=True)
    return rows


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
        "inbox": count(" WHERE status = ?", ("new",)),
        "applied": count(" WHERE status = ?", ("applied",)),
        "saved": count(" WHERE status = ?", ("saved",)),
        "archived": count(" WHERE status = ?", ("archived",)),
        "applied_last_7_days": applied_last_7_days,
    }


@app.post("/api/jobs/{job_id}/status")
def update_status(job_id: str, payload: StatusUpdate, conn=Depends(get_db)):
    if payload.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
    if payload.status == "applied":
        cur = conn.execute(
            "UPDATE jobs SET status = ?, applied_at = CURRENT_TIMESTAMP WHERE id = ?",
            (payload.status, job_id),
        )
    else:
        cur = conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (payload.status, job_id)
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job_id": job_id, "status": payload.status}


@app.post("/api/jobs/{job_id}/prep")
def generate_prep(job_id: str, conn=Depends(get_db)):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(row)

    result = get_prep_provider().generate(job)
    conn.execute(
        "UPDATE jobs SET summary = ?, tailored_bullets = ? WHERE id = ?",
        (result.summary, result.bullets_text(), job_id),
    )

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
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
