import base64
import io
from pathlib import Path

import mycdp
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, ArrayObject, NameObject
from seleniumbase import SB

VIEWPORT_W = 1199
VIEWPORT_H = 900
URL = "https://www.mbofsmithtown.com/2019-model-year-specials/"


def _run(sb, cmd):
    return sb.cdp.loop.run_until_complete(sb.cdp.page.send(cmd))


def _scroll_full(sb):
    """Scroll page using sb.cdp.scroll_down — matches examples/cdp_mode pattern."""
    total = sb.cdp.evaluate(
        "(document.documentElement || document.body || {scrollHeight:0}).scrollHeight"
    )
    step = sb.cdp.evaluate("Math.round(window.innerHeight * 0.8)")
    steps = max(1, int(total / step) + 1)
    for _ in range(steps):
        sb.cdp.scroll_down(amount=step)
        sb.sleep(0.2)
    sb.cdp.scroll_to_top()
    sb.sleep(0.3)


def _get_content_height(sb):
    metrics = _run(sb, mycdp.page.get_layout_metrics())
    # metrics is a 6-tuple; [5] = cssContentSize (dom.Rect)
    h = int(metrics[5].height) if metrics[5] else 0
    if not h:
        h = sb.cdp.execute_script(
            "return (document.documentElement || document.body || {scrollHeight:0}).scrollHeight"
        )
    return h


def _force_lazy_images(sb):
    """Force lazy images to load using sb.cdp.evaluate — matches examples pattern."""
    # Standard lazy load patterns
    sb.cdp.evaluate("""
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
    """)
    # Swiper internal lazy load — call the API on all instances, then DOM fallback
    sb.cdp.evaluate("""
    try {
        // Trigger Swiper's own lazy.load() on every registered instance
        if (window.swiper && typeof window.swiper === 'object') {
            Object.values(window.swiper).forEach(function(s) {
                if (s && s.lazy && typeof s.lazy.load === 'function') {
                    try { s.lazy.load(); } catch(e) {}
                }
            });
        }
        // DOM fallback: force any remaining data-src images
        document.querySelectorAll('.swiper-lazy[data-src]').forEach(function(img) {
            img.src = img.dataset.src;
            img.classList.add('swiper-lazy-loaded');
            img.classList.remove('swiper-lazy');
            var p = img.parentElement &&
                img.parentElement.querySelector('.swiper-lazy-preloader');
            if (p) p.remove();
        });
        // Handle background lazy images
        document.querySelectorAll('.swiper-lazy[data-background]').forEach(function(el) {
            el.style.backgroundImage = 'url(' + el.dataset.background + ')';
            el.classList.add('swiper-lazy-loaded');
            el.classList.remove('swiper-lazy');
        });
    } catch(e) {}
    """)



def _remove_overlays(sb):
    # Remove known third-party widgets by selector
    sb.cdp.evaluate("""
    [
        '#matador-livechat-123789',
        '#matadorLiveChat123789IFrame',
        '#ae_app',
        '.termly-styles-root-b60a7d',
        '#podium-bubble',
        'div.cn-b13-container',
        '.di-action-bar',
        '.di-action-bar--desktop'
    ].forEach(sel => {
        document.querySelectorAll(sel).forEach(el => el.remove());
    });
    """)
    # Remove any remaining large fixed/absolute overlays by size
    sb.cdp.evaluate("""
    (() => {
        const vw = window.innerWidth, vh = window.innerHeight;
        document.querySelectorAll('*').forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.display === 'none') return;
            if (s.position !== 'fixed' && s.position !== 'absolute') return;
            if ((parseInt(s.zIndex) || 0) < 100) return;
            const r = el.getBoundingClientRect();
            if (r.width >= vw * 0.6 && r.height >= vh * 0.6) el.remove();
        });
        if (document.body) {
            document.body.classList.remove(
                'modal-open','overflow-hidden','noscroll','no-scroll','scroll-lock','body-locked'
            );
            document.body.style.removeProperty('overflow');
            document.body.style.removeProperty('overflow-y');
        }
    })()
    """)


_CSS = (
    "@media print { html, body { width: auto !important; height: auto !important; } }\n"
    "@media print { a::after { content: '' !important; } }\n"
    "* { animation: none !important; transition: none !important; }\n"
    "* { print-color-adjust: exact !important; -webkit-print-color-adjust: exact !important; }\n"
    "html { background: #ffffff !important; }\n"
    "body::after { content: none !important; }\n"
)


def _inject_css(sb, css):
    escaped = css.replace("\\", "\\\\").replace("`", "\\`")
    sb.cdp.execute_script(
        f"(function(){{var s=document.createElement('style');"
        f"s.textContent=`{escaped}`;"
        f"(document.head||document.documentElement).appendChild(s);}})()"
    )


def _save_pdf(sb, pdf_path, content_h):
    _run(sb, mycdp.emulation.set_emulated_media(media="screen"))
    data, _ = _run(sb, mycdp.page.print_to_pdf(
        print_background=True,
        paper_width=max(1.0, VIEWPORT_W / 96.0),
        paper_height=max(1.0, content_h / 96.0),
        margin_top=0.0,
        margin_bottom=0.0,
        margin_left=0.0,
        margin_right=0.0,
        prefer_css_page_size=False,
    ))
    Path(pdf_path).write_bytes(base64.b64decode(data))


def _flatten_pdf(pdf_path, margin_pt=20, border_pt=0):
    pdf_bytes = Path(pdf_path).read_bytes()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_height = sum(float(p.mediabox.height) for p in reader.pages)
    width = float(reader.pages[0].mediabox.width)
    writer = PdfWriter()
    merged = writer.add_blank_page(
        width=width + 2 * margin_pt,
        height=total_height + 2 * margin_pt,
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
    with Path(pdf_path).open("wb") as f:
        writer.write(f)


with SB(uc=True, test=True, window_size=f"{VIEWPORT_W},{VIEWPORT_H}") as sb:
    sb.activate_cdp_mode(URL)
    sb.sleep(1)
    sb.solve_captcha()
    sb.sleep(2)

    # Retry solve until inventory (#hits) is visible — Turnstile may need multiple passes
    for _ in range(3):
        if sb.cdp.is_element_present("#hits"):
            break
        sb.solve_captcha()
        sb.sleep(3)

    # Wait for vehicle inventory cards to be AJAX-injected
    sb.cdp.select("#hits", timeout=15)
    sb.sleep(3)

    # Verify actual rendered viewport (diagnostic)
    actual_w = sb.cdp.evaluate("window.innerWidth")
    print(f"[viewport] actual innerWidth: {actual_w}px (target: {VIEWPORT_W}px)")

    # Trigger lazy-loaded content via smooth incremental scroll
    _scroll_full(sb)
    sb.sleep(2)

    # Force lazy images (standard + Swiper internal lazy load)
    _force_lazy_images(sb)
    sb.sleep(2)

    # Inject CSS first — hide-overlays suppresses banners/widgets before DOM sweep
    _inject_css(sb, _CSS)
    sb.sleep(2)

    # DOM sweep — remove overlay nodes after CSS has settled
    _remove_overlays(sb)
    sb.sleep(1)

    # PNG — full-page CDP screenshot
    sb.cdp.loop.run_until_complete(
        sb.cdp.page.save_screenshot("screenshot.png", full_page=True)
    )

    # PDF — measure height, print, flatten to single page
    content_h = _get_content_height(sb)
    _save_pdf(sb, "screenshot.pdf", content_h)
    _flatten_pdf("screenshot.pdf", margin_pt=20, border_pt=0)
