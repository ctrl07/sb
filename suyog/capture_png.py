import os
import sys
import re
import logging
import platform
import random
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seleniumbase import SB
from page_capture import PageCapture, load_config

# Paths
HERE       = Path(__file__).resolve().parent
URLS_FILE  = HERE / "urls.txt"
PHOTOS_DIR = HERE / "photos"
DONE_FILE  = HERE / "done_urls.txt"
SKIPPED_FILE = HERE / "skipped_urls.txt"
LOG_FILE   = HERE / "run_png.log"


def slugify(url: str) -> str:
    base = re.sub(r"^https?://", "", url.lower())
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("capture_png")
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


# Main
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

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

log.info(f"Starting PNG-only batch: {len(pending)} pending / {len(all_urls)} total.")

_display = None
if platform.system() == "Linux":
    try:
        from pyvirtualdisplay import Display as _Display
        _display = _Display(visible=False, size=(cfg["viewport"]["width"], cfg["viewport"]["height"]))
        _display.start()
    except ImportError:
        pass

import contextlib

with contextlib.ExitStack() as stack:
    skip_f = stack.enter_context(SKIPPED_FILE.open("a", encoding="utf-8"))
    done_f = stack.enter_context(DONE_FILE.open("a", encoding="utf-8"))

    with SB(uc=True, test=True, window_size=f"{cfg['viewport']['width']},{cfg['viewport']['height']}") as sb:
        page = PageCapture(sb, cfg)

        for i, url in enumerate(pending, start=1):
            slug = slugify(url)
            log.info(f"({i}/{len(pending)}): {url}")
            try:
                page.open(url)
                page.scroll()
                sb.sleep(cfg["timing"].get("stabilization_ms", 2500) / 1000)
                page.hide_overlays()
                page.capture_png(PHOTOS_DIR / f"{slug}.png")
                done_f.write(f"{url}\n")
                done_f.flush()
                log.info("  Done")
            except Exception as exc:
                log.error(f"  SKIPPED {url} — {exc}")
                skip_f.write(f"{url}\n")
                skip_f.flush()

            time.sleep(random.uniform(
                cfg["timing"]["inter_page_delay_min"],
                cfg["timing"]["inter_page_delay_max"],
            ))

if _display is not None:
    _display.stop()
