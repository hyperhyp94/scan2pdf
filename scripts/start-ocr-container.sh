#!/usr/bin/env bash
# OCR-Helfer-Container (Tesseract deu/eng) für Scan2PDF
set -euo pipefail
docker rm -f scan2pdf-ocr 2>/dev/null || true
docker run -d --name scan2pdf-ocr --restart unless-stopped \
  -v /home/sven/hermes-workspace/projects/scan2pdf/work:/work \
  --no-healthcheck --entrypoint /bin/sh ghcr.io/paperless-ngx/paperless-ngx:latest \
  -c "trap 'exit 0' TERM INT; while true; do sleep 3600; done"
docker ps --filter name=scan2pdf-ocr --format '{{.Names}}: {{.Status}}'
