# Scan2PDF

Bilder (Handyfotos oder Scans) rein → automatisch zugeschnitten und gerade ausgerichtet →
auf eine weiße A4-Seite gelegt → OCR → **ein** durchsuchbares PDF für alle Bilder zusammen.
Optional direkt in den Paperless-Eingang schieben.

## Ablauf

1. Bilder auswählen / fotografieren (mehrere = ein PDF, Reihenfolge = Auswahlreihenfolge)
2. „PDF erstellen“ → Ergebnis erscheint unten mit Download- und Paperless-Button

## Bildverarbeitung (`pipeline.py`)

| Schritt | Was passiert |
|---|---|
| Papiererkennung | Otsu-Schwelle + Projektionen: findet das helle Blatt auf dunklem/gemischtem Untergrund |
| Rand-Crop | dunkle Pixel = Inhalt, weiße Ränder werden weggeschnitten |
| Deskew | Projektionsprofil-Maximum über ±4° → schiefe Fotos werden gerade gezogen |
| A4 | Skalierung auf A4 minus 8 mm Rand, weißer Hintergrund, zentriert (Querformat bei Querformat-Bild) |
| OCR | `ocrmypdf -l deu+eng --deskew --optimize 1` in Container `scan2pdf-ocr` |

## OCR-Container

OCR läuft nicht im Host (kein apt/tesseract lokal), sondern in einem eigenen, dauerhaft
laufenden Container auf Basis des Paperless-Images:

```bash
docker run -d --name scan2pdf-ocr --restart unless-stopped \
  -v /home/sven/hermes-workspace/projects/scan2pdf/work:/work \
  --entrypoint /bin/sh ghcr.io/paperless-ngx/paperless-ngx:latest \
  -c "trap 'exit 0' TERM INT; while true; do sleep 3600; done"
```

Sprachen: `deu`, `eng`, `fra`, `ita`, `spa`.

## Betrieb

```bash
systemctl --user status tool-scan2pdf     # Status
journalctl --user -u tool-scan2pdf -f     # Logs
```

- Port: **5120**
- LAN: http://192.168.1.134:5120
- Speicherung: `data/out/*.pdf` (Metadaten in `data/results.json`)

## API

| Route | Zweck |
|---|---|
| `POST /api/process` | multipart `files` (+ `ocr=1`) → PDF bauen |
| `GET /api/results` | Liste der fertigen PDFs (inkl. OCR-Textauszug) |
| `GET /download/<datei>` | PDF herunterladen |
| `POST /api/paperless/<datei>` | Kopie in `/mnt/ssd/paperless-consume` |
| `GET /health` | Health-Check |

## Chat-Weg (ohne Browser)

Bilder, die im Chat ankommen, landen in `~/.hermes/image_cache`. Von dort direkt verarbeiten:

```bash
./venv/bin/python scripts/scan_cli.py --latest 2          # 2 neueste Chat-Bilder
./venv/bin/python scripts/scan_cli.py bild1.jpg bild2.jpg # explizite Dateien
./venv/bin/python scripts/scan_cli.py --latest 1 --no-ocr
```

Gibt JSON mit `path` (fertiges PDF), `pages`, `ocr`, `text` aus.

