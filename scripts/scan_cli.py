#!/usr/bin/env python3
"""Scan2PDF CLI — Bilder (auch die aus dem Chat-Cache) -> ein OCR-PDF.

Beispiele:
  ./venv/bin/python scripts/scan_cli.py bild1.jpg bild2.jpg
  ./venv/bin/python scripts/scan_cli.py --latest 3          # 3 neueste Bilder aus ~/.hermes/image_cache
  ./venv/bin/python scripts/scan_cli.py --latest 2 --no-ocr
  ./venv/bin/python scripts/scan_cli.py bild.jpg --label "Avacon Brief (avacon,feggendorf)" \\
      --paperless --modus antworten --ordner Privat
Gibt JSON mit dem Pfad des fertigen PDFs aus. Mit --paperless landet es (wie bei den Web-Seiten)
als "Titel (tag1,tag2).pdf" in consume/<modus>/<Ordner>/ und wird in der Ergebnisliste vermerkt.
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
    ap.add_argument("--label", default="", help='Titel mit Tags in Klammern: "Titel (tag1,tag2)"')
    ap.add_argument("--paperless", action="store_true", help="PDF in den Paperless-Eingang legen")
    ap.add_argument("--modus", default="archivieren", choices=["archivieren", "antworten"])
    ap.add_argument("--ordner", default="", help="Unterordner (= Ordner-Tag), muss existieren")
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
    res["label"] = args.label.strip()
    if args.paperless:
        import app  # gleiche Namens-/Ordnerlogik wie die Web-Seiten (startet keinen Server)
        try:
            dst = app._to_paperless(Path(res["path"]), res["label"], args.modus, args.ordner)
            res["paperless"] = f"{dst.parent.relative_to(app.PAPERLESS_CONSUME)}/{dst.name}"
        except Exception as exc:
            res["paperless_error"] = str(exc)
    if Path(args.out).resolve() == (BASE / "data" / "out").resolve():
        import time
        import app
        res["created"] = int(time.time())
        app._add_meta({k: v for k, v in res.items() if k != "sources"})
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
