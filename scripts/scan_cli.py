#!/usr/bin/env python3
"""Scan2PDF CLI — Bilder (auch die aus dem Chat-Cache) -> ein OCR-PDF.

Beispiele:
  ./venv/bin/python scripts/scan_cli.py bild1.jpg bild2.jpg
  ./venv/bin/python scripts/scan_cli.py --latest 3          # 3 neueste Bilder aus ~/.hermes/image_cache
  ./venv/bin/python scripts/scan_cli.py --latest 2 --no-ocr
Gibt JSON mit dem Pfad des fertigen PDFs aus.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image  # noqa: E402

import pipeline  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CACHE = Path.home() / ".hermes" / "image_cache"
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}


def newest_cache(n, max_age_min=120):
    import time
    if not CACHE.exists():
        return []
    now = time.time()
    files = [p for p in CACHE.iterdir()
             if p.suffix.lower() in EXTS and (now - p.stat().st_mtime) < max_age_min * 60]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[-n:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*", type=Path)
    ap.add_argument("--latest", type=int, default=0,
                    help="N neueste Bilder aus ~/.hermes/image_cache (30 min)")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--out", default=str(BASE / "data" / "out"))
    args = ap.parse_args()

    files = list(args.images)
    if args.latest:
        files += newest_cache(args.latest)
    files = [f for f in files if f.exists() and f.suffix.lower() in EXTS]
    if not files:
        print(json.dumps({"error": "keine Bilder gefunden", "cache": str(CACHE)}))
        return 1

    imgs = [Image.open(f) for f in files]
    res = pipeline.process_job(imgs, BASE / "work", Path(args.out),
                               do_ocr=not args.no_ocr)
    res["sources"] = [str(f) for f in files]
    res["path"] = str(Path(args.out) / res["file"])
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
