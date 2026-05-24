import sys
import re
import csv
import contextlib
import logging
import random
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seleniumbase import SB
from page_capture import PageCapture, load_config

# Paths 
HERE         = Path(__file__).resolve().parent
URLS_FILE    = HERE / "urls.txt"
PHOTOS_DIR   = HERE / "photos"
PDFS_DIR     = HERE / "pdfs"
DONE_FILE    = HERE / "done_urls.txt"
SKIPPED_FILE = HERE / "skipped_urls.txt"
DATA_FILE    = HERE / "data.csv"
LOG_FILE     = HERE / "run.log"

# Helpers

def slugify(url: str) -> str:
    base = re.sub(r"^https?://", "", url.lower())
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("case")
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
PDFS_DIR.mkdir(parents=True, exist_ok=True)

cfg       = load_config(HERE / "config.yaml")
log       = setup_logging(LOG_FILE)
all_urls  = load_urls(URLS_FILE)
done_urls = load_completed(DONE_FILE)
pending   = [u for u in all_urls if u not in done_urls]

if not pending:
    log.info("No pending URLs. Add URLs to urls.txt and re-run.")
    sys.exit(0)

log.info(f"Starting batch: {len(pending)} pending / {len(all_urls)} total.")

with contextlib.ExitStack() as stack:
    skip_f = stack.enter_context(SKIPPED_FILE.open("a", encoding="utf-8"))
    done_f = stack.enter_context(DONE_FILE.open("a", encoding="utf-8"))
    csv_f  = stack.enter_context(DATA_FILE.open("w", newline="", encoding="utf-8"))
    writer = csv.DictWriter(csv_f, fieldnames=["url", "page_name", "h1"])
    writer.writeheader()

    with SB(uc=True, test=True, window_size=f"{cfg['viewport']['width']},{cfg['viewport']['height']}") as sb:
        page = PageCapture(sb, cfg)

        for i, url in enumerate(pending, start=1):
            slug = slugify(url)
            log.info(f"({i}/{len(pending)}): {url}")
            try:
                data = page.run(
                    url,
                    png_path=PHOTOS_DIR / f"{slug}.png",
                    pdf_path=PDFS_DIR   / f"{slug}.pdf",
                )
                writer.writerow({"url": url, **data})
                csv_f.flush()
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
