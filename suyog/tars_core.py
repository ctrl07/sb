"""manual_archive.py — standalone CLI archiver using TARS capture logic.

Completely self-contained. No local imports required.
Optionally reads %LOCALAPPDATA%/TARS/config.yaml when present; all values
have built-in defaults so the script runs with zero configuration.

Dependencies (pip install):
    playwright  pypdf  pyyaml

Usage
-----
  # archive one or more URLs directly
  python manual_archive.py https://example.com https://other.com

  # read URLs from a file
  python manual_archive.py --urls-file my_urls.txt

  # PDF only, custom output folder
  python manual_archive.py --pdf --output-dir C:/archive https://example.com

  # point at a non-default Chrome CDP endpoint
  python manual_archive.py --chrome http://localhost:9222 https://example.com

  # run non-interactively (no bot-check prompt, stdout log only)
  python manual_archive.py --skip-prompt --no-log-file https://example.com
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

import base64
import hashlib
import io
import logging
import os
import re
import time
import random
from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False


# ── Built-in defaults ────────────────────────────────────

_DEFAULTS: dict = {
    "chrome":   {"debug_url": "http://localhost:9222", "debug_port": 9222},
    "viewport": {"width": 1920, "height": 1080},
    "pdf":      {"margin_pt": 20, "border_pt": 0, "background_color": "#ffffff"},
    "timing": {
        "page_load_wait_ms":      3000,
        "stabilization_ms":       5000,
        "navigation_timeout_ms":  30000,
        "inter_page_delay_min":   1.5,
        "inter_page_delay_max":   4.0,
    },
    "hide": {},
    "output": {
        "dir":     str(Path.cwd() / "archive"),
        "photos":  str(Path.cwd() / "archive" / "photos"),
        "pdfs":    str(Path.cwd() / "archive" / "pdfs"),
        "log":     str(Path.cwd() / "archive" / "manual_archive.log"),
    },
}

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_APP_DATA     = Path(_LOCALAPPDATA) / "TARS" if _LOCALAPPDATA else None
_CONFIG_FILE  = _APP_DATA / "config.yaml" if _APP_DATA else None


def _load_config() -> dict:
    """Return merged config: built-in defaults + TARS config.yaml (if found)."""
    import copy, collections.abc
    cfg = copy.deepcopy(_DEFAULTS)

    if not (_YAML_OK and _CONFIG_FILE and _CONFIG_FILE.exists()):
        return cfg  # run on defaults only

    with _CONFIG_FILE.open("r", encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}

    # Deep-merge raw on top of defaults
    def _merge(base, override):
        for k, v in override.items():
            if isinstance(v, collections.abc.Mapping) and isinstance(base.get(k), collections.abc.Mapping):
                _merge(base[k], v)
            else:
                base[k] = v
    _merge(cfg, raw)

    # Resolve output_dir template and rebuild output paths
    d = cfg.get("output_dir", _DEFAULTS["output"]["dir"])
    d = d.replace("{localappdata}", _LOCALAPPDATA)
    cfg["output"] = {
        "dir":     d,
        "photos":  f"{d}/photos",
        "pdfs":    f"{d}/pdfs",
        "log":     f"{d}/manual_archive.log",
    }
    return cfg


def _setup_logging(log_file: Path | None, name: str = "manual_archive") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    logger.propagate = False
    return logger


def _slugify(url: str) -> str:
    base = re.sub(r"^https?://", "", url.lower())
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return base


def _build_css(cfg: dict) -> str:
    hide = cfg.get("hide", {})
    sections = []
    for group, selectors in hide.items():
        if selectors:
            sel_str = ",\n".join(selectors)
            sections.append(
                f"/* {group} */\n{sel_str} {{\n"
                f"  display: none !important; visibility: hidden !important;\n}}"
            )
    bg = cfg["pdf"]["background_color"]
    return "\n".join([
        "@media print { html, body { width: auto !important; height: auto !important; } }",
        "@media print { a::after { content: '' !important; } }",
        "* { animation: none !important; transition: none !important; }",
        "* { print-color-adjust: exact !important; -webkit-print-color-adjust: exact !important; }",
        *sections,
        f"html {{ background: {bg} !important; }}",
        "body::after { content: none !important; }",
    ])


def _load_urls_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


# ── CDP connection ────────────────────────────────────────────────────────────

_WEBDRIVER_PATCH = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
)


@contextmanager
def _cdp_connect(debug_url: str, viewport: dict):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(debug_url)
        except Exception as exc:
            raise ConnectionError(str(exc)) from exc

        context = browser.contexts[0]
        context.add_init_script(_WEBDRIVER_PATCH)
        page = context.new_page()
        page.set_viewport_size(viewport)
        try:
            yield page
        finally:
            page.close()


# ── Font cache ────────────────────────────────────────────────────────────────

FONT_CACHE_DIR = (
    (_APP_DATA / "font_cache") if _APP_DATA
    else (Path.home() / ".tars_font_cache")
)
FONT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_FONT_MIME = {
    ".woff2": "font/woff2",
    ".woff":  "font/woff",
    ".ttf":   "font/ttf",
    ".otf":   "font/otf",
    ".eot":   "application/vnd.ms-fontobject",
}

_FONT_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "use.typekit.net",
    "use.fontawesome.com",
    "kit.fontawesome.com",
    "fonts.bunny.net",
)


def _font_route(route, request) -> None:
    url        = request.url
    ext        = Path(url.split("?")[0]).suffix.lower()
    cache_path = FONT_CACHE_DIR / hashlib.sha256(url.encode()).hexdigest()

    if cache_path.exists():
        mime = _FONT_MIME.get(ext, "font/woff2")
        route.fulfill(body=cache_path.read_bytes(),
                      headers={"Content-Type": mime,
                               "Cache-Control": "max-age=31536000"})
        return
    try:
        resp = route.fetch()
        body = resp.body()
        cache_path.write_bytes(body)
        route.fulfill(response=resp)
    except Exception:
        route.abort()


def _register_font_cache(page) -> None:
    page.route(
        "**/*",
        lambda route, request: (
            _font_route(route, request)
            if (request.resource_type == "font"
                or any(h in request.url for h in _FONT_HOSTS))
            else route.continue_()
        ),
    )


# ── Page capture helpers ──────────────────────────────────────────────────────

def _scroll_page_full(page) -> int:
    return page.evaluate("""
    async () => {
        const step  = window.innerHeight * 0.8;
        const delay = ms => new Promise(r => setTimeout(r, ms));
        const root  = document.documentElement || document.body || { scrollHeight: 0 };
        const total = root.scrollHeight;
        if (!total) return 0;
        for (let y = 0; y < total; y += step) {
            window.scrollTo(0, y);
            await delay(150 + Math.floor(Math.random() * 100));
        }
        window.scrollTo(0, 0);
        await delay(300);
        return (document.documentElement || document.body || { scrollHeight: 0 }).scrollHeight;
    }
    """)


def _cdp_get_content_height(page) -> int:
    cdp = page.context.new_cdp_session(page)
    try:
        m  = cdp.send("Page.getLayoutMetrics", {})
        cs = m.get("contentSize") or {}
        h  = int(cs.get("height") or 0)
        if not h:
            h = page.evaluate("(document.documentElement || document.body || { scrollHeight: 0 }).scrollHeight")
        return h
    finally:
        cdp.detach()


def _save_pdf_cdp(page, pdf_path: Path, content_h: int, viewport: dict) -> None:
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Emulation.setEmulatedMedia", {"media": "screen"})
        result = cdp.send("Page.printToPDF", {
            "printBackground":   True,
            "paperWidth":        max(1.0, viewport["width"] / 96.0),
            "paperHeight":       max(1.0, content_h / 96.0),
            "marginTop":         0,
            "marginBottom":      0,
            "marginLeft":        0,
            "marginRight":       0,
            "preferCSSPageSize": False,
        })
        pdf_path.write_bytes(base64.b64decode(result["data"]))
    finally:
        cdp.detach()


def _flatten_to_one_page(pdf_path: Path, margin_pt: float, border_pt: float) -> None:
    from pypdf.generic import DecodedStreamObject, ArrayObject, NameObject
    # Read into memory first to avoid Windows file-sharing violations
    pdf_bytes    = pdf_path.read_bytes()
    reader       = PdfReader(io.BytesIO(pdf_bytes))
    total_height = sum(float(p.mediabox.height) for p in reader.pages)
    width        = float(reader.pages[0].mediabox.width)
    writer  = PdfWriter()
    merged  = writer.add_blank_page(
        width  = width        + 2 * margin_pt,
        height = total_height + 2 * margin_pt,
    )
    if border_pt > 0:
        border_ops = (
            f"q {border_pt:.2f} w 0 0 0 RG "
            f"{margin_pt:.2f} {margin_pt:.2f} {width:.2f} {total_height:.2f} re S Q\n"
        ).encode()
        border_stream = DecodedStreamObject()
        border_stream.set_data(border_ops)
        merged[NameObject("/Contents")] = ArrayObject([writer._add_object(border_stream)])
    y = total_height + margin_pt
    for p in reader.pages:
        ph = float(p.mediabox.height)
        y -= ph
        merged.merge_transformed_page(p, (1, 0, 0, 1, margin_pt, y))
    with pdf_path.open("wb") as f:
        writer.write(f)


# ── Core capture ──────────────────────────────────────────────────────────────

def capture_url(page, url: str, photos_dir: Path, pdfs_dir: Path,
                do_pdf: bool, do_png: bool, css: str,
                timing: dict, viewport: dict,
                margin_pt: float, border_pt: float, log) -> None:
    slug = _slugify(url)

    # Set DPR=2 so the browser natively fetches 2x images from srcset, picture/source,
    # and CSS image-set/background — no DOM surgery needed.
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width":             viewport["width"],
            "height":            viewport["height"],
            "deviceScaleFactor": 2,
            "mobile":            False,
        })
    finally:
        cdp.detach()

    try:
        page.goto(url, wait_until="domcontentloaded",
                  timeout=timing["page_load_wait_ms"])
    except PWTimeout:
        pass  # DOM never settled; proceed with what loaded

    page.emulate_media(media="screen")
    _scroll_page_full(page)

    # Wait for JS-rendered / AJAX-injected content to settle after scroll.
    try:
        page.wait_for_load_state("networkidle", timeout=timing["navigation_timeout_ms"])
    except Exception:
        pass  # timeout is acceptable — capture whatever has loaded

    # Force-load lazy images at full resolution
    page.evaluate("""async () => {
        document.querySelectorAll('img[data-src]').forEach(img => {
            img.src = img.dataset.src;
            if (img.dataset.srcset) img.srcset = img.dataset.srcset;
        });
        document.querySelectorAll('img[data-srcset]').forEach(img => {
            img.srcset = img.dataset.srcset;
        });
        document.querySelectorAll('[data-bg]').forEach(el => {
            el.style.backgroundImage = 'url(' + el.dataset.bg + ')';
        });
        document.querySelectorAll('img[loading="lazy"]').forEach(img => {
            img.loading = 'eager';
        });
        const imagesArray = document.images ? Array.from(document.images) : [];
        const imgs = imagesArray.filter(i => !i.complete);
        await Promise.all(imgs.map(i => new Promise(res => {
            i.onload = i.onerror = res;
        })));
    }""")

    try:
        page.add_style_tag(content=css)
    except Exception:
        try:
            page.evaluate("""(css) => {
                const head = document.head || document.getElementsByTagName('head')[0] || document.documentElement;
                if (head) {
                    const style = document.createElement('style');
                    style.textContent = css;
                    head.appendChild(style);
                }
            }""", css)
        except Exception:
            pass
    page.wait_for_timeout(timing["stabilization_ms"])

    # Remove overlays / modals / sticky bars
    page.evaluate("""() => {
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        document.querySelectorAll('*').forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.display === 'none') return;
            if (s.position !== 'fixed' && s.position !== 'absolute') return;
            const z = parseInt(s.zIndex) || 0;
            if (z < 100) return;
            const r = el.getBoundingClientRect();
            if (r.width >= vw * 0.6 && r.height >= vh * 0.6) {
                el.remove();
            }
        });
        if (document.body) {
            document.body.classList.remove(
                'modal-open','overflow-hidden','noscroll','no-scroll','scroll-lock','body-locked'
            );
            document.body.style.removeProperty('overflow');
        }
    }""")

    if do_png:
        out = photos_dir / f"{slug}.png"
        page.screenshot(path=str(out), full_page=True)
        log.info(f"  PNG → {out}")

    if do_pdf:
        pdf_path = pdfs_dir / f"{slug}.pdf"

        # Kill parallax engines
        page.evaluate("""() => {
            try { if (window.skrollr && window.skrollr.get) window.skrollr.get().destroy(); } catch(e) {}
            try { if (window.AOS) window.AOS.init({ disable: true }); } catch(e) {}
        }""")

        # Freeze fixed/sticky elements so they don't repeat across printed page boundaries
        page.evaluate("""() => {
            document.querySelectorAll('*').forEach(el => {
                try {
                    const s = window.getComputedStyle(el);
                    if (s.position === 'fixed' || s.position === 'sticky') {
                        el.style.setProperty('position', 'absolute', 'important');
                    }
                } catch(e) {}
            });
        }""")

        # Convert vh units to px to avoid page-break artefacts
        vh_overrides = page.evaluate("""() => {
            var rules = [];
            var vh    = window.innerHeight;
            Array.from(document.styleSheets).forEach(function(sheet) {
                try {
                    Array.from(sheet.cssRules || []).forEach(function(rule) {
                        if (!rule.selectorText || !rule.style) return;
                        if (rule.style.cssText.indexOf('vh') === -1) return;
                        var overrides = [];
                        ['height', 'min-height', 'max-height'].forEach(function(prop) {
                            var val = rule.style.getPropertyValue(prop);
                            if (!val || val.indexOf('vh') === -1) return;
                            var num = parseFloat(val);
                            if (isNaN(num)) return;
                            overrides.push(prop + ': ' + Math.round(num * vh / 100) + 'px !important');
                        });
                        if (overrides.length) {
                            rules.push(rule.selectorText + ' { ' + overrides.join('; ') + ' }');
                        }
                    });
                } catch(e) {}
            });
            document.querySelectorAll('[style*="vh"]').forEach(function(el) {
                if (el.style.height && el.style.height.indexOf('vh') !== -1)
                    el.style.removeProperty('height');
                if (el.style.minHeight && el.style.minHeight.indexOf('vh') !== -1)
                    el.style.removeProperty('min-height');
            });
            return rules;
        }""")
        if vh_overrides:
            css_override = "\n".join(vh_overrides)
            try:
                page.add_style_tag(content=css_override)
            except Exception:
                try:
                    page.evaluate("""(css) => {
                        const head = document.head || document.getElementsByTagName('head')[0] || document.documentElement;
                        if (head) {
                            const style = document.createElement('style');
                            style.textContent = css;
                            head.appendChild(style);
                        }
                    }""", css_override)
                except Exception:
                    pass
            page.wait_for_timeout(400)

        content_h = _cdp_get_content_height(page)
        try:
            _save_pdf_cdp(page, pdf_path, content_h, viewport)
        except Exception:
            page.pdf(path=str(pdf_path), width=f"{viewport['width']}px",
                     height=f"{content_h}px", print_background=True)
        _flatten_to_one_page(pdf_path, margin_pt, border_pt)
        log.info(f"  PDF → {pdf_path}")

    # Restore default device metrics so the next URL starts clean.
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Emulation.clearDeviceMetricsOverride", {})
    finally:
        cdp.detach()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = ArgumentParser(
        prog="manual_archive",
        description="Manually archive one or more URLs to PDF/PNG using a running Chrome instance.",
    )
    parser.add_argument("urls", nargs="*", metavar="URL",
                        help="URLs to archive (can also use --urls-file)")
    parser.add_argument("--urls-file", metavar="FILE",
                        help="Text file with one URL per line")
    parser.add_argument("--pdf", action="store_true",
                        help="Capture PDF (default: both PDF and PNG)")
    parser.add_argument("--png", action="store_true",
                        help="Capture PNG (default: both PDF and PNG)")
    parser.add_argument("--output-dir", metavar="DIR",
                        help="Output folder (default: TARS config output_dir)")
    parser.add_argument("--chrome", metavar="URL", default=None,
                        help="Chrome CDP endpoint (default: from config, usually http://localhost:9222)")
    parser.add_argument("--skip-prompt", action="store_true",
                        help="Skip the homepage bot-check prompt")
    parser.add_argument("--no-log-file", action="store_true",
                        help="Log to stdout only, no log file")
    args = parser.parse_args()

    # ── Load config (falls back to built-in defaults if config absent)
    cfg = _load_config()

    timing     = cfg["timing"]
    viewport   = cfg["viewport"]
    margin_pt  = float(cfg["pdf"]["margin_pt"])
    border_pt  = float(cfg["pdf"].get("border_pt", 0))
    chrome_url = args.chrome or cfg["chrome"]["debug_url"]
    css        = _build_css(cfg)

    # ── Resolve output paths
    if args.output_dir:
        base       = Path(args.output_dir)
        log_file   = base / "manual_archive.log"
    else:
        base       = Path(cfg["output"]["dir"])
        log_file   = Path(cfg["output"]["log"])

    photos_dir = base / "photos"
    pdfs_dir   = base / "pdfs"
    base.mkdir(parents=True, exist_ok=True)
    photos_dir.mkdir(parents=True, exist_ok=True)
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    log = _setup_logging(None if args.no_log_file else log_file)

    # ── Collect URLs
    urls: list[str] = list(args.urls)
    if args.urls_file:
        file_urls = _load_urls_from_file(Path(args.urls_file))
        if not file_urls:
            log.warning(f"No URLs found in {args.urls_file}")
        urls.extend(file_urls)

    if not urls:
        parser.print_help()
        sys.exit(0)

    do_pdf = args.pdf or not (args.pdf or args.png)
    do_png = args.png or not (args.pdf or args.png)

    log.info(f"Archiving {len(urls)} URL(s)  |  PDF={do_pdf}  PNG={do_png}")
    log.info(f"Output: {base}")

    start_time = datetime.now()

    try:
        with _cdp_connect(chrome_url, viewport) as page:
            _register_font_cache(page)

            if not args.skip_prompt:
                preview_url = input("Enter a homepage URL to preview (blank to skip): ").strip()
                if preview_url:
                    preview = page.context.new_page()
                    preview.set_viewport_size(viewport)
                    preview.goto(preview_url, wait_until="domcontentloaded",
                                 timeout=timing["navigation_timeout_ms"])
                    input("Complete any bot checks, then press Enter to start capture...")
                    _ctx = page.context
                    preview.close()
                    # The blank capture tab may have been closed during the bot-check;
                    # verify it is still alive and open a fresh one if not.
                    try:
                        page.evaluate("1")
                    except Exception:
                        page = _ctx.new_page()
                        page.set_viewport_size(viewport)
                        _register_font_cache(page)

            for i, url in enumerate(urls, start=1):
                log.info(f"({i}/{len(urls)}): {url}")
                try:
                    capture_url(page, url, photos_dir, pdfs_dir,
                                do_pdf, do_png, css,
                                timing, viewport, margin_pt, border_pt, log)
                except Exception as exc:
                    log.warning(f"  FAILED {url} — {exc}")
                if i < len(urls):
                    time.sleep(random.uniform(timing["inter_page_delay_min"],
                                              timing["inter_page_delay_max"]))

    except ConnectionError:
        print(
            f"\nCould not connect to Chrome at {chrome_url}.\n"
            f"Launch Chrome with remote debugging enabled, then retry."
        )
        sys.exit(1)

    end_time = datetime.now()
    log.info(f"Started : {start_time:%Y-%m-%d %H:%M:%S}")
    log.info(f"Finished: {end_time:%Y-%m-%d %H:%M:%S}")
    log.info(f"Elapsed : {end_time - start_time}")


if __name__ == "__main__":
    main()