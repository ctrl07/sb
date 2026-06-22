"""
FastAPI service — wraps PageCapture for bulk screenshot/PDF capture.
Designed to be called by the Cloudflare Worker (CF handles client auth + D1 + R2 serving).
Can also run standalone for local dev (files written to JOBS_DIR, no R2/Worker needed).
"""
import asyncio
import io
import json
import logging
import os
import random
import re
import sys
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests as http
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, field_validator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from page_capture import PageCapture, load_config  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────
CFG       = load_config(HERE / "config.yaml")
LOG       = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JOBS_DIR       = Path(os.getenv("JOBS_DIR", "/tmp/sb_jobs"))
MAX_URLS       = int(os.getenv("MAX_URLS", "20"))
API_KEY        = os.getenv("API_KEY", "")
URL_TIMEOUT    = int(os.getenv("URL_TIMEOUT", "120"))

# Cloudflare integration — optional (falls back to local mode when unset)
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")   # shared secret with CF Worker
WORKER_URL     = os.getenv("WORKER_URL", "")        # e.g. https://screenshot-worker.workers.dev

# R2 via S3-compatible API — optional (files stay local when unset)
R2_ACCOUNT_ID  = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY  = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY  = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET      = os.getenv("R2_BUCKET_NAME", "screenshot-files")

# Supabase — persistent job store (optional; falls back to in-memory when unset)
SUPABASE_URL   = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY", "")

PRODUCTION = bool(R2_ACCOUNT_ID and WORKER_URL and INTERNAL_TOKEN)

JOBS_DIR.mkdir(parents=True, exist_ok=True)

_jobs: dict[str, dict] = {}
_executor = ThreadPoolExecutor(max_workers=1)  # one browser at a time

# ── R2 client (lazy) ──────────────────────────────────────────────────────────
def _r2():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _upload_r2(local: Path, key: str):
    ct = "image/png" if key.endswith(".png") else "application/pdf"
    _r2().upload_file(str(local), R2_BUCKET, key, ExtraArgs={"ContentType": ct})
    LOG.info("R2 ← %s", key)


def _notify_worker(job_id: str, payload: dict):
    if not WORKER_URL or not INTERNAL_TOKEN:
        return
    try:
        http.post(
            f"{WORKER_URL}/internal/jobs/{job_id}",
            json=payload,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=10,
        )
    except Exception as e:
        LOG.warning("[%s] Worker notify failed: %s", job_id[:8], e)

# ── Supabase (persistent job store) ──────────────────────────────────────────
_sb_client = None

def _sb():
    global _sb_client
    if _sb_client is None and SUPABASE_URL and SUPABASE_KEY:
        from supabase import create_client
        _sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb_client


def _sb_upsert(job_id: str, job: dict):
    sb = _sb()
    if not sb:
        return
    try:
        sb.table("jobs").upsert({
            "id":      job_id,
            "status":  job.get("status", "queued"),
            "total":   job.get("total", 0),
            "done":    job.get("done", 0),
            "formats": ",".join(job.get("formats", ["png", "pdf"])),
            "results": job.get("results", []),
            "error":   job.get("error"),
        }).execute()
    except Exception as e:
        LOG.warning("Supabase write failed: %s", e)


def _sb_get_job(job_id: str) -> dict | None:
    sb = _sb()
    if not sb:
        return None
    try:
        res = sb.table("jobs").select("*").eq("id", job_id).maybe_single().execute()
        return res.data
    except Exception as e:
        LOG.warning("Supabase read failed: %s", e)
        return None

# ── Models ────────────────────────────────────────────────────────────────────
class CaptureRequest(BaseModel):
    urls: list[str]
    formats: list[str] = ["png", "pdf"]
    api_key: str = ""

    @field_validator("urls")
    @classmethod
    def check_urls(cls, v):
        if not v:
            raise ValueError("urls must not be empty")
        if len(v) > MAX_URLS:
            raise ValueError(f"max {MAX_URLS} URLs per request")
        cleaned = [u.strip() for u in v if u.strip()]
        if not cleaned:
            raise ValueError("no valid URLs provided")
        return cleaned

    @field_validator("formats")
    @classmethod
    def check_formats(cls, v):
        allowed = {"png", "pdf"}
        bad = [f for f in v if f not in allowed]
        if bad:
            raise ValueError(f"unknown formats: {bad}")
        return v

class InternalCaptureRequest(BaseModel):
    job_id: str
    urls: list[str]
    formats: list[str] = ["png", "pdf"]

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Screenshot Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Auth ──────────────────────────────────────────────────────────────────────
def _auth(key: str):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ── Job runner (blocking — runs in thread executor) ───────────────────────────
def _slugify(url: str) -> str:
    base = re.sub(r"^https?://", "", url.lower())
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")[:80]


def _capture_one(page, sb, url: str, png_out, pdf_out) -> dict:
    """Capture a single URL. Runs in a daemon thread so we can enforce a timeout."""
    page.open(url)
    page.scroll()
    sb.sleep(CFG["timing"].get("stabilization_ms", 2500) / 1000)
    page.hide_overlays()
    if png_out:
        page.capture_png(png_out)
    if pdf_out:
        page.capture_pdf(pdf_out)
    return page.extract_data()


def _run_job(job_id: str, urls: list[str], formats: list[str]):
    import threading
    from seleniumbase import SB

    # Start virtual display on Linux (Xvfb via sbvirtualdisplay)
    _display = None
    if os.name != "nt":
        try:
            from sbvirtualdisplay import Display
            _display = Display(visible=False, size=(1920, 1080))
            _display.start()
            LOG.info("[%s] Virtual display started", job_id[:8])
        except Exception as e:
            LOG.warning("[%s] Virtual display failed (%s) — Chrome may crash", job_id[:8], e)

    job     = _jobs[job_id]
    job_dir = JOBS_DIR / job_id
    png_dir = job_dir / "photos"
    pdf_dir = job_dir / "pdfs"
    png_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    job.update(status="processing", total=len(urls), done=0, results=[])
    _sb_upsert(job_id, job)

    try:
        w, h = CFG["viewport"]["width"], CFG["viewport"]["height"]
        with SB(uc=True, test=True, window_size=f"{w},{h}") as sb:
            page = PageCapture(sb, CFG)
            for url in urls:
                slug    = _slugify(url)
                png_out = png_dir / f"{slug}.png" if "png" in formats else None
                pdf_out = pdf_dir / f"{slug}.pdf" if "pdf" in formats else None
                result  = {"url": url}
                try:
                    # Run capture in a daemon thread so we can time it out
                    outcome = {}
                    error   = [None]

                    def _do():
                        try:
                            outcome.update(_capture_one(page, sb, url, png_out, pdf_out))
                        except Exception as exc:
                            error[0] = exc

                    t = threading.Thread(target=_do, daemon=True)
                    t.start()
                    t.join(URL_TIMEOUT)

                    if t.is_alive():
                        raise TimeoutError(f"Timed out after {URL_TIMEOUT}s")
                    if error[0]:
                        raise error[0]

                    if PRODUCTION:
                        png_url, pdf_url = None, None
                        if png_out and png_out.exists():
                            key = f"{job_id}/photos/{png_out.name}"
                            _upload_r2(png_out, key)
                            png_url = f"{WORKER_URL}/files/{key}"
                        if pdf_out and pdf_out.exists():
                            key = f"{job_id}/pdfs/{pdf_out.name}"
                            _upload_r2(pdf_out, key)
                            pdf_url = f"{WORKER_URL}/files/{key}"
                    else:
                        png_url = f"/files/{job_id}/photos/{png_out.name}" if png_out else None
                        pdf_url = f"/files/{job_id}/pdfs/{pdf_out.name}"   if pdf_out else None

                    result.update({
                        "status":    "ok",
                        "page_name": outcome.get("page_name", ""),
                        "h1":        outcome.get("h1", ""),
                        "png":       png_url,
                        "pdf":       pdf_url,
                    })
                    LOG.info("[%s] OK: %s", job_id[:8], url)
                except Exception as exc:
                    LOG.error("[%s] FAIL: %s — %s", job_id[:8], url, exc)
                    result.update({"status": "error", "error": str(exc)})
                finally:
                    job["results"].append(result)
                    job["done"] += 1
                    _sb_upsert(job_id, job)
                    if PRODUCTION:
                        _notify_worker(job_id, {
                            "status":  job["status"],
                            "results": job["results"],
                            "done":    job["done"],
                            "total":   job["total"],
                        })

                time.sleep(random.uniform(
                    CFG["timing"]["inter_page_delay_min"],
                    CFG["timing"]["inter_page_delay_max"],
                ))

        job["status"] = "done"
    except Exception as exc:
        LOG.exception("[%s] Fatal: %s", job_id[:8], exc)
        job.update(status="failed", error=str(exc))
    finally:
        _sb_upsert(job_id, job)
        if PRODUCTION:
            _notify_worker(job_id, {
                "status":  job.get("status", "failed"),
                "results": job.get("results", []),
                "done":    job.get("done", 0),
                "total":   job.get("total", 0),
                "error":   job.get("error"),
            })
        if _display:
            try:
                _display.stop()
            except Exception:
                pass

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/capture")
async def capture(req: CaptureRequest):
    _auth(req.api_key)
    job_id = str(uuid.uuid4())
    job = {"status": "queued", "total": len(req.urls), "done": 0, "results": [], "formats": req.formats}
    _jobs[job_id] = job
    _sb_upsert(job_id, job)
    asyncio.get_running_loop().run_in_executor(_executor, _run_job, job_id, req.urls, req.formats)
    return {"job_id": job_id, "status": "queued", "url_count": len(req.urls)}


@app.post("/internal/capture")
async def internal_capture(
    req: InternalCaptureRequest,
    x_internal_token: str | None = Header(None),
):
    if not INTERNAL_TOKEN or x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    job = {"status": "queued", "total": len(req.urls), "done": 0, "results": [], "formats": req.formats}
    _jobs[req.job_id] = job
    _sb_upsert(req.job_id, job)
    asyncio.get_running_loop().run_in_executor(_executor, _run_job, req.job_id, req.urls, req.formats)
    return {"ok": True}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id) or _sb_get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/files/{job_id}/{folder}/{filename}")
async def serve_file(job_id: str, folder: str, filename: str):
    path = JOBS_DIR / job_id / folder / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if ".." in str(path.resolve()):          # path traversal guard
        raise HTTPException(status_code=400)
    media = "image/png" if filename.endswith(".png") else "application/pdf"
    return StreamingResponse(
        open(path, "rb"), media_type=media,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/download/{job_id}")
async def download_zip(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Job status: {job['status']}")
    job_dir = JOBS_DIR / job_id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(job_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(job_dir))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=screenshots-{job_id[:8]}.zip"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


# ── Frontend ─────────────────────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SnapShot — Bulk Screenshot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<style>
* { font-family: 'Inter', system-ui, sans-serif; }
.card-enter { animation: cardEnter 0.4s cubic-bezier(0.16,1,0.3,1) both; }
@keyframes cardEnter { from { opacity:0; transform:translateY(12px) scale(0.98); } to { opacity:1; transform:none; } }
.prog-bar { transition: width 0.5s cubic-bezier(0.4,0,0.2,1); }
.thumb { object-fit:cover; object-position:top; }
.btn-main { background:linear-gradient(135deg,#6366f1,#4f46e5); box-shadow:0 4px 12px rgba(99,102,241,.35); transition:all .2s; }
.btn-main:hover:not(:disabled) { transform:translateY(-1px); box-shadow:0 6px 18px rgba(99,102,241,.45); }
.btn-main:active:not(:disabled) { transform:none; }
.btn-main:disabled { opacity:.55; cursor:not-allowed; box-shadow:none; }
.fmt-pill { transition:all .15s; user-select:none; }
.fmt-pill.on  { background:#eef2ff; color:#4338ca; border-color:#a5b4fc; }
.fmt-pill.off { background:transparent; color:#94a3b8; border-color:transparent; }
.pulse-dot::after { content:''; display:inline-block; width:6px; height:6px; border-radius:50%; background:currentColor; margin-left:5px; animation:pulse 1.1s ease-in-out infinite; vertical-align:middle; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
::-webkit-scrollbar { width:5px; }
::-webkit-scrollbar-thumb { background:#cbd5e1; border-radius:3px; }
</style>
</head>
<body class="bg-slate-50 min-h-screen">

<nav class="bg-white border-b border-slate-100 sticky top-0 z-10">
  <div class="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between">
    <div class="flex items-center gap-2.5">
      <div class="w-7 h-7 rounded-lg bg-indigo-600 flex items-center justify-center shrink-0">
        <svg class="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/>
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/>
        </svg>
      </div>
      <span class="font-semibold text-slate-900 text-sm">SnapShot</span>
      <span class="text-xs bg-indigo-50 text-indigo-600 font-medium px-2 py-0.5 rounded-full">Bulk</span>
    </div>
    <span class="text-xs text-slate-400 hidden sm:block">Full-page PNG &amp; PDF · overlays removed</span>
  </div>
</nav>

<div class="max-w-5xl mx-auto px-6 py-10">

  <div class="mb-7">
    <h1 class="text-2xl font-bold text-slate-900">Capture websites at scale</h1>
    <p class="text-slate-500 text-sm mt-1">Paste one URL per line. Cookie banners, chat widgets &amp; overlays are stripped automatically.</p>
  </div>

  <div class="bg-white rounded-2xl border border-slate-200 shadow-sm p-6 mb-4">
    <div class="flex items-center justify-between mb-2">
      <label for="urls" class="text-sm font-medium text-slate-700">URLs</label>
      <span id="url-counter" class="text-xs text-slate-400 tabular-nums">0 / """ + str(MAX_URLS) + """</span>
    </div>
    <textarea id="urls" rows="7" oninput="countUrls()"
      class="w-full border border-slate-200 rounded-xl px-3.5 py-3 font-mono text-sm text-slate-700 placeholder-slate-300 bg-slate-50 focus:outline-none focus:border-indigo-400 focus:bg-white transition-colors resize-none"
      placeholder="https://example.com&#10;https://another-site.com"></textarea>

    <div class="flex flex-wrap items-center gap-3 mt-4">
      <div class="flex gap-1 bg-slate-100 rounded-lg p-1">
        <button id="fmt-png" onclick="toggleFmt('png')" class="fmt-pill on text-xs font-semibold px-3 py-1.5 rounded-md border">PNG</button>
        <button id="fmt-pdf" onclick="toggleFmt('pdf')" class="fmt-pill on text-xs font-semibold px-3 py-1.5 rounded-md border">PDF</button>
      </div>
""" + ("""      <input id="api-key" type="password" placeholder="API key"
        class="border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-700 placeholder-slate-400 focus:outline-none focus:border-indigo-400 transition-colors w-40">
""" if API_KEY else "") + """      <button id="submit-btn" onclick="submitJob()"
        class="btn-main ml-auto text-white font-semibold text-sm px-7 py-2.5 rounded-xl">
        Capture
      </button>
    </div>
  </div>

  <div id="progress-box" class="hidden bg-white rounded-2xl border border-slate-200 shadow-sm px-6 py-4 mb-4 card-enter">
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-2">
        <span id="status-dot" class="w-2 h-2 rounded-full bg-amber-400 shrink-0"></span>
        <span id="status-text" class="text-sm font-semibold text-slate-700">Queued</span>
      </div>
      <span id="progress-count" class="text-xs tabular-nums text-slate-400">0 / 0</span>
    </div>
    <div class="flex items-center gap-3 mt-3">
      <div class="flex-1 bg-slate-100 rounded-full h-1.5 overflow-hidden">
        <div id="progress-bar" class="prog-bar bg-indigo-500 h-full rounded-full" style="width:0%"></div>
      </div>
      <span id="progress-pct" class="text-xs tabular-nums text-slate-400 w-8 text-right">0%</span>
    </div>
  </div>

  <div id="results-box" class="hidden">
    <div class="flex items-center justify-between mb-4">
      <h2 class="font-semibold text-slate-800 text-sm">Results <span id="result-count" class="font-normal text-slate-400"></span></h2>
      <button onclick="doZip()"
        class="flex items-center gap-1.5 text-xs font-semibold bg-slate-900 hover:bg-slate-700 text-white px-4 py-2 rounded-xl transition-colors">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
        </svg>
        Download ZIP
      </button>
    </div>
    <div id="results-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"></div>
  </div>

</div>

<script>
const MAX = """ + str(MAX_URLS) + """;
let jobId = null, timer = null;
const fmts = new Set(['png','pdf']);

function toggleFmt(f) {
  if (fmts.has(f)) { if (fmts.size > 1) { fmts.delete(f); setFmt(f, false); } }
  else { fmts.add(f); setFmt(f, true); }
}
function setFmt(f, on) {
  const el = document.getElementById('fmt-'+f);
  el.classList.toggle('on', on); el.classList.toggle('off', !on);
}
function countUrls() {
  const n = getUrls().length;
  const el = document.getElementById('url-counter');
  el.textContent = n + ' / ' + MAX;
  el.className = n > MAX ? 'text-xs text-red-500 tabular-nums' : 'text-xs text-slate-400 tabular-nums';
}
function getUrls() {
  return document.getElementById('urls').value.split('\\n').map(u=>u.trim()).filter(Boolean);
}

async function submitJob() {
  const u = getUrls();
  if (!u.length) { alert('Enter at least one URL.'); return; }
  if (!fmts.size) { alert('Select at least one format.'); return; }
  const ak = document.getElementById('api-key');
  const btn = document.getElementById('submit-btn');
  btn.disabled=true; btn.textContent='Submitting…';
  try {
    const res = await fetch('/capture', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ urls:u, formats:[...fmts], api_key: ak?ak.value.trim():'' }) });
    const data = await res.json();
    if (!res.ok) { alert(data.detail||'Failed'); btn.disabled=false; btn.textContent='Capture'; return; }
    jobId = data.job_id;
    document.getElementById('progress-box').classList.remove('hidden');
    document.getElementById('results-box').classList.add('hidden');
    document.getElementById('results-grid').innerHTML='';
    btn.textContent='Running…';
    timer = setInterval(()=>poll(jobId), 2500);
  } catch(e) { alert('Network error: '+e.message); btn.disabled=false; btn.textContent='Capture'; }
}

async function poll(id) {
  try {
    const r = await fetch('/jobs/'+id); if(!r.ok) return;
    const j = await r.json();
    const pct = j.total ? Math.round(j.done/j.total*100) : 0;
    document.getElementById('progress-bar').style.width = pct+'%';
    document.getElementById('progress-count').textContent = j.done+' / '+j.total;
    document.getElementById('progress-pct').textContent = pct+'%';
    const dot=document.getElementById('status-dot'), txt=document.getElementById('status-text');
    if (j.status==='queued')     { dot.className='w-2 h-2 rounded-full bg-amber-400 shrink-0'; txt.className='text-sm font-semibold text-slate-700'; txt.textContent='Queued'; }
    if (j.status==='processing') { dot.className='w-2 h-2 rounded-full bg-indigo-500 shrink-0'; txt.className='text-sm font-semibold text-indigo-600 pulse-dot'; txt.textContent='Capturing'; }
    if (j.status==='done')       { dot.className='w-2 h-2 rounded-full bg-green-500 shrink-0'; txt.className='text-sm font-semibold text-green-700'; txt.textContent='Done'; }
    if (j.status==='failed')     { dot.className='w-2 h-2 rounded-full bg-red-500 shrink-0'; txt.className='text-sm font-semibold text-red-600'; txt.textContent='Failed'; }
    if (j.status==='done'||j.status==='failed') {
      clearInterval(timer);
      const btn=document.getElementById('submit-btn'); btn.disabled=false; btn.textContent='Capture';
      if (j.results?.length) showResults(j);
    }
  } catch(_) {}
}

function showResults(j) {
  document.getElementById('results-box').classList.remove('hidden');
  document.getElementById('result-count').textContent='— '+j.results.length+' site'+(j.results.length!==1?'s':'');
  document.getElementById('results-grid').innerHTML=j.results.map((r,i)=>{
    const err=r.status==='error';
    return `<div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden card-enter" style="animation-delay:${i*60}ms">
      ${r.png
        ?`<a href="${r.png}" target="_blank" class="block h-44 overflow-hidden bg-slate-100">
            <img src="${r.png}" class="thumb w-full h-full hover:scale-105 transition-transform duration-500" loading="lazy" alt="">
          </a>`
        :`<div class="h-44 bg-slate-100 flex items-center justify-center">
            <svg class="w-8 h-8 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
          </div>`}
      <div class="p-4">
        ${err
          ?`<div class="flex gap-2 items-start">
              <span class="mt-0.5 shrink-0 w-4 h-4 rounded-full bg-red-100 flex items-center justify-center text-red-500 text-xs font-bold">!</span>
              <div><p class="text-sm font-medium text-slate-700 truncate">${esc(r.url)}</p>
                   <p class="text-xs text-red-500 mt-0.5 break-all">${esc(r.error)}</p></div>
             </div>`
          :`<p class="text-sm font-semibold text-slate-800 truncate" title="${esc(r.page_name||r.url)}">${esc(r.page_name||r.url)}</p>
            <p class="text-xs text-slate-400 truncate mb-3">${esc(r.url)}</p>
            <div class="flex gap-2">
              ${r.png?`<a href="${r.png}" download class="flex-1 text-center text-xs bg-indigo-50 hover:bg-indigo-100 text-indigo-700 font-semibold py-1.5 rounded-lg transition-colors">PNG</a>`:''}
              ${r.pdf?`<a href="${r.pdf}" download class="flex-1 text-center text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold py-1.5 rounded-lg transition-colors">PDF</a>`:''}
            </div>`}
      </div>
    </div>`;
  }).join('');
}

function doZip() { window.location.href='/download/'+jobId; }
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
</script>
</body>
</html>
"""
