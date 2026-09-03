from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from pathlib import Path
from typing import Optional

app = FastAPI(title="Job Intel Hub")
DB_PATH = Path(__file__).parent / "jobs.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

class StatusUpdate(BaseModel):
    status: str

@app.get("/api/jobs")
def list_jobs(track: Optional[str] = None, domain: Optional[str] = None, status: Optional[str] = "new"):
    conn = get_db()
    c = conn.cursor()
    
    query = "SELECT * FROM jobs WHERE 1=1"
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
        
    query += " ORDER BY date_discovered DESC"
    c.execute(query, params)
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

@app.post("/api/jobs/{job_id}/status")
def update_status(job_id: str, payload: StatusUpdate):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE jobs SET status = ? WHERE id = ?", (payload.status, job_id))
    conn.commit()
    conn.close()
    return {"ok": True, "job_id": job_id, "status": payload.status}

@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Job Intel Hub</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans">
  <div class="max-w-7xl mx-auto px-4 py-8">
    <header class="flex justify-between items-center mb-8 border-b border-slate-800 pb-4">
      <div>
        <h1 class="text-2xl font-bold tracking-tight text-white">Job Intel Hub</h1>
        <p class="text-sm text-slate-400">Autonomous Sourcing & Intake Review</p>
      </div>
      <div id="stats" class="text-xs text-slate-400 flex gap-4"></div>
    </header>

    <!-- Navigation & Filters -->
    <div class="flex flex-wrap gap-4 items-center justify-between mb-6">
      <!-- Track Switcher -->
      <div class="flex bg-slate-900 p-1 rounded-lg border border-slate-800" id="track-filters">
        <button onclick="setTrack('all')" class="px-4 py-1.5 rounded-md text-sm font-medium transition bg-blue-600 text-white" data-track="all">All Tracks</button>
        <button onclick="setTrack('internship')" class="px-4 py-1.5 rounded-md text-sm font-medium text-slate-400 hover:text-white transition" data-track="internship">Internships</button>
        <button onclick="setTrack('full_time')" class="px-4 py-1.5 rounded-md text-sm font-medium text-slate-400 hover:text-white transition" data-track="full_time">Full-Time Entry</button>
      </div>

      <!-- Domain Pills -->
      <div class="flex gap-2" id="domain-filters">
        <button onclick="setDomain('all')" class="px-3 py-1 text-xs rounded-full border border-slate-700 bg-slate-800 text-white font-medium" data-domain="all">All Domains</button>
        <button onclick="setDomain('SWE')" class="px-3 py-1 text-xs rounded-full border border-slate-800 bg-slate-900 text-slate-400 hover:text-white" data-domain="SWE">SWE</button>
        <button onclick="setDomain('Systems')" class="px-3 py-1 text-xs rounded-full border border-slate-800 bg-slate-900 text-slate-400 hover:text-white" data-domain="Systems">Systems</button>
        <button onclick="setDomain('AI/ML')" class="px-3 py-1 text-xs rounded-full border border-slate-800 bg-slate-900 text-slate-400 hover:text-white" data-domain="AI/ML">AI/ML</button>
      </div>
    </div>

    <!-- Job Cards List -->
    <div id="job-list" class="grid grid-cols-1 gap-4">
      <div class="text-center py-12 text-slate-500">Loading pipeline...</div>
    </div>
  </div>

  <script>
    let currentTrack = 'all';
    let currentDomain = 'all';

    function setTrack(track) {
      currentTrack = track;
      document.querySelectorAll('#track-filters button').forEach(b => {
        b.className = b.dataset.track === track 
          ? 'px-4 py-1.5 rounded-md text-sm font-medium transition bg-blue-600 text-white' 
          : 'px-4 py-1.5 rounded-md text-sm font-medium text-slate-400 hover:text-white transition';
      });
      loadJobs();
    }

    function setDomain(dom) {
      currentDomain = dom;
      document.querySelectorAll('#domain-filters button').forEach(b => {
        b.className = b.dataset.domain === dom
          ? 'px-3 py-1 text-xs rounded-full border border-slate-700 bg-slate-800 text-white font-medium'
          : 'px-3 py-1 text-xs rounded-full border border-slate-800 bg-slate-900 text-slate-400 hover:text-white';
      });
      loadJobs();
    }

    async function setStatus(jobId, status) {
      await fetch(`/api/jobs/${jobId}/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status })
      });
      loadJobs();
    }

    async function loadJobs() {
      const res = await fetch(`/api/jobs?track=${currentTrack}&domain=${currentDomain}&status=new`);
      const jobs = await res.json();
      const list = document.getElementById('job-list');
      
      if (!jobs.length) {
        list.innerHTML = '<div class="text-center py-12 text-slate-500">No active positions in this view.</div>';
        return;
      }

      list.innerHTML = jobs.map(j => `
        <div class="bg-slate-900 border border-slate-800 hover:border-slate-700 rounded-lg p-5 flex flex-col md:flex-row justify-between gap-4 transition">
          <div class="space-y-1.5">
            <div class="flex items-center gap-2">
              <span class="text-xs px-2 py-0.5 rounded font-medium ${j.track === 'internship' ? 'bg-amber-950 text-amber-300 border border-amber-800' : 'bg-emerald-950 text-emerald-300 border border-emerald-800'}">
                ${j.track.toUpperCase()}
              </span>
              <span class="text-xs px-2 py-0.5 rounded font-medium bg-slate-800 text-slate-300 border border-slate-700">
                ${j.domain}
              </span>
              ${j.term ? `<span class="text-xs text-slate-400">(${j.term})</span>` : ''}
              ${j.is_remote ? `<span class="text-xs bg-indigo-950 text-indigo-300 px-1.5 py-0.5 rounded border border-indigo-800">Remote</span>` : ''}
            </div>
            <h2 class="text-lg font-semibold text-white">${j.title}</h2>
            <div class="text-sm text-slate-300 font-medium">${j.company} <span class="text-slate-500 font-normal">· ${j.location || 'Location Not Specified'}</span></div>
          </div>
          <div class="flex items-center gap-2 self-start md:self-center">
            <a href="${j.url}" target="_blank" class="px-3.5 py-1.5 rounded text-xs font-semibold bg-blue-600 hover:bg-blue-500 text-white transition flex items-center gap-1">
              Apply ↗
            </a>
            <button onclick="setStatus('${j.id}', 'applied')" class="px-3 py-1.5 rounded text-xs font-medium bg-slate-800 hover:bg-emerald-900 hover:text-emerald-300 text-slate-300 transition">
              Mark Applied
            </button>
            <button onclick="setStatus('${j.id}', 'archived')" class="px-2.5 py-1.5 rounded text-xs font-medium bg-slate-800 hover:bg-rose-950 hover:text-rose-400 text-slate-400 transition">
              ✕
            </button>
          </div>
        </div>
      `).join('');
    }

    loadJobs();
  </script>
</body>
</html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
