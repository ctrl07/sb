"""PageCapture — Page Object Model for full-page PNG + PDF capture.
"""
import base64
from pathlib import Path
import mycdp
import yaml

_CHALLENGE_TITLES = {"just a moment", "attention required", "checking your browser", "please wait"}


_BASE_CSS = (
    "@media print { html, body { width: auto !important; height: auto !important; } }\n"
    "@media print { a::after { content: '' !important; } }\n"
"* { animation: none !important; transition: none !important; }\n"
    "* { print-color-adjust: exact !important; -webkit-print-color-adjust: exact !important; }\n"
    "html { background: #ffffff !important; }\n"
    "body::after { content: none !important; }\n"
)


def load_config(path: Path) -> dict:
    defaults = {
        "viewport": {"width": 1920, "height": 1080},
        "timing": {
            "scroll_interval_ms":   600,
            "stabilization_ms":    4000,
            "inter_page_delay_min": 1.5,
            "inter_page_delay_max": 4.0,
        },
        "hide": {},
    }
    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for key, val in raw.items():
            defaults[key] = val
    return defaults


def _build_css(hide: dict) -> str:
    parts = [_BASE_CSS]
    for selectors in hide.values():
        if not selectors:
            continue
        sel_str = ",\n".join(selectors)
        parts.append(
            f"{sel_str} {{\n"
            f"  display: none !important;\n"
            f"  visibility: hidden !important;\n"
            f"}}"
        )
    return "\n".join(parts)


def _run(sb, cmd):
    return sb.cdp.loop.run_until_complete(sb.cdp.page.send(cmd))


def _build_selectors(hide: dict) -> list[str]:
    """Flat list of every CSS selector from the hide config."""
    result = []
    for selectors in hide.values():
        if selectors:
            result.extend(selectors)
    return result


class PageCapture:
    def __init__(self, sb, config: dict):
        self.sb           = sb
        self.viewport     = config.get("viewport", {"width": 1920, "height": 1080})
        self.timing       = config.get("timing", {})
        hide              = config.get("hide", {})
        self.css          = _build_css(hide)
        self._selectors   = _build_selectors(hide)
        self._cdp_active  = False

    def open(self, url: str):
        """Navigate and solve Turnstile — retries up to 5 times."""
        if self._cdp_active:
            self.sb.cdp.open(url)
        else:
            self.sb.activate_cdp_mode(url)
            self._cdp_active = True
        self.sb.sleep(2)
        self.sb.solve_captcha()
        self.sb.sleep(5)
        for _ in range(5):
            title = (self.sb.cdp.evaluate("document.title") or "").lower()
            if not any(t in title for t in _CHALLENGE_TITLES):
                break
            self.sb.solve_captcha()
            self.sb.sleep(5)

    def scroll(self):
        total = self.sb.cdp.evaluate(
            "(document.documentElement || document.body || {scrollHeight:0}).scrollHeight"
        )
        step = self.sb.cdp.evaluate("Math.round(window.innerHeight * 0.8)")
        steps = max(1, int(total / (step or 1)) + 1)
        for _ in range(steps):
            self.sb.cdp.scroll_down(amount=step)
            self.sb.sleep(0.1)
        self.sb.cdp.scroll_to_top()
        self.sb.sleep(0.3)

    def hide_overlays(self):
        escaped = self.css.replace("\\", "\\\\").replace("`", "\\`")
        self.sb.cdp.execute_script(
            f"(function(){{var s=document.createElement('style');"
            f"s.textContent=`{escaped}`;"
            f"(document.head||document.documentElement).appendChild(s);}})()"
        )
        self.sb.sleep(1)
        # DOM removal — remove all configured-selector elements so they can't
        # re-appear when full_page=True resizes the viewport for PNG capture.
        if self._selectors:
            import json
            sel_json = json.dumps(self._selectors)
            self.sb.cdp.evaluate(f"""
            (() => {{
                const sels = {sel_json};
                sels.forEach(sel => {{
                    try {{
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    }} catch(e) {{}}
                }});
            }})()
            """)
        # DOM sweep — remove remaining large fixed/absolute overlays
        self.sb.cdp.evaluate("""
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

    def _content_height(self) -> int:
        metrics = _run(self.sb, mycdp.page.get_layout_metrics())
        h = int(metrics[5].height) if metrics[5] else 0
        if not h:
            h = self.sb.cdp.evaluate(
                "(document.documentElement || document.body || {scrollHeight:0}).scrollHeight"
            )
        return h

    def _remove_configured_elements(self):
        """Remove all elements matching the hide-config selectors from the DOM."""
        if not self._selectors:
            return
        import json
        sel_json = json.dumps(self._selectors)
        self.sb.cdp.evaluate(f"""
        (() => {{
            const sels = {sel_json};
            sels.forEach(sel => {{
                try {{
                    document.querySelectorAll(sel).forEach(el => el.remove());
                }} catch(e) {{}}
            }});
        }})()
        """)

    def capture_png(self, path: Path):
        """Full-page PNG — expands viewport manually so we can re-sweep after
        the resize event fires (chat widgets re-inject on resize)."""
        height = self._content_height()
        w = self.viewport["width"]
        # Expand to full-page height — this triggers resize listeners
        _run(self.sb, mycdp.emulation.set_device_metrics_override(
            width=w, height=height, device_scale_factor=1, mobile=False
        ))
        # Re-remove anything that re-injected itself during the resize
        self._remove_configured_elements()
        data = _run(self.sb, mycdp.page.capture_screenshot(
            format_="png",
            clip=mycdp.page.Viewport(x=0, y=0, width=w, height=height, scale=1),
            capture_beyond_viewport=True,
        ))
        path.write_bytes(base64.b64decode(data))
        # Restore original viewport
        _run(self.sb, mycdp.emulation.set_device_metrics_override(
            width=w, height=self.viewport["height"], device_scale_factor=1, mobile=False
        ))

    def _flatten_positions(self):
        """Convert all fixed/sticky elements to static so they don't repeat on PDF pages.
        Called after PNG capture so the screenshot is not affected."""
        self.sb.cdp.evaluate("""
        (() => {
            document.querySelectorAll('*').forEach(el => {
                const pos = window.getComputedStyle(el).position;
                if (pos === 'fixed' || pos === 'sticky') {
                    el.style.setProperty('position', 'static', 'important');
                }
            });
        })()
        """)

    def capture_pdf(self, path: Path):
        """Single-page PDF via CDP printToPDF."""
        self._flatten_positions()
        height = self._content_height()
        _run(self.sb, mycdp.emulation.set_emulated_media(media="screen"))
        data, _ = _run(self.sb, mycdp.page.print_to_pdf(
            print_background=True,
            paper_width=max(1.0, self.viewport["width"] / 96.0),
            paper_height=max(1.0, (height + 150) / 96.0),
            margin_top=0.0,
            margin_bottom=0.0,
            margin_left=0.0,
            margin_right=0.0,
            prefer_css_page_size=True,
        ))
        path.write_bytes(base64.b64decode(data))

    def extract_data(self) -> dict:
        title = self.sb.cdp.evaluate("document.title") or ""
        try:
            h1 = self.sb.cdp.evaluate(
                "(document.querySelector('h1') || {innerText:''}).innerText"
            ).strip()
        except Exception:
            h1 = ""
        return {"page_name": title, "h1": h1}

    def run(self, url: str, png_path: Path, pdf_path: Path) -> dict:
        """Full capture pipeline for one URL. Returns page data dict."""
        self.open(url)
        self.scroll()
        self.sb.sleep(self.timing.get("stabilization_ms", 2500) / 1000)
        self.hide_overlays()
        self.capture_png(png_path)
        self.capture_pdf(pdf_path)
        return self.extract_data()
