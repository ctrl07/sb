"""seo_capture.py — Extract SEO signals from a list of URLs.

Captured fields
---------------
url, title, title_len, meta_description, meta_desc_len, canonical,
robots_meta, h1, h2s, h3s, og_title, og_description, og_image,
schema_types, word_count, internal_links, external_links,
images_missing_alt, status
"""
import contextlib
import csv
import json
import logging
import os
import platform
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seleniumbase import SB
from page_capture import PageCapture, load_config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE         = Path(__file__).resolve().parent
URLS_FILE    = HERE / "urls.txt"
DONE_FILE    = HERE / "seo_done_urls.txt"
SKIPPED_FILE = HERE / "seo_skipped_urls.txt"
DATA_FILE    = HERE / "seo_data.csv"
LOG_FILE     = HERE / "seo_run.log"

FIELDS = [
    "url", "title", "title_len", "meta_description", "meta_desc_len",
    "canonical", "robots_meta", "h1", "h2s", "h3s",
    "og_title", "og_description", "og_image",
    "schema_types", "word_count",
    "internal_links", "external_links", "images_missing_alt",
    "status",
]

# ---------------------------------------------------------------------------
# JS payload — runs in-page and returns all SEO data as a JSON string
# ---------------------------------------------------------------------------
_SEO_JS = r"""
(() => {
    const q  = (s) => document.querySelector(s);
    const qa = (s) => Array.from(document.querySelectorAll(s));
    const metaContent = (attr, val) => {
        const el = document.querySelector(`meta[${attr}="${val}"]`);
        return el ? (el.getAttribute('content') || '') : '';
    };

    const title      = document.title || '';
    const metaDesc   = metaContent('name', 'description');
    const canonical  = (q('link[rel="canonical"]') || {}).href || '';
    const robotsMeta = metaContent('name', 'robots');

    const h1  = (q('h1') || {innerText: ''}).innerText.trim();
    const h2s = qa('h2').map(e => e.innerText.trim()).filter(Boolean).join(' | ');
    const h3s = qa('h3').map(e => e.innerText.trim()).filter(Boolean).join(' | ');

    const ogTitle = metaContent('property', 'og:title');
    const ogDesc  = metaContent('property', 'og:description');
    const ogImage = metaContent('property', 'og:image');

    const schemaTypes = qa('script[type="application/ld+json"]')
        .map(s => { try { const d = JSON.parse(s.textContent); return d['@type'] || ''; } catch(e) { return ''; } })
        .flat()
        .filter(Boolean)
        .join(' | ');

    const bodyText  = (document.body || {innerText: ''}).innerText || '';
    const wordCount = bodyText.trim().split(/\s+/).filter(Boolean).length;

    const host = window.location.hostname;
    let internal = 0, external = 0;
    qa('a[href]').forEach(a => {
        try {
            const u = new URL(a.href, window.location.href);
            if (u.hostname === host) internal++;
            else if (u.protocol.startsWith('http')) external++;
        } catch(e) {}
    });

    const imagesMissingAlt = qa('img').filter(img => !img.getAttribute('alt')).length;

    return JSON.stringify({
        title, metaDesc, canonical, robotsMeta,
        h1, h2s, h3s,
        ogTitle, ogDesc, ogImage,
        schemaTypes, wordCount,
        internal, external, imagesMissingAlt,
    });
})()
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(url: str) -> str:
    base = re.sub(r"^https?://", "", url.lower())
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("seo_capture")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.propagate = False
    return logger


def load_urls(path: Path) -> list:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def load_completed(path: Path) -> set:
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def extract_seo(sb) -> dict:
    raw = sb.cdp.evaluate(_SEO_JS)
    if not raw:
        return {}
    data = json.loads(raw)
    return {
        "title":              data.get("title", ""),
        "title_len":          len(data.get("title", "")),
        "meta_description":   data.get("metaDesc", ""),
        "meta_desc_len":      len(data.get("metaDesc", "")),
        "canonical":          data.get("canonical", ""),
        "robots_meta":        data.get("robotsMeta", ""),
        "h1":                 data.get("h1", ""),
        "h2s":                data.get("h2s", ""),
        "h3s":                data.get("h3s", ""),
        "og_title":           data.get("ogTitle", ""),
        "og_description":     data.get("ogDesc", ""),
        "og_image":           data.get("ogImage", ""),
        "schema_types":       data.get("schemaTypes", ""),
        "word_count":         data.get("wordCount", 0),
        "internal_links":     data.get("internal", 0),
        "external_links":     data.get("external", 0),
        "images_missing_alt": data.get("imagesMissingAlt", 0),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

cfg      = load_config(HERE / "config.yaml")
log      = setup_logging(LOG_FILE)
_env_urls = os.environ.get("CAPTURE_URLS", "")
all_urls = (
    [u.strip() for u in re.split(r"\s+", _env_urls) if u.strip()]
    if _env_urls else load_urls(URLS_FILE)
)
done_urls = load_completed(DONE_FILE)
pending   = [u for u in all_urls if u not in done_urls]

if not pending:
    log.info("No pending URLs. Add URLs to urls.txt and re-run.")
    sys.exit(0)

log.info(f"Starting SEO batch: {len(pending)} pending / {len(all_urls)} total.")

_display = None
if platform.system() == "Linux":
    try:
        from pyvirtualdisplay import Display as _Display
        _display = _Display(visible=False, size=(cfg["viewport"]["width"], cfg["viewport"]["height"]))
        _display.start()
    except ImportError:
        pass

with contextlib.ExitStack() as stack:
    skip_f = stack.enter_context(SKIPPED_FILE.open("a", encoding="utf-8"))
    done_f = stack.enter_context(DONE_FILE.open("a", encoding="utf-8"))
    csv_f  = stack.enter_context(DATA_FILE.open("w", newline="", encoding="utf-8"))
    writer = csv.DictWriter(csv_f, fieldnames=FIELDS)
    writer.writeheader()

    with SB(uc=True, test=True, window_size=f"{cfg['viewport']['width']},{cfg['viewport']['height']}") as sb:
        page = PageCapture(sb, cfg)

        for i, url in enumerate(pending, start=1):
            log.info(f"({i}/{len(pending)}): {url}")
            try:
                page.open(url)
                sb.sleep(cfg["timing"].get("stabilization_ms", 2500) / 1000)

                seo = extract_seo(sb)
                writer.writerow({"url": url, "status": "ok", **seo})
                csv_f.flush()
                done_f.write(f"{url}\n")
                done_f.flush()
                log.info("  Done")
            except Exception as exc:
                log.error(f"  SKIPPED {url} — {exc}")
                writer.writerow({"url": url, "status": f"error: {exc}"})
                csv_f.flush()
                skip_f.write(f"{url}\n")
                skip_f.flush()

            time.sleep(random.uniform(
                cfg["timing"]["inter_page_delay_min"],
                cfg["timing"]["inter_page_delay_max"],
            ))

if _display is not None:
    _display.stop()

log.info(f"Done. Results saved to {DATA_FILE}")
