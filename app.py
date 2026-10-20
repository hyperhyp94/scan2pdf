#!/usr/bin/env python3
"""Scan2PDF — Bilder zuschneiden, auf A4 legen, OCR, als PDF zusammenfassen."""
import io
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_from_directory
from PIL import Image

import ipaddress

import pipeline
import webui_chat

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
OUT = BASE / "data" / "out"
META = BASE / "data" / "results.json"
PAPERLESS_CONSUME = Path(os.environ.get("S2P_CONSUME", "/mnt/ssd/paperless-consume"))

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


MODI = ("archivieren", "antworten")


def _folders():
    """Vorhandene Unterordner (= Ordner-Tags), wie bei Nextcloud."""
    base = PAPERLESS_CONSUME / "archivieren"
    try:
        return sorted(d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith((".", "_")))
    except OSError:
        return []


def _target_dir(modus="archivieren", ordner=""):
    modus = modus if modus in MODI else "archivieren"
    d = PAPERLESS_CONSUME / modus
    if ordner and ordner in _folders():          # nur existierende Ordner, kein Path-Traversal
        d = d / ordner
    return d


def _consume_name(label, fallback, folder=None):
    """Dateiname fuer Paperless. 'Titel (tag1,tag2)' -> 'Titel (tag1,tag2).pdf' —
    der Paperless-Tagger liest die Klammer am Ende als Tags (wie bei den Nextcloud-Ordnern)."""
    label = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", (label or "")).strip()
    label = re.sub(r"\s+", " ", label)
    if not label:
        return fallback
    m = re.search(r"\(([^()]*)\)\s*$", label)
    title = (label[:m.start()] if m else label).strip() or "Scan"
    tags = ",".join(t.strip() for t in m.group(1).split(",") if t.strip()) if m else ""
    base = f"{title} ({tags})" if tags else title
    name, n = f"{base}.pdf", 2
    folder = folder or PAPERLESS_CONSUME
    while (folder / name).exists():          # Kollision: Zaehler VOR der Klammer
        name = (f"{title} {n} ({tags}).pdf" if tags else f"{title} {n}.pdf")
        n += 1
    return name


def _to_paperless(src, label="", modus="archivieren", ordner=""):
    folder = _target_dir(modus, ordner)
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / _consume_name(label, src.name, folder)
    shutil.copy2(src, dst)
    return dst


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
    <input type="text" id="label" placeholder="Titel (tag1,tag2,tag3)" autocomplete="off" autocapitalize="off"
           style="width:100%;height:auto;padding:12px;font-size:16px;border-radius:10px;border:1px solid #2a2f3a;background:var(--card);color:inherit">
  </div>
  <div class="row">
    <input type="checkbox" id="pl">
    <label for="pl">direkt nach Paperless (Tags aus der Klammer)</label>
  </div>
  <div class="row" style="gap:8px">
    <select id="modus" style="flex:1;padding:12px;font-size:15px;border-radius:10px;border:1px solid #2a2f3a;background:var(--card);color:inherit"><option value="archivieren">📁 archivieren</option><option value="antworten">✉️ antworten</option></select>
    <select id="ordner" style="flex:1;padding:12px;font-size:15px;border-radius:10px;border:1px solid #2a2f3a;background:var(--card);color:inherit"><option value="">(kein Ordner)</option></select>
  </div>
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

fetch('/api/folders').then(r => r.json()).then(list => list.forEach(n => {
  const o = document.createElement('option'); o.value = o.textContent = n; $('ordner').appendChild(o); }));
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
  fd.append('label', $('label').value);
  fd.append('paperless', $('pl').checked ? '1' : '0');
  fd.append('modus', $('modus').value);
  fd.append('ordner', $('ordner').value);
  $('go').disabled = true;
  $('status').innerHTML = '<span class="spinner"></span>Wird verarbeitet … (OCR dauert ein paar Sekunden pro Seite)';
  try {
    const r = await fetch('/api/process', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    files.length = 0; render(); $('label').value = '';
    $('status').innerHTML = (j.paperless ? '📥 → Paperless: ' + j.paperless + '<br>' : '') + (j.ocr
      ? '✅ Fertig — ' + j.pages + ' Seite(n), OCR ok'
      : '⚠️ Fertig ohne OCR — ' + j.pages + ' Seite(n)');
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
    const r = await fetch('/api/paperless/' + encodeURIComponent(b.dataset.pl) + '?modus=' + $('modus').value +
      '&ordner=' + encodeURIComponent($('ordner').value), { method: 'POST' });
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
  #label { width:100%; max-width:420px; padding:14px; margin-bottom:10px; font-size:17px; border-radius:12px;
           border:1px solid #2a2f3a; background:var(--card); color:var(--txt); }
  .sel { display:flex; gap:8px; width:100%; max-width:420px; margin-bottom:10px; }
  .sel select { flex:1; min-width:0; padding:12px; font-size:15px; border-radius:12px; border:1px solid #2a2f3a;
                background:var(--card); color:var(--txt); }
  .chk { display:flex; gap:8px; align-items:center; margin-bottom:16px; color:#8b93a7; font-size:15px; }
  .chk input { width:20px; height:20px; }
</style></head><body>
  <h1>📷 Scan2PDF</h1>
  <div class="sub">Tippen → Foto machen → PDF wird automatisch erstellt (OCR, A4)</div>
  <input type="text" id="label" placeholder="Titel (tag1,tag2,tag3)" autocomplete="off" autocapitalize="off">
  <div class="sel">
    <select id="modus"><option value="archivieren">📁 archivieren</option><option value="antworten">✉️ antworten (Antwort nötig)</option></select>
    <select id="ordner"><option value="">(kein Ordner)</option></select>
  </div>
  <label class="chk"><input type="checkbox" id="pl" checked> direkt nach Paperless</label>
  <input type="file" id="cam" accept="image/*" capture="environment">
  <button class="cam" id="btn">Foto aufnehmen</button>
  <div class="status" id="status"></div>
  <div class="nav"><a href="/scan">📄 Scan-Modus (mehrere Seiten, Rand korrigieren)</a> · <a href="/">Vollansicht</a></div>
<script>
const $ = (id) => document.getElementById(id);
$('btn').onclick = () => $('cam').click();
['modus', 'ordner'].forEach(k => { const v = localStorage.getItem('s2p_' + k); if (v) $(k).value = v; });
fetch('/api/folders').then(r => r.json()).then(list => {
  list.forEach(n => { const o = document.createElement('option'); o.value = o.textContent = n; $('ordner').appendChild(o); });
  const v = localStorage.getItem('s2p_ordner'); if (v) $('ordner').value = v;
});
['modus', 'ordner'].forEach(k => $(k).onchange = () => localStorage.setItem('s2p_' + k, $(k).value));
$('cam').onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  const fd = new FormData();
  fd.append('files', f);
  fd.append('ocr', '1');
  fd.append('label', $('label').value);
  fd.append('paperless', $('pl').checked ? '1' : '0');
  fd.append('modus', $('modus').value);
  fd.append('ordner', $('ordner').value);
  $('btn').disabled = true;
  $('status').innerHTML = '<span class="spinner"></span>Foto wird zu PDF verarbeitet …';
  try {
    const r = await fetch('/api/process', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    const pl = j.paperless ? '<br>📥 Paperless: ' + j.paperless
             : (j.paperless_error ? '<br><span class="err">Paperless-Fehler: ' + j.paperless_error + '</span>' : '');
    $('status').innerHTML = '<span class="ok">✅ PDF fertig — ' + j.pages + ' Seite, ' + j.size_kb + ' KB</span>' + pl + '<br>' +
      '<a class="dl" href="/download/' + encodeURIComponent(j.file) + '">⬇︎ PDF herunterladen</a>';
    $('label').value = '';
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
    result["label"] = (request.form.get("label") or "").strip()
    if request.form.get("paperless") == "1":
        try:
            dst = _to_paperless(OUT / result["file"], result["label"],
                                request.form.get("modus", "archivieren"), request.form.get("ordner", ""))
            result["paperless"] = f"{dst.parent.relative_to(PAPERLESS_CONSUME)}/{dst.name}"
        except Exception as exc:
            result["paperless_error"] = str(exc)
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
    label = request.args.get("label") or next(
        (i.get("label", "") for i in _load_meta() if i.get("file") == name), "")
    try:
        dst = _to_paperless(src, label, request.args.get("modus", "archivieren"),
                            request.args.get("ordner", ""))
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(ok=True, target=f"{dst.parent.relative_to(PAPERLESS_CONSUME)}/{dst.name}")


@app.route("/api/folders")
def api_folders():
    return jsonify(_folders())


def _internal_request():
    """Nur Host und Docker-Netze (Paperless-Container) duerfen den Chat-Hook aufrufen."""
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("172.16.0.0/12")


@app.route("/api/chat-hook", methods=["POST"])
def api_chat_hook():
    """Paperless-Prozessor meldet ein 'antworten'-Dokument -> neuer Chat im Hermes-WebUI."""
    if not _internal_request():
        return jsonify(error="nicht erlaubt"), 403
    d = request.get_json(silent=True) or {}
    doc_id = str(d.get("document_id") or "").strip()
    if not doc_id.isdigit():
        return jsonify(error="document_id fehlt"), 400
    title = (d.get("title") or "Dokument").strip()
    lines = [f"📄 Neues Dokument zur Bearbeitung (Paperless-ID {doc_id})", "",
             f"Titel: {title}"]
    for key, label in (("correspondent", "Von"), ("document_type", "Typ"), ("deadline", "Frist")):
        if d.get(key):
            lines.append(f"{label}: {d[key]}")
    if d.get("tags"):
        lines.append("Tags: " + ", ".join(map(str, d["tags"])))
    if d.get("action_required"):
        lines.append("⚠️ Aktion erforderlich")
    if d.get("summary"):
        lines += ["", "Zusammenfassung:", str(d["summary"])[:1500]]
    if d.get("proaktiv"):
        lines += ["", str(d["proaktiv"])[:1000]]
    lines += ["", "Lies das Dokument bei Bedarf über Paperless (ID oben), fasse kurz zusammen "
                  "und frag mich, was ich damit tun soll (z. B. Antwort entwerfen, Termin eintragen, "
                  "nur ablegen). Entwürfe nur zeigen, nie senden."]
    try:
        sid, url = webui_chat.create_chat(f"📄 {title}"[:80], "\n".join(lines))
    except Exception as exc:
        return jsonify(error=str(exc)), 502
    return jsonify(ok=True, session_id=sid, url=url)


# ───────────────────────── Scan-Modus (Seite für Seite, Ränder korrigierbar) ─────────────────────────
SCANS = BASE / "data" / "scans"          # data/scans/<sid>/{meta.json, <pid>.src.jpg, <pid>.page.png}
SCANS.mkdir(parents=True, exist_ok=True)
_SID = re.compile(r"^[0-9a-f]{12}$")
SCAN_TTL = 3 * 86400


def _scan_dir(sid, create=False):
    if not _SID.match(sid or ""):
        return None
    d = SCANS / sid
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d if d.is_dir() else None


def _scan_meta(d):
    f = d / "meta.json"
    return json.loads(f.read_text()) if f.exists() else {"pages": []}


def _scan_save(d, meta):
    (d / "meta.json").write_text(json.dumps(meta))


def _scan_cleanup():
    now = time.time()
    for d in SCANS.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > SCAN_TTL:
            shutil.rmtree(d, ignore_errors=True)


def _page_public(sid, pg):
    return {"id": pg["id"], "quad": pg["quad"], "auto": pg.get("auto", False),
            "src": f"/api/scan/{sid}/img/{pg['id']}.src.jpg",
            "thumb": f"/api/scan/{sid}/img/{pg['id']}.page.jpg?v={pg.get('rev', 0)}"}


def _render_page(d, pg):
    """Zuschnitt aus Original + Ecken neu berechnen -> A4-Seite (PNG fuer OCR, JPG-Vorschau)."""
    frame = Image.open(d / f"{pg['id']}.src.jpg").convert("RGB")
    page = pipeline.to_a4(pipeline.extract_with_quad(frame, pg["quad"]))
    page.save(d / f"{pg['id']}.page.png", dpi=(pipeline.DPI, pipeline.DPI))
    prev = page.copy()
    prev.thumbnail((700, 990))
    prev.save(d / f"{pg['id']}.page.jpg", quality=80)
    pg["rev"] = pg.get("rev", 0) + 1


SCAN_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Scan2PDF Scan</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --acc:#4f8cff; --txt:#e8eaf0; --mut:#8b93a7; --ok:#3ccf7a; --err:#ff6b6b; --line:#2a2f3a; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { margin:0; font-family:system-ui,sans-serif; background:var(--bg); color:var(--txt); padding:16px 14px 120px; }
  h1 { font-size:20px; margin:4px 0 2px; } .sub { color:var(--mut); font-size:13px; margin-bottom:14px; }
  .btn { padding:15px 16px; border:0; border-radius:14px; font-size:17px; font-weight:600; background:var(--card); color:var(--txt);
         border:1px solid var(--line); cursor:pointer; }
  .btn.p { background:var(--acc); border-color:var(--acc); color:#fff; } .btn:disabled { opacity:.45; }
  .row { display:flex; gap:8px; margin:10px 0; } .row > * { flex:1; min-width:0; }
  input[type=text], select { width:100%; padding:13px; font-size:16px; border-radius:12px; border:1px solid var(--line);
         background:var(--card); color:var(--txt); }
  .chk { display:flex; gap:8px; align-items:center; color:var(--mut); font-size:15px; margin:8px 0; } .chk input { width:20px; height:20px; }
  .pages { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px; margin:14px 0; }
  .pg { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:6px; position:relative; }
  .pg img { width:100%; display:block; border-radius:6px; background:#fff; aspect-ratio:210/297; object-fit:contain; }
  .pg .n { position:absolute; left:10px; top:10px; background:#000a; border-radius:8px; padding:1px 8px; font-size:13px; }
  .pg .badge { font-size:11px; color:var(--mut); margin:4px 2px 0; } .badge.warn { color:#ffb454; }
  .pg .acts { display:flex; gap:5px; margin-top:6px; } .pg .acts button { flex:1; padding:8px 0; font-size:14px; border-radius:9px; border:1px solid var(--line); background:#0f1115; color:var(--txt); }
  .status { min-height:22px; margin:8px 0; font-size:15px; } .ok { color:var(--ok); } .err { color:var(--err); }
  .bar { position:fixed; left:0; right:0; bottom:0; padding:10px 14px calc(10px + env(safe-area-inset-bottom)); background:#0f1115ee;
         border-top:1px solid var(--line); display:flex; gap:8px; backdrop-filter:blur(8px); }
  .bar .btn { flex:1; padding:16px 6px; }
  /* Rand-Editor */
  #ed { position:fixed; inset:0; background:#000; z-index:20; display:none; flex-direction:column; }
  #ed .top { padding:10px 12px; display:flex; gap:8px; align-items:center; font-size:14px; color:var(--mut); }
  #edwrap { flex:1; position:relative; overflow:hidden; touch-action:none; }
  #edimg { position:absolute; left:0; top:0; user-select:none; -webkit-user-drag:none; }
  #edsvg { position:absolute; left:0; top:0; }
  #ed .bot { padding:10px 12px calc(10px + env(safe-area-inset-bottom)); display:flex; gap:8px; }
  .spinner { display:inline-block; width:15px; height:15px; border:2px solid #555; border-top-color:var(--acc); border-radius:50%;
             animation:s 1s linear infinite; vertical-align:-3px; margin-right:8px; } @keyframes s { to { transform:rotate(360deg); } }
  a { color:var(--acc); } input[type=file] { display:none; }
</style></head><body>
<h1>📄 Scan</h1>
<div class="sub">Seite fotografieren → Rand prüfen → nächste Seite → PDF erstellen</div>
<input type="text" id="label" placeholder="Titel (tag1,tag2,tag3)" autocomplete="off" autocapitalize="off">
<div class="row">
  <select id="modus"><option value="archivieren">📁 archivieren</option><option value="antworten">✉️ antworten</option></select>
  <select id="ordner"><option value="">(kein Ordner)</option></select>
</div>
<label class="chk"><input type="checkbox" id="pl" checked> direkt nach Paperless</label>
<div class="status" id="status"></div>
<div class="pages" id="pages"></div>
<input type="file" id="cam" accept="image/*" capture="environment">
<input type="file" id="gal" accept="image/*" multiple>
<div style="margin-top:20px;font-size:13px;color:var(--mut)"><a href="/kamera">Schnell-Kamera (1 Foto)</a> · <a href="/">Vollansicht</a></div>
<div class="bar">
  <button class="btn p" id="add">📷 Seite</button>
  <button class="btn" id="pick">🖼 Galerie</button>
  <button class="btn p" id="fin" disabled>✅ PDF</button>
</div>

<div id="ed">
  <div class="top"><span id="edinfo">Ecken mit dem Finger ziehen</span></div>
  <div id="edwrap"><img id="edimg" alt=""><svg id="edsvg"></svg></div>
  <div class="bot">
    <button class="btn" id="edall">Ganzes Bild</button>
    <button class="btn" id="edcancel">Abbrechen</button>
    <button class="btn p" id="edok">Übernehmen</button>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let sid = sessionStorage.getItem('s2p_sid'), pages = [];
const KEYS = ['label', 'modus', 'ordner'];
KEYS.forEach(k => { const v = localStorage.getItem('s2p_' + k); if (v !== null && k !== 'label') $(k).value = v; });
KEYS.filter(k => k !== 'label').forEach(k => $(k).onchange = () => localStorage.setItem('s2p_' + k, $(k).value));
fetch('/api/folders').then(r => r.json()).then(l => { l.forEach(n => { const o = document.createElement('option'); o.value = o.textContent = n; $('ordner').appendChild(o); });
  const v = localStorage.getItem('s2p_ordner'); if (v) $('ordner').value = v; });

function msg(h, cls) { $('status').className = 'status ' + (cls || ''); $('status').innerHTML = h || ''; }
async function ensure() {
  if (sid) { const r = await fetch('/api/scan/' + sid); if (r.ok) { pages = (await r.json()).pages; render(); return; } }
  const r = await fetch('/api/scan/new', { method: 'POST' }); sid = (await r.json()).sid; sessionStorage.setItem('s2p_sid', sid); pages = []; render();
}
function render() {
  const box = $('pages'); box.innerHTML = '';
  pages.forEach((p, i) => {
    const d = document.createElement('div'); d.className = 'pg';
    d.innerHTML = `<img src="${p.thumb}" alt=""><span class="n">${i + 1}</span>
      <div class="badge ${p.auto ? '' : 'warn'}">${p.auto ? 'Rand automatisch' : 'Rand manuell / prüfen'}</div>
      <div class="acts"><button data-a="edit" data-i="${i}">✂️</button><button data-a="up" data-i="${i}">◀</button>
      <button data-a="down" data-i="${i}">▶</button><button data-a="del" data-i="${i}">🗑</button></div>`;
    box.appendChild(d);
  });
  $('fin').disabled = pages.length === 0; $('fin').textContent = pages.length ? `✅ PDF (${pages.length})` : '✅ PDF';
}
$('pages').onclick = async (e) => {
  const b = e.target.closest('button[data-a]'); if (!b) return;
  const p = pages[+b.dataset.i], a = b.dataset.a;
  if (a === 'edit') return openEditor(p);
  const url = a === 'del' ? `/api/scan/${sid}/delete/${p.id}` : `/api/scan/${sid}/move/${p.id}`;
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dir: a }) });
  if (r.ok) { pages = (await r.json()).pages; render(); }
};
async function upload(files) {
  await ensure();
  for (const f of files) {
    msg(`<span class="spinner"></span>Seite ${pages.length + 1} wird erkannt …`);
    const fd = new FormData(); fd.append('file', f);
    try {
      const r = await fetch(`/api/scan/${sid}/add`, { method: 'POST', body: fd }); const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      pages.push(j.page); render();
      msg(j.page.auto ? '✅ Rand erkannt — bei Bedarf ✂️ antippen.' : '⚠️ Rand nicht sicher erkannt — bitte mit ✂️ prüfen.', j.page.auto ? 'ok' : '');
    } catch (err) { msg('❌ ' + err.message, 'err'); }
  }
}
$('add').onclick = () => $('cam').click(); $('pick').onclick = () => $('gal').click();
$('cam').onchange = (e) => { const f = [...e.target.files]; e.target.value = ''; upload(f); };
$('gal').onchange = (e) => { const f = [...e.target.files]; e.target.value = ''; upload(f); };

$('fin').onclick = async () => {
  if (!pages.length) return; localStorage.setItem('s2p_label', $('label').value);
  $('fin').disabled = true; $('add').disabled = true; msg(`<span class="spinner"></span>PDF wird erstellt (OCR, ca. ${pages.length * 12} s) …`);
  const fd = new FormData(); fd.append('label', $('label').value); fd.append('ocr', '1'); fd.append('paperless', $('pl').checked ? '1' : '0');
  fd.append('modus', $('modus').value); fd.append('ordner', $('ordner').value);
  try {
    const r = await fetch(`/api/scan/${sid}/finish`, { method: 'POST', body: fd }); const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    sessionStorage.removeItem('s2p_sid'); sid = null; pages = []; render(); $('label').value = '';
    msg(`✅ PDF fertig — ${j.pages} Seite(n), ${j.size_kb} KB` + (j.paperless ? `<br>📥 Paperless: ${j.paperless}` : '') +
      (j.paperless_error ? `<br><span class="err">Paperless-Fehler: ${j.paperless_error}</span>` : '') +
      `<br><a href="/download/${encodeURIComponent(j.file)}">⬇︎ PDF herunterladen</a>`, 'ok');
  } catch (err) { msg('❌ ' + err.message, 'err'); } finally { $('add').disabled = false; $('fin').disabled = pages.length === 0; }
};

/* ---------- Rand-Editor ---------- */
let cur = null, q = [], dragging = -1, W = 0, H = 0, sc = 1, ox = 0, oy = 0;
const img = $('edimg'), svg = $('edsvg'), wrap = $('edwrap');
function layout() {
  const bw = wrap.clientWidth, bh = wrap.clientHeight; sc = Math.min(bw / W, bh / H);
  const w = W * sc, h = H * sc; ox = (bw - w) / 2; oy = (bh - h) / 2;
  img.style.cssText = `left:${ox}px;top:${oy}px;width:${w}px;height:${h}px`;
  svg.setAttribute('width', bw); svg.setAttribute('height', bh); draw();
}
function px(i) { return [ox + q[i][0] * W * sc, oy + q[i][1] * H * sc]; }
function draw() {
  const P = q.map((_, i) => px(i)); let h = `<polygon points="${P.map(p => p.join(',')).join(' ')}" fill="#4f8cff22" stroke="#4f8cff" stroke-width="2"/>`;
  P.forEach((p, i) => { h += `<circle cx="${p[0]}" cy="${p[1]}" r="26" fill="#4f8cff33" stroke="#fff" stroke-width="2"/><circle cx="${p[0]}" cy="${p[1]}" r="5" fill="#fff"/>`; });
  svg.innerHTML = h;
}
function openEditor(p) {
  cur = p; q = p.quad.map(c => [...c]); $('ed').style.display = 'flex';
  img.onload = () => { W = img.naturalWidth; H = img.naturalHeight; layout(); };
  img.src = p.src + '?t=' + Date.now(); if (img.complete && img.naturalWidth) { W = img.naturalWidth; H = img.naturalHeight; layout(); }
}
function evpt(e) { const r = wrap.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
wrap.addEventListener('pointerdown', (e) => {
  const [x, y] = evpt(e); let best = -1, bd = 1e9;
  q.forEach((_, i) => { const [px_, py_] = px(i), d = Math.hypot(px_ - x, py_ - y); if (d < bd) { bd = d; best = i; } });
  if (bd < 70) { dragging = best; wrap.setPointerCapture(e.pointerId); e.preventDefault(); }
});
wrap.addEventListener('pointermove', (e) => {
  if (dragging < 0) return; const [x, y] = evpt(e);
  q[dragging] = [Math.min(1, Math.max(0, (x - ox) / (W * sc))), Math.min(1, Math.max(0, (y - oy) / (H * sc)))]; draw();
});
['pointerup', 'pointercancel'].forEach(t => wrap.addEventListener(t, () => { dragging = -1; }));
window.addEventListener('resize', () => { if ($('ed').style.display === 'flex') layout(); });
$('edall').onclick = () => { q = [[0.01, 0.01], [0.99, 0.01], [0.99, 0.99], [0.01, 0.99]]; draw(); };
$('edcancel').onclick = () => { $('ed').style.display = 'none'; };
$('edok').onclick = async () => {
  $('edok').disabled = true; $('edinfo').innerHTML = '<span class="spinner"></span>wird neu berechnet …';
  try {
    const r = await fetch(`/api/scan/${sid}/quad/${cur.id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ quad: q }) });
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status);
    pages = pages.map(p => p.id === cur.id ? j.page : p); render(); $('ed').style.display = 'none'; msg('✅ Rand übernommen.', 'ok');
  } catch (err) { $('edinfo').textContent = '❌ ' + err.message; } finally { $('edok').disabled = false; }
};
ensure();
</script></body></html>"""


@app.route("/scan")
def scan_page():
    return render_template_string(SCAN_PAGE)


@app.route("/api/scan/new", methods=["POST"])
def api_scan_new():
    _scan_cleanup()
    sid = uuid.uuid4().hex[:12]
    d = _scan_dir(sid, create=True)
    _scan_save(d, {"pages": []})
    return jsonify(sid=sid)


@app.route("/api/scan/<sid>")
def api_scan_get(sid):
    d = _scan_dir(sid)
    if not d:
        return jsonify(error="Sitzung unbekannt"), 404
    return jsonify(pages=[_page_public(sid, pg) for pg in _scan_meta(d)["pages"]])


@app.route("/api/scan/<sid>/add", methods=["POST"])
def api_scan_add(sid):
    d = _scan_dir(sid)
    if not d:
        return jsonify(error="Sitzung unbekannt"), 404
    f = request.files.get("file")
    if not f:
        return jsonify(error="kein Bild"), 400
    try:
        frame = pipeline.prepare_frame(Image.open(io.BytesIO(f.read())))
    except Exception as exc:
        return jsonify(error=f"Bild nicht lesbar: {exc}"), 400
    meta = _scan_meta(d)
    pid = uuid.uuid4().hex[:8]
    frame.save(d / f"{pid}.src.jpg", quality=90)
    quad = pipeline.detect_quad_norm(frame)
    auto = quad is not None
    if quad is None:                      # nichts Verlaessliches erkannt: ganzes Bild, von Hand anpassbar
        quad = [[0.02, 0.02], [0.98, 0.02], [0.98, 0.98], [0.02, 0.98]]
    pg = {"id": pid, "quad": quad, "auto": auto}
    try:
        _render_page(d, pg)
    except Exception as exc:
        (d / f"{pid}.src.jpg").unlink(missing_ok=True)
        return jsonify(error=str(exc)), 500
    meta["pages"].append(pg)
    _scan_save(d, meta)
    return jsonify(page=_page_public(sid, pg), count=len(meta["pages"]))


@app.route("/api/scan/<sid>/quad/<pid>", methods=["POST"])
def api_scan_quad(sid, pid):
    d = _scan_dir(sid)
    if not d:
        return jsonify(error="Sitzung unbekannt"), 404
    meta = _scan_meta(d)
    pg = next((p for p in meta["pages"] if p["id"] == pid), None)
    if not pg:
        return jsonify(error="Seite unbekannt"), 404
    quad = (request.get_json(silent=True) or {}).get("quad")
    try:
        old = pg["quad"]
        pg["quad"] = [[float(x), float(y)] for x, y in quad]
        pg["auto"] = False
        _render_page(d, pg)
    except Exception as exc:
        pg["quad"] = old
        return jsonify(error=str(exc)), 400
    _scan_save(d, meta)
    return jsonify(page=_page_public(sid, pg))


@app.route("/api/scan/<sid>/delete/<pid>", methods=["POST"])
def api_scan_delete(sid, pid):
    d = _scan_dir(sid)
    if not d or not re.match(r"^[0-9a-f]{8}$", pid):
        return jsonify(error="unbekannt"), 404
    meta = _scan_meta(d)
    meta["pages"] = [p for p in meta["pages"] if p["id"] != pid]
    for ext in ("src.jpg", "page.png", "page.jpg"):
        (d / f"{pid}.{ext}").unlink(missing_ok=True)
    _scan_save(d, meta)
    return jsonify(pages=[_page_public(sid, pg) for pg in meta["pages"]])


@app.route("/api/scan/<sid>/move/<pid>", methods=["POST"])
def api_scan_move(sid, pid):
    d = _scan_dir(sid)
    if not d:
        return jsonify(error="Sitzung unbekannt"), 404
    meta = _scan_meta(d)
    pages = meta["pages"]
    i = next((k for k, p in enumerate(pages) if p["id"] == pid), None)
    if i is None:
        return jsonify(error="Seite unbekannt"), 404
    j = i + (1 if (request.get_json(silent=True) or {}).get("dir") == "down" else -1)
    if 0 <= j < len(pages):
        pages[i], pages[j] = pages[j], pages[i]
        _scan_save(d, meta)
    return jsonify(pages=[_page_public(sid, pg) for pg in pages])


@app.route("/api/scan/<sid>/img/<name>")
def api_scan_img(sid, name):
    d = _scan_dir(sid)
    if not d or not re.match(r"^[0-9a-f]{8}\.(src|page)\.(jpg|png)$", name):
        return jsonify(error="unbekannt"), 404
    resp = send_from_directory(d, name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/scan/<sid>/finish", methods=["POST"])
def api_scan_finish(sid):
    d = _scan_dir(sid)
    if not d:
        return jsonify(error="Sitzung unbekannt"), 404
    meta = _scan_meta(d)
    if not meta["pages"]:
        return jsonify(error="keine Seiten"), 400
    try:
        pages = [Image.open(d / f"{pg['id']}.page.png").convert("RGB") for pg in meta["pages"]]
        result = pipeline.process_pages(pages, WORK, OUT, do_ocr=request.form.get("ocr", "1") == "1")
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    result["created"] = int(os.path.getmtime(OUT / result["file"]))
    result["label"] = (request.form.get("label") or "").strip()
    if request.form.get("paperless") == "1":
        try:
            dst = _to_paperless(OUT / result["file"], result["label"],
                                request.form.get("modus", "archivieren"), request.form.get("ordner", ""))
            result["paperless"] = f"{dst.parent.relative_to(PAPERLESS_CONSUME)}/{dst.name}"
        except Exception as exc:
            result["paperless_error"] = str(exc)
    _add_meta(result)
    shutil.rmtree(d, ignore_errors=True)
    return jsonify(result)


@app.route("/api/scan/<sid>/discard", methods=["POST"])
def api_scan_discard(sid):
    d = _scan_dir(sid)
    if d:
        shutil.rmtree(d, ignore_errors=True)
    return jsonify(ok=True)


@app.route("/health")
def health():
    return jsonify(ok=True, ts=int(time.time()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5120, debug=False)
