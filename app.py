from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from pathlib import Path
from typing import Optional

from db import init_db, get_connection, VALID_STATUSES
from prep import get_prep_provider

DB_PATH = Path(__file__).parent / "jobs.db"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Job Intel Hub", lifespan=lifespan)


def get_db():
    return get_connection()


class StatusUpdate(BaseModel):
    status: str


@app.get("/api/jobs")
def list_jobs(
    track: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = "new",
    search: Optional[str] = None,
):
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
    if search:
        like = f"%{search.strip()}%"
        query += " AND (company LIKE ? OR title LIKE ? OR location LIKE ?)"
        params.extend([like, like, like])

    query += " ORDER BY date_discovered DESC"

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    return rows


@app.get("/api/stats")
def stats():
    with get_db() as conn:
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
def analytics():
    """Application funnel: total counts per pipeline stage plus recent momentum.

    `inbox` maps to the internal `new` status. `applied_last_7_days` counts
    roles whose applied_at timestamp falls within the trailing 7 days.
    """
    with get_db() as conn:
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
def update_status(job_id: str, payload: StatusUpdate):
    if payload.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
    with get_db() as conn:
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
def generate_prep(job_id: str):
    with get_db() as conn:
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
    return _INDEX_HTML


_INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Job Intel Hub</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root { color-scheme: dark; }
    body { background:#0a0a0b; }
    ::-webkit-scrollbar { width:10px; height:10px; }
    ::-webkit-scrollbar-thumb { background:#27272a; border-radius:6px; }
    .fade-in { animation: fade .18s ease; }
    @keyframes fade { from { opacity:0; transform:translateY(4px);} to {opacity:1;transform:none;} }
    kbd { font-size:10px; background:#18181b; border:1px solid #27272a; border-bottom-width:2px; border-radius:4px; padding:1px 5px; color:#a1a1aa; }
  </style>
</head>
<body class="text-zinc-100 min-h-screen font-sans antialiased" style="font-family:'Inter',ui-sans-serif,system-ui,sans-serif;">
  <div class="max-w-7xl mx-auto px-5 py-6">

    <!-- Header -->
    <header class="flex items-center justify-between mb-5">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-sm font-bold">JI</div>
        <div>
          <h1 class="text-base font-semibold tracking-tight text-white leading-none">Job Intel Hub</h1>
          <p class="text-xs text-zinc-500 mt-0.5">Autonomous sourcing &amp; application command center</p>
        </div>
      </div>
      <div class="text-xs text-zinc-600 flex items-center gap-2">
        <kbd>/</kbd> search <span class="text-zinc-800">·</span> <kbd>j</kbd>/<kbd>k</kbd> move <span class="text-zinc-800">·</span> <kbd>a</kbd> apply <span class="text-zinc-800">·</span> <kbd>s</kbd> save <span class="text-zinc-800">·</span> <kbd>x</kbd> dismiss
      </div>
    </header>

    <!-- Application funnel -->
    <section class="mb-5">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-semibold uppercase tracking-wider text-zinc-500">Application Funnel</h2>
        <span id="funnel-momentum" class="text-[11px] text-zinc-500"></span>
      </div>
      <div id="funnel" class="grid grid-cols-2 sm:grid-cols-5 gap-2.5"></div>
    </section>

    <!-- Metrics bar -->
    <div id="metrics" class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2.5 mb-5"></div>

    <!-- Controls -->
    <div class="flex flex-col lg:flex-row gap-3 lg:items-center justify-between mb-4">
      <div class="relative flex-1 max-w-md">
        <span class="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-600 text-sm">⌕</span>
        <input id="search" type="text" placeholder="Search company, title, location…" autocomplete="off"
          class="w-full bg-zinc-900/70 border border-zinc-800 rounded-lg pl-9 pr-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-indigo-500/70 focus:ring-1 focus:ring-indigo-500/30 transition" />
      </div>
      <div class="flex flex-wrap gap-2">
        <div class="flex bg-zinc-900/70 p-0.5 rounded-lg border border-zinc-800" id="track-filters">
          <button data-track="all"        class="track-btn px-3 py-1.5 rounded-md text-xs font-medium">All</button>
          <button data-track="internship" class="track-btn px-3 py-1.5 rounded-md text-xs font-medium">Internships</button>
          <button data-track="full_time"  class="track-btn px-3 py-1.5 rounded-md text-xs font-medium">Full-Time</button>
        </div>
        <div class="flex bg-zinc-900/70 p-0.5 rounded-lg border border-zinc-800" id="domain-filters">
          <button data-domain="all"     class="dom-btn px-3 py-1.5 rounded-md text-xs font-medium">All</button>
          <button data-domain="SWE"     class="dom-btn px-3 py-1.5 rounded-md text-xs font-medium">SWE</button>
          <button data-domain="Systems" class="dom-btn px-3 py-1.5 rounded-md text-xs font-medium">Systems</button>
          <button data-domain="AI/ML"   class="dom-btn px-3 py-1.5 rounded-md text-xs font-medium">AI/ML</button>
        </div>
      </div>
    </div>

    <!-- Status tabs -->
    <div class="flex gap-1 border-b border-zinc-800 mb-4" id="status-tabs">
      <button data-status="new"      class="tab px-3 py-2 text-sm font-medium border-b-2 -mb-px">Inbox</button>
      <button data-status="applied"  class="tab px-3 py-2 text-sm font-medium border-b-2 -mb-px">Applied</button>
      <button data-status="saved"    class="tab px-3 py-2 text-sm font-medium border-b-2 -mb-px">Saved</button>
      <button data-status="archived" class="tab px-3 py-2 text-sm font-medium border-b-2 -mb-px">Archived</button>
    </div>

    <div id="job-list" class="grid grid-cols-1 gap-2.5">
      <div class="text-center py-16 text-zinc-600 text-sm">Loading pipeline…</div>
    </div>
  </div>

  <script>
    const state = { track:'all', domain:'all', status:'new', search:'', jobs:[], cursor:0 };

    const ACTIVE_PILL = 'bg-zinc-100 text-zinc-900';
    const IDLE_PILL   = 'text-zinc-400 hover:text-zinc-100';

    function paintToggles() {
      document.querySelectorAll('.track-btn').forEach(b => {
        b.className = 'track-btn px-3 py-1.5 rounded-md text-xs font-medium transition ' + (b.dataset.track===state.track?ACTIVE_PILL:IDLE_PILL);
      });
      document.querySelectorAll('.dom-btn').forEach(b => {
        b.className = 'dom-btn px-3 py-1.5 rounded-md text-xs font-medium transition ' + (b.dataset.domain===state.domain?ACTIVE_PILL:IDLE_PILL);
      });
      document.querySelectorAll('.tab').forEach(b => {
        const on = b.dataset.status===state.status;
        b.className = 'tab px-3 py-2 text-sm font-medium border-b-2 -mb-px transition ' +
          (on ? 'border-indigo-500 text-white' : 'border-transparent text-zinc-500 hover:text-zinc-300');
      });
    }

    function metricCard(label, value, accent) {
      return `<div class="bg-zinc-900/60 border border-zinc-800 rounded-xl px-3.5 py-2.5">
        <div class="text-lg font-semibold ${accent||'text-white'} leading-none">${value}</div>
        <div class="text-[11px] uppercase tracking-wide text-zinc-500 mt-1.5">${label}</div>
      </div>`;
    }

    function funnelCard(label, value, accent, ring) {
      return `<div class="bg-zinc-900/60 border ${ring||'border-zinc-800'} rounded-xl px-3.5 py-3">
        <div class="text-2xl font-bold ${accent||'text-white'} leading-none">${value}</div>
        <div class="text-[11px] uppercase tracking-wide text-zinc-500 mt-2">${label}</div>
      </div>`;
    }

    async function loadAnalytics() {
      const a = await (await fetch('/api/analytics')).json();
      document.getElementById('funnel').innerHTML =
        funnelCard('Inbox', a.inbox, 'text-white') +
        funnelCard('Applied', a.applied, 'text-teal-400', 'border-teal-500/30') +
        funnelCard('Saved', a.saved, 'text-amber-400', 'border-amber-500/30') +
        funnelCard('Archived', a.archived, 'text-zinc-400') +
        funnelCard('Applied · 7d', a.applied_last_7_days, 'text-indigo-400', 'border-indigo-500/30');
      const m = document.getElementById('funnel-momentum');
      m.textContent = a.applied_last_7_days > 0
        ? `\u2191 ${a.applied_last_7_days} applied in the last 7 days`
        : 'No applications in the last 7 days';
    }

    async function loadStats() {
      const s = await (await fetch('/api/stats')).json();
      document.getElementById('metrics').innerHTML =
        metricCard('Total', s.total) +
        metricCard('Intern', s.internship, 'text-amber-400') +
        metricCard('Full-Time', s.full_time, 'text-emerald-400') +
        metricCard('SWE', s.swe, 'text-sky-400') +
        metricCard('Systems', s.systems, 'text-violet-400') +
        metricCard('AI/ML', s.aiml, 'text-rose-400') +
        metricCard('Applied', s.applied, 'text-teal-400');
    }

    // Lightweight fuzzy: subsequence match + contiguous bonus, client-side.
    function fuzzy(q, text) {
      if (!q) return true;
      q = q.toLowerCase(); text = (text||'').toLowerCase();
      if (text.includes(q)) return true;
      let i = 0;
      for (const ch of text) { if (ch === q[i]) i++; if (i === q.length) return true; }
      return false;
    }

    function visibleJobs() {
      const q = state.search.trim();
      if (!q) return state.jobs;
      return state.jobs.filter(j => fuzzy(q, `${j.company} ${j.title} ${j.location||''}`));
    }

    function nextStatusButtons(j) {
      const btn = (label, status, cls) =>
        `<button onclick="setStatus('${j.id}','${status}')" class="px-2.5 py-1.5 rounded-md text-xs font-medium ${cls} transition">${label}</button>`;
      let out = `<a href="${j.url}" target="_blank" rel="noopener" class="px-3 py-1.5 rounded-md text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition">Apply ↗</a>`;
      out += `<button onclick="prep('${j.id}')" class="px-2.5 py-1.5 rounded-md text-xs font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 transition">Prep</button>`;
      if (state.status !== 'applied') out += btn('Applied','applied','bg-zinc-800 hover:bg-emerald-900 hover:text-emerald-300 text-zinc-300');
      if (state.status !== 'saved')   out += btn('Save','saved','bg-zinc-800 hover:bg-amber-900 hover:text-amber-300 text-zinc-300');
      if (state.status !== 'archived')out += btn('✕','archived','bg-zinc-800 hover:bg-rose-950 hover:text-rose-400 text-zinc-400');
      return out;
    }

    function render() {
      const list = document.getElementById('job-list');
      const jobs = visibleJobs();
      if (state.cursor >= jobs.length) state.cursor = Math.max(0, jobs.length-1);
      if (!jobs.length) {
        list.innerHTML = '<div class="text-center py-16 text-zinc-600 text-sm">No positions in this view.</div>';
        return;
      }
      list.innerHTML = jobs.map((j,idx) => {
        const trackCls = j.track==='internship'
          ? 'bg-amber-500/10 text-amber-300 border-amber-500/30'
          : 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
        const focus = idx===state.cursor ? 'border-indigo-500/70 ring-1 ring-indigo-500/20' : 'border-zinc-800 hover:border-zinc-700';
        const prepHtml = j.summary
          ? `<div class="mt-3 pt-3 border-t border-zinc-800 text-xs text-zinc-400 space-y-1"><div class="text-zinc-300">${j.summary}</div><pre class="whitespace-pre-wrap text-zinc-500">${(j.tailored_bullets||'')}</pre></div>` : '';
        return `<div class="fade-in bg-zinc-900/50 border ${focus} rounded-xl p-4 transition" onclick="state.cursor=${idx};render()">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div class="min-w-0">
              <div class="flex items-center gap-2 mb-1.5 flex-wrap">
                <span class="text-[10px] px-1.5 py-0.5 rounded border font-medium ${trackCls}">${j.track.replace('_',' ').toUpperCase()}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded border border-zinc-700 bg-zinc-800/60 text-zinc-300 font-medium">${j.domain}</span>
                ${j.term?`<span class="text-[10px] text-zinc-500">${j.term}</span>`:''}
                ${j.is_remote?`<span class="text-[10px] px-1.5 py-0.5 rounded bg-sky-500/10 text-sky-300 border border-sky-500/30">Remote</span>`:''}
              </div>
              <h2 class="text-sm font-semibold text-white truncate">${j.title}</h2>
              <div class="text-xs text-zinc-400 mt-0.5">${j.company} <span class="text-zinc-600">· ${j.location||'—'}</span></div>
            </div>
            <div class="flex items-center gap-1.5 shrink-0" onclick="event.stopPropagation()">${nextStatusButtons(j)}</div>
          </div>
          ${prepHtml}
        </div>`;
      }).join('');
    }

    async function loadJobs() {
      const p = new URLSearchParams({ track:state.track, domain:state.domain, status:state.status });
      state.jobs = await (await fetch('/api/jobs?'+p.toString())).json();
      state.cursor = 0;
      render();
      loadStats();
      loadAnalytics();
    }

    async function setStatus(jobId, status) {
      await fetch(`/api/jobs/${jobId}/status`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status})
      });
      loadJobs();
    }

    async function prep(jobId) {
      const r = await (await fetch(`/api/jobs/${jobId}/prep`, {method:'POST'})).json();
      const j = state.jobs.find(x => x.id===jobId);
      if (j) { j.summary = r.summary; j.tailored_bullets = r.bullets.map(b=>'- '+b).join('\\n'); render(); }
    }

    // Wire up controls
    document.querySelectorAll('.track-btn').forEach(b => b.onclick = () => { state.track=b.dataset.track; paintToggles(); loadJobs(); });
    document.querySelectorAll('.dom-btn').forEach(b => b.onclick = () => { state.domain=b.dataset.domain; paintToggles(); loadJobs(); });
    document.querySelectorAll('.tab').forEach(b => b.onclick = () => { state.status=b.dataset.status; paintToggles(); loadJobs(); });

    const searchEl = document.getElementById('search');
    searchEl.addEventListener('input', e => { state.search = e.target.value; render(); });

    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
      if (e.target === searchEl) { if (e.key==='Escape') searchEl.blur(); return; }
      const jobs = visibleJobs();
      if (e.key === '/') { e.preventDefault(); searchEl.focus(); return; }
      if (e.key === 'j') { state.cursor = Math.min(jobs.length-1, state.cursor+1); render(); }
      if (e.key === 'k') { state.cursor = Math.max(0, state.cursor-1); render(); }
      const cur = jobs[state.cursor];
      if (!cur) return;
      if (e.key === 'a') setStatus(cur.id, 'applied');
      if (e.key === 's') setStatus(cur.id, 'saved');
      if (e.key === 'x') setStatus(cur.id, 'archived');
      if (e.key === 'p') prep(cur.id);
      if (e.key === 'Enter') window.open(cur.url, '_blank');
    });

    paintToggles();
    loadJobs();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
