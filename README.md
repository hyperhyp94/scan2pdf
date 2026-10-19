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
| Papiererkennung | Papier = **hell + farbneutral** (HSV). Holz/Tisch ist braun = gesättigt → fällt raus. Größte papierfarbene Fläche = Blatt |
| Perspektive | Ist das Blatt komplett im Bild: Viereck-Erkennung + `warpPerspective` → gerade Rechteck-Projektion |
| Fallback | Blatt angeschnitten: Blattform maskieren, alles außerhalb weiß übermalen |
| Untergrund | `_whiteout_background()`: gesättigte (braune) Restpixel werden weiß → kein Tisch mehr sichtbar |
| Begradigen | Projektionsprofil-Maximum, grob→fein über ±14°, vorzeichenunabhängig |
| Ausleuchtung | `_flatten_illumination()`: Bild ÷ weichgezeichnetes Eigenbild → Schatten/Falten weg, Papier überall weiß. **Der wichtigste Schritt für OCR-Qualität** |
| A4 | Passend auf A4 (210×297 mm) skaliert, 3 mm weißer Rand, zentriert (Querformat bleibt Querformat) |
| OCR | `tesseract --psm 6 -l deu+eng pdf` pro Seite (Tesseract schreibt das PDF selbst, unsichtbarer Textlayer) |
| Zusammenführen | Seiten-PDFs per PyMuPDF mergen → ein PDF für alle Bilder |
| Komprimieren | Ghostscript `/prepress`, 200 dpi JPEG q85 → typ. 0,4 MB/Seite |

### Warum nicht ocrmypdf?

`ocrmypdf` hat bei den Testfotos die Tabellenzone („Ihre Einzelposten", Beträge) **komplett
verworfen** — Tesseract direkt auf derselben Seite liest sie problemlos. Deshalb macht
Tesseract das PDF selbst, Ghostscript komprimiert danach (Textlayer bleibt erhalten).

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

