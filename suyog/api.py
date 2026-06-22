"""
FastAPI service — wraps PageCapture for bulk screenshot/PDF capture.
"""
import asyncio
import io
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

from fastapi import FastAPI, HTTPException, Request
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

JOBS_DIR    = Path(os.getenv("JOBS_DIR", "/tmp/sb_jobs"))
MAX_URLS    = int(os.getenv("MAX_URLS", "20"))
API_KEY     = os.getenv("API_KEY", "")   # empty = no auth required
URL_TIMEOUT = int(os.getenv("URL_TIMEOUT", "120"))  # seconds per URL before giving up

JOBS_DIR.mkdir(parents=True, exist_ok=True)

_jobs: dict[str, dict] = {}
_executor = ThreadPoolExecutor(max_workers=1)  # one browser at a time

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

                    result.update({
                        "status":    "ok",
                        "page_name": outcome.get("page_name", ""),
                        "h1":        outcome.get("h1", ""),
                        "png":       f"/files/{job_id}/photos/{png_out.name}" if png_out else None,
                        "pdf":       f"/files/{job_id}/pdfs/{pdf_out.name}"   if pdf_out else None,
                    })
                    LOG.info("[%s] OK: %s", job_id[:8], url)
                except Exception as exc:
                    LOG.error("[%s] FAIL: %s — %s", job_id[:8], url, exc)
                    result.update({"status": "error", "error": str(exc)})
                finally:
                    job["results"].append(result)
                    job["done"] += 1

                time.sleep(random.uniform(
                    CFG["timing"]["inter_page_delay_min"],
                    CFG["timing"]["inter_page_delay_max"],
                ))

        job["status"] = "done"
    except Exception as exc:
        LOG.exception("[%s] Fatal: %s", job_id[:8], exc)
        job.update(status="failed", error=str(exc))
    finally:
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
    _jobs[job_id] = {"status": "queued", "total": len(req.urls), "done": 0, "results": []}
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_job, job_id, req.urls, req.formats)
    return {"job_id": job_id, "status": "queued", "url_count": len(req.urls)}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
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
<title>Screenshot Service</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .thumb { object-fit: cover; object-position: top; }
  .fade  { animation: fadeIn .3s ease; }
  @keyframes fadeIn { from { opacity:0; transform:translateY(6px); } to { opacity:1; } }
</style>
</head>
<body class="bg-gray-50 min-h-screen">
<div class="max-w-4xl mx-auto px-4 py-10">

  <!-- Header -->
  <div class="mb-8">
    <h1 class="text-3xl font-bold text-gray-900">Screenshot Service</h1>
    <p class="text-gray-500 mt-1">Full-page PNG &amp; PDF capture — overlays, cookie banners, and chat widgets removed automatically.</p>
  </div>

  <!-- Form -->
  <div class="bg-white rounded-2xl shadow-sm border border-gray-100 p-6 mb-6">
    <label class="block text-sm font-semibold text-gray-700 mb-2">
      URLs <span class="font-normal text-gray-400">(one per line, max """ + str(MAX_URLS) + """)</span>
    </label>
    <textarea id="urls" rows="8"
      class="w-full border border-gray-200 rounded-xl p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
      placeholder="https://example.com&#10;https://another-site.com"></textarea>

    <div class="flex flex-wrap items-center gap-6 mt-4">
      <div class="flex gap-4">
        <label class="flex items-center gap-2 text-sm cursor-pointer select-none">
          <input type="checkbox" id="fmt-png" checked class="w-4 h-4 accent-blue-600"> PNG
        </label>
        <label class="flex items-center gap-2 text-sm cursor-pointer select-none">
          <input type="checkbox" id="fmt-pdf" checked class="w-4 h-4 accent-blue-600"> PDF
        </label>
      </div>
""" + ("""
      <input id="api-key" type="password" placeholder="API key"
        class="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-48">
""" if API_KEY else "") + """
      <button id="submit-btn" onclick="submitJob()"
        class="ml-auto bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white font-semibold px-6 py-2 rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
        Capture
      </button>
    </div>
  </div>

  <!-- Progress -->
  <div id="progress-box" class="hidden bg-white rounded-2xl shadow-sm border border-gray-100 p-6 mb-6 fade">
    <div class="flex justify-between items-center mb-3">
      <span id="status-text" class="text-sm font-semibold text-gray-700">Queued…</span>
      <span id="progress-count" class="text-sm text-gray-400">0 / 0</span>
    </div>
    <div class="bg-gray-100 rounded-full h-2 overflow-hidden">
      <div id="progress-bar" class="bg-blue-600 h-2 rounded-full transition-all duration-500" style="width:0%"></div>
    </div>
  </div>

  <!-- Results -->
  <div id="results-box" class="hidden fade">
    <div class="flex justify-between items-center mb-4">
      <h2 class="text-lg font-semibold text-gray-800">Results</h2>
      <button id="zip-btn"
        class="bg-gray-800 hover:bg-gray-900 text-white text-sm font-medium px-4 py-2 rounded-xl transition-colors">
        Download all as ZIP
      </button>
    </div>
    <div id="results-grid" class="grid grid-cols-1 sm:grid-cols-2 gap-4"></div>
  </div>

</div>

<script>
let currentJobId = null;
let pollTimer    = null;

async function submitJob() {
  const urlText = document.getElementById('urls').value.trim();
  const urls    = urlText.split('\\n').map(u => u.trim()).filter(Boolean);
  if (!urls.length) { alert('Enter at least one URL.'); return; }

  const formats = [];
  if (document.getElementById('fmt-png').checked) formats.push('png');
  if (document.getElementById('fmt-pdf').checked) formats.push('pdf');
  if (!formats.length) { alert('Select at least one format.'); return; }

  const apiKeyEl = document.getElementById('api-key');
  const api_key  = apiKeyEl ? apiKeyEl.value.trim() : '';

  const btn = document.getElementById('submit-btn');
  btn.disabled    = true;
  btn.textContent = 'Submitting…';

  try {
    const res  = await fetch('/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ urls, formats, api_key }),
    });
    const data = await res.json();
    if (!res.ok) { alert(data.detail || 'Request failed'); btn.disabled = false; btn.textContent = 'Capture'; return; }

    currentJobId = data.job_id;
    document.getElementById('progress-box').classList.remove('hidden');
    document.getElementById('results-box').classList.add('hidden');
    document.getElementById('results-grid').innerHTML = '';
    btn.textContent = 'Running…';
    pollTimer = setInterval(() => poll(data.job_id), 2500);
  } catch (e) {
    alert('Network error: ' + e.message);
    btn.disabled    = false;
    btn.textContent = 'Capture';
  }
}

async function poll(jobId) {
  try {
    const res = await fetch('/jobs/' + jobId);
    if (!res.ok) return;
    const job = await res.json();

    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    document.getElementById('progress-bar').style.width   = pct + '%';
    document.getElementById('progress-count').textContent = job.done + ' / ' + job.total;
    document.getElementById('status-text').textContent    =
      job.status === 'queued'     ? 'Queued — waiting for browser…' :
      job.status === 'processing' ? 'Processing…' :
      job.status === 'done'       ? '✓ Done' : '✗ Failed';

    if (job.status === 'done' || job.status === 'failed') {
      clearInterval(pollTimer);
      const btn = document.getElementById('submit-btn');
      btn.disabled    = false;
      btn.textContent = 'Capture';
      if (job.results && job.results.length) showResults(job);
    }
  } catch (_) {}
}

function showResults(job) {
  const box = document.getElementById('results-box');
  box.classList.remove('hidden');

  document.getElementById('zip-btn').onclick = () => {
    window.location.href = '/download/' + currentJobId;
  };

  const grid = document.getElementById('results-grid');
  grid.innerHTML = job.results.map(r => {
    const isErr = r.status === 'error';
    return `
    <div class="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden fade">
      ${r.png
        ? `<a href="${r.png}" target="_blank" rel="noopener">
             <img src="${r.png}" class="thumb w-full h-44 bg-gray-100" loading="lazy" alt="">
           </a>`
        : `<div class="w-full h-44 bg-gray-100 flex items-center justify-center text-gray-300 text-sm">No image</div>`
      }
      <div class="p-4">
        <p class="text-sm font-semibold text-gray-800 truncate" title="${esc(r.page_name || r.url)}">${esc(r.page_name || r.url)}</p>
        <p class="text-xs text-gray-400 truncate mb-3" title="${esc(r.url)}">${esc(r.url)}</p>
        ${isErr
          ? `<p class="text-xs text-red-500 break-all">${esc(r.error)}</p>`
          : `<div class="flex gap-2">
               ${r.png ? `<a href="${r.png}" download class="text-xs bg-gray-100 hover:bg-gray-200 px-3 py-1 rounded-lg font-medium transition-colors">PNG</a>` : ''}
               ${r.pdf ? `<a href="${r.pdf}" download class="text-xs bg-gray-100 hover:bg-gray-200 px-3 py-1 rounded-lg font-medium transition-colors">PDF</a>` : ''}
             </div>`
        }
      </div>
    </div>`;
  }).join('');
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""
