#!/usr/bin/env python3
"""Scan2PDF — Bilder zuschneiden, auf A4 legen, OCR, als PDF zusammenfassen."""
import io
import json
import os
import shutil
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_from_directory
from PIL import Image

import pipeline

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
OUT = BASE / "data" / "out"
META = BASE / "data" / "results.json"
PAPERLESS_CONSUME = Path("/mnt/ssd/paperless-consume")

WORK.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024  # 120 MB


def _load_meta():
    if META.exists():
        try:
            return json.loads(META.read_text())
        except Exception:
            return []
    return []


def _save_meta(items):
    META.parent.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(items[:200], indent=1))


def _add_meta(entry):
    items = [i for i in _load_meta() if i.get("file") != entry["file"]]
    items.insert(0, entry)
    _save_meta(items)


PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<title>Scan2PDF</title>
<style>
  :root {
    --bg: #0f1115; --card: #181b22; --card2: #20242d; --line: #2b303b;
    --txt: #e8ebf0; --muted: #98a0ae; --acc: #4f8cff; --ok: #35c07a; --warn: #f0a13a;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--txt);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 16px 14px 40px; max-width: 720px; margin-inline: auto;
  }
  h1 { font-size: 20px; margin: 4px 0 2px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
  .drop {
    border: 2px dashed var(--line); border-radius: 16px; background: var(--card);
    padding: 26px 16px; text-align: center; transition: .15s; cursor: pointer;
  }
  .drop.over { border-color: var(--acc); background: #1b2333; }
  .drop .big { font-size: 34px; line-height: 1; margin-bottom: 8px; }
  .drop .t { font-weight: 600; font-size: 15px; }
  .drop .h { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .btns { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
  button, .btn {
    font: inherit; border: 0; border-radius: 12px; padding: 13px 18px; min-height: 46px;
    background: var(--card2); color: var(--txt); font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    text-decoration: none;
  }
  button.primary { background: var(--acc); color: #fff; flex: 1 1 200px; }
  button:disabled { opacity: .45; }
  .thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr));
            gap: 8px; margin-top: 14px; }
  .thumb { position: relative; border-radius: 10px; overflow: hidden; background: #000;
           aspect-ratio: 3/4; }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .thumb .x {
    position: absolute; top: 0; right: 0; width: 26px; height: 26px; min-height: 0;
    border-radius: 0 0 0 8px; background: #000a; color: #fff; font-size: 14px;
    padding: 0; display: flex; align-items: center; justify-content: center;
  }
  .thumb .n { position: absolute; left: 4px; bottom: 4px; font-size: 11px;
              background: #000b; padding: 1px 5px; border-radius: 5px; }
  .row { display: flex; align-items: center; gap: 10px; margin-top: 14px;
         color: var(--muted); font-size: 14px; }
  .row input { width: 20px; height: 20px; accent-color: var(--acc); }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
          padding: 14px; margin-top: 12px; }
  .card .name { font-weight: 600; font-size: 14px; word-break: break-all; }
  .card .meta { color: var(--muted); font-size: 12px; margin-top: 3px; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 20px;
           background: #35c07a22; color: var(--ok); margin-left: 6px; }
  .badge.warn { background: #f0a13a22; color: var(--warn); }
  .card .acts { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .card .acts .btn { padding: 10px 14px; min-height: 42px; font-size: 13px; }
  .card .acts .btn.p { background: #4f8cff22; color: #8ab4ff; }
  .status { margin-top: 14px; font-size: 14px; }
  .spinner { display: inline-block; width: 15px; height: 15px; border: 2px solid #fff5;
             border-top-color: #fff; border-radius: 50%; animation: spin .8s linear infinite;
             vertical-align: -2px; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .hint { color: var(--muted); font-size: 12px; margin-top: 18px; line-height: 1.5; }
  details summary { cursor: pointer; color: var(--muted); font-size: 12px; margin-top: 8px; }
  pre.txt { white-space: pre-wrap; font-size: 11px; color: #b9c2d0; max-height: 180px;
            overflow: auto; background: #0b0d11; padding: 10px; border-radius: 8px; }
  h2 { font-size: 14px; color: var(--muted); font-weight: 600; margin: 24px 0 6px;
       text-transform: uppercase; letter-spacing: .06em; }
</style>
</head>
<body>
  <h1>📄 Scan2PDF</h1>
  <div class="sub">Bilder rein → Ränder weg → A4 → OCR → ein PDF</div>

  <div class="drop" id="drop">
    <div class="big">📷</div>
    <div class="t">Bilder wählen oder hierher ziehen</div>
    <div class="h">JPG / PNG / HEIC · mehrere = ein PDF</div>
  </div>
  <input type="file" id="file" accept="image/*" multiple hidden>
  <div class="btns">
    <button type="button" id="pick">Bilder auswählen</button>
    <button type="button" id="camera">Kamera</button>
  </div>
  <input type="file" id="cam" accept="image/*" capture="environment" hidden>

  <div class="thumbs" id="thumbs"></div>

  <div class="row">
    <input type="checkbox" id="ocr" checked>
    <label for="ocr">OCR (durchsuchbares PDF, deutsch)</label>
  </div>

  <div class="btns">
    <button class="primary" id="go" disabled>PDF erstellen</button>
  </div>
  <div class="status" id="status"></div>

  <h2>Fertige PDFs</h2>
  <div id="results"></div>

  <div class="hint">
    Tipp: Seiten möglichst gerade und mit etwas Abstand fotografieren — Schatten am
    Rand werden als Inhalt erkannt. Reihenfolge = Reihenfolge der Auswahl.
  </div>

<script>
const files = [];
const $ = (id) => document.getElementById(id);
const drop = $('drop'), thumbBox = $('thumbs');

function render() {
  thumbBox.innerHTML = '';
  files.forEach((f, i) => {
    const url = URL.createObjectURL(f);
    const d = document.createElement('div');
    d.className = 'thumb';
    d.innerHTML = `<img src="${url}" alt=""><button class="x" data-i="${i}">✕</button>
                   <span class="n">${i + 1}</span>`;
    thumbBox.appendChild(d);
  });
  $('go').disabled = files.length === 0;
}

function addFiles(list) {
  for (const f of list) if (f.type.startsWith('image/')) files.push(f);
  render();
}

$('pick').onclick = () => $('file').click();
$('camera').onclick = () => $('cam').click();
drop.onclick = () => $('file').click();
$('file').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };
$('cam').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };
thumbBox.onclick = (e) => {
  const b = e.target.closest('button.x');
  if (!b) return;
  files.splice(+b.dataset.i, 1);
  render();
};
['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add('over');
}));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));

$('go').onclick = async () => {
  if (!files.length) return;
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  fd.append('ocr', $('ocr').checked ? '1' : '0');
  $('go').disabled = true;
  $('status').innerHTML = '<span class="spinner"></span>Wird verarbeitet … (OCR dauert ein paar Sekunden pro Seite)';
  try {
    const r = await fetch('/api/process', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    files.length = 0; render();
    $('status').innerHTML = j.ocr
      ? '✅ Fertig — ' + j.pages + ' Seite(n), OCR ok'
      : '⚠️ Fertig ohne OCR — ' + j.pages + ' Seite(n)';
    loadResults();
  } catch (err) {
    $('status').textContent = '❌ Fehler: ' + err.message;
  } finally {
    $('go').disabled = files.length === 0;
  }
};

async function loadResults() {
  const r = await fetch('/api/results');
  const items = await r.json();
  const box = $('results');
  box.innerHTML = items.length ? '' : '<div class="hint">Noch keine PDFs.</div>';
  for (const it of items) {
    const d = document.createElement('div');
    d.className = 'card';
    const date = it.created ? new Date(it.created * 1000).toLocaleString('de-DE') : '';
    const ocrBadge = it.ocr ? '<span class="badge">OCR</span>'
                            : '<span class="badge warn">kein OCR</span>';
    d.innerHTML = `
      <div class="name">${it.file}${ocrBadge}</div>
      <div class="meta">${it.pages} Seite(n) · ${it.size_kb} KB · ${date}</div>
      <div class="acts">
        <a class="btn" href="/download/${encodeURIComponent(it.file)}">⬇︎ Download</a>
        <button class="btn p" data-pl="${it.file}">📥 Paperless</button>
      </div>`;
    if (it.text) {
      const det = document.createElement('details');
      det.innerHTML = '<summary>Text anzeigen</summary><pre class="txt"></pre>';
      det.querySelector('pre').textContent = it.text;
      d.appendChild(det);
    }
    box.appendChild(d);
  }
}

$('results').onclick = async (e) => {
  const b = e.target.closest('button[data-pl]');
  if (!b) return;
  b.disabled = true; b.textContent = '…';
  try {
    const r = await fetch('/api/paperless/' + encodeURIComponent(b.dataset.pl), { method: 'POST' });
    const j = await r.json();
    b.textContent = r.ok ? '✅ übergeben' : '❌ ' + (j.error || 'Fehler');
  } catch (err) {
    b.textContent = '❌ Fehler';
  }
};

loadResults();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


KAMERA_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Scan2PDF Kamera</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --acc:#4f8cff; --txt:#e8eaf0; --ok:#3ccf7a; --err:#ff6b6b; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--txt);
         min-height:100vh; display:flex; flex-direction:column; align-items:center; padding:24px 16px; }
  h1 { font-size:22px; margin:8px 0 4px; }
  .sub { color:#8b93a7; font-size:14px; margin-bottom:24px; text-align:center; }
  .cam { width:100%; max-width:420px; padding:34px 0; font-size:28px; font-weight:700;
         border:0; border-radius:18px; background:var(--acc); color:#fff; cursor:pointer; }
  .cam:disabled { opacity:.5; }
  .status { margin-top:22px; font-size:16px; min-height:24px; text-align:center; }
  .ok { color:var(--ok); } .err { color:var(--err); }
  .spinner { display:inline-block; width:16px; height:16px; border:2px solid #555; border-top-color:var(--acc);
             border-radius:50%; animation:s 1s linear infinite; vertical-align:-3px; margin-right:8px; }
  @keyframes s { to { transform: rotate(360deg); } }
  .dl { display:inline-block; margin-top:14px; padding:12px 18px; border-radius:12px; background:var(--card);
        color:var(--txt); text-decoration:none; border:1px solid #2a2f3a; }
  .nav { margin-top:30px; color:#8b93a7; font-size:14px; }
  .nav a { color:var(--acc); }
  input[type=file] { display:none; }
</style></head><body>
  <h1>📷 Scan2PDF</h1>
  <div class="sub">Tippen → Foto machen → PDF wird automatisch erstellt (OCR, A4)</div>
  <input type="file" id="cam" accept="image/*" capture="environment">
  <button class="cam" id="btn">Foto aufnehmen</button>
  <div class="status" id="status"></div>
  <div class="nav"><a href="/">Zur Vollansicht (Mehrere Fotos, Paperless)</a></div>
<script>
const $ = (id) => document.getElementById(id);
$('btn').onclick = () => $('cam').click();
$('cam').onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  const fd = new FormData();
  fd.append('files', f);
  fd.append('ocr', '1');
  $('btn').disabled = true;
  $('status').innerHTML = '<span class="spinner"></span>Foto wird zu PDF verarbeitet …';
  try {
    const r = await fetch('/api/process', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    $('status').innerHTML = '<span class="ok">✅ PDF fertig — ' + j.pages + ' Seite, ' + j.size_kb + ' KB</span><br>' +
      '<a class="dl" href="/download/' + encodeURIComponent(j.file) + '">⬇︎ PDF herunterladen</a>';
  } catch (err) {
    $('status').innerHTML = '<span class="err">❌ ' + err.message + '</span>';
  } finally {
    $('btn').disabled = false;
  }
};
</script></body></html>"""


@app.route("/kamera")
def kamera():
    return render_template_string(KAMERA_PAGE)


@app.route("/api/process", methods=["POST"])
def api_process():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="keine Bilder"), 400
    images = []
    for f in files:
        try:
            images.append(Image.open(io.BytesIO(f.read())))
        except Exception as exc:
            return jsonify(error=f"{f.filename}: {exc}"), 400
    do_ocr = request.form.get("ocr", "1") == "1"
    try:
        result = pipeline.process_job(images, WORK, OUT, do_ocr=do_ocr)
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    result["created"] = int(os.path.getmtime(OUT / result["file"]))
    _add_meta(result)
    return jsonify(result)


@app.route("/api/results")
def api_results():
    items = _load_meta()
    alive = [i for i in items if (OUT / i["file"]).exists()]
    return jsonify(alive)


@app.route("/download/<path:name>")
def download(name):
    return send_from_directory(OUT, name, as_attachment=True)


@app.route("/api/paperless/<path:name>", methods=["POST"])
def api_paperless(name):
    src = OUT / name
    if not src.exists():
        return jsonify(error="Datei fehlt"), 404
    try:
        PAPERLESS_CONSUME.mkdir(parents=True, exist_ok=True)
        dst = PAPERLESS_CONSUME / name
        shutil.copy2(src, dst)
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(ok=True, target=str(dst))


@app.route("/health")
def health():
    return jsonify(ok=True, ts=int(time.time()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5120, debug=False)
