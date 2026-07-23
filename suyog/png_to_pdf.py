# /// script
# requires-python = ">=3.11"
# dependencies = ["img2pdf", "Pillow"]
# ///

import sys
import img2pdf
from pathlib import Path
from PIL import Image

HERE = Path(__file__).resolve().parent
INPUT_DIR = HERE / "out" / "photos"
OUTPUT_DIR = HERE / "out" / "pdfs"

# DPI to declare in the PDF. Higher = smaller page (more "zoomed in" pixels).
# 150 is a good default for 1x screenshots; use 300 if shooting at 2x scale.
PDF_DPI = 150


def get_image_dpi(png: Path) -> tuple[int, int]:
    with Image.open(png) as im:
        dpi = im.info.get("dpi")
        if dpi and dpi[0] > 0 and dpi[1] > 0:
            return (int(dpi[0]), int(dpi[1]))
    return (PDF_DPI, PDF_DPI)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not INPUT_DIR.exists():
        print(f"Input dir not found: {INPUT_DIR}", file=sys.stderr)
        sys.exit(1)

    pngs = sorted(INPUT_DIR.glob("*.png"))

    if not pngs:
        print("No PNG files found in photos/.", file=sys.stderr)
        sys.exit(1)

    print(f"Converting {len(pngs)} PNG(s) from {INPUT_DIR} to {OUTPUT_DIR} (fallback DPI={PDF_DPI})...\n")
    for png in pngs:
        dpi = get_image_dpi(png)
        out = OUTPUT_DIR / png.with_suffix(".pdf").name
        with open(out, "wb") as f:
            f.write(img2pdf.convert(str(png), dpi=dpi))
        print(f"  {png.name}  [{dpi[0]} dpi] -> {out}")

    print("\nDone.")


if __name__ == "__main__":
    main()