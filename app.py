from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from config import SETTINGS
from db import DatabaseBusy, JobDB, JobFilters, VALID_STATUSES
from prep import get_prep_provider
import matcher
import worker

TEMPLATES_DIR = Path(__file__).parent / "templates"


class StatusUpdate(BaseModel):
    status: str


def create_app(settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store.init()
        yield

    app = FastAPI(title="Job Intel Hub", lifespan=lifespan)
    app.state.store = JobDB(settings.db_path)
    app.state.profiles = matcher.ProfileFile(settings.profile_path)

    @app.exception_handler(DatabaseBusy)
    async def database_busy(request: Request, exc: DatabaseBusy):
        return JSONResponse(
            status_code=503,
            content={"detail": "Database busy; retry shortly"},
            headers={"Retry-After": "1"},
        )

    @app.get("/api/jobs")
    def list_jobs(
        request: Request,
        track: Optional[str] = None,
        domain: Optional[str] = None,
        status: Optional[str] = "new",
        search: Optional[str] = None,
        sort: Optional[str] = "score_desc",
        source: Optional[str] = None,
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ):
        rows = request.app.state.store.list_jobs(JobFilters(
            track=track, domain=domain, status=status, source=source, search=search,
        ))
        # Stable score sorting keeps newest-first within ties.
        ranked = matcher.rank(rows, request.app.state.profiles.current(), by_score=sort != "date_desc")
        return ranked[offset:offset + limit]

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, request: Request):
        job = request.app.state.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return matcher.with_match(job, request.app.state.profiles.current())

    @app.get("/api/stats")
    def stats(request: Request):
        return request.app.state.store.stats()

    @app.get("/api/analytics")
    def analytics(request: Request):
        return {"scraper_running": worker.lock_held(settings.lock_path), **request.app.state.store.funnel()}

    @app.post("/api/jobs/{job_id}/status")
    def update_status(job_id: str, payload: StatusUpdate, request: Request):
        if payload.status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
        if not request.app.state.store.set_status(job_id, payload.status):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"ok": True, "job_id": job_id, "status": payload.status}

    @app.post("/api/jobs/{job_id}/prep")
    def generate_prep(job_id: str, request: Request):
        store = request.app.state.store
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        result = get_prep_provider().generate(job)
        if not store.save_prep(job_id, result.summary, result.bullets_text()):
            raise HTTPException(status_code=404, detail="Job not found")
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

    return app


app = create_app(SETTINGS)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=SETTINGS.host, port=SETTINGS.port, reload=True)
