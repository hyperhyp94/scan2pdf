"""Bild -> A4-zugeschnitten -> OCR -> PDF Pipeline."""
import io
import os
import subprocess
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

DPI = 300
A4_PORTRAIT_MM = (210, 297)
A4_LANDSCAPE_MM = (297, 210)
MARGIN_MM = 8          # weisser Rand um das Dokument auf der A4-Seite
OCR_CONTAINER = "scan2pdf-ocr"
OCR_LANG = "deu+eng"


def _mm2px(mm, dpi=DPI):
    return int(round(mm / 25.4 * dpi))


def _otsu(gray_u8):
    """Otsu-Schwelle auf einem uint8-Graustufenbild."""
    hist = np.bincount(gray_u8.ravel(), minlength=256).astype(float)
    total = gray_u8.size
    sum_all = np.dot(np.arange(256), hist)
    sum_b = 0.0
    w_b = 0.0
    best = 0
    best_var = -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var = var
            best = t
    return best


def _detect_page(img):
    """Findet das Papier im Foto (helles Rechteck auf dunklerem Untergrund)."""
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    h, w = gray.shape
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    # Untergrund schon hell -> Papier fuellt das Bild, kein Page-Crop noetig
    if np.percentile(border, 85) > 215:
        return None
    thresh = max(140, _otsu(gray))
    mask = gray >= thresh
    rows = mask.mean(axis=1)
    cols = mask.mean(axis=0)
    r_idx = np.where(rows > 0.12)[0]
    c_idx = np.where(cols > 0.12)[0]
    if len(r_idx) == 0 or len(c_idx) == 0:
        return None
    top, bottom = int(r_idx[0]), int(r_idx[-1])
    left, right = int(c_idx[0]), int(c_idx[-1])
    # Plausibilitaet: Papier muss mind. 15% der Flaeche sein
    if (bottom - top) * (right - left) < 0.15 * h * w:
        return None
    return left, top, right + 1, bottom + 1


def _content_bbox(img, thresh):
    """Bounding-Box des Inhalts (alles was deutlich dunkler als Papier ist)."""
    rgb = img.convert("RGB")
    arr = np.asarray(rgb, dtype=np.int16)
    gray = arr.mean(axis=2)
    mask = gray < thresh
    h, w = gray.shape
    rows = mask.sum(axis=1)
    cols = mask.sum(axis=0)
    r_idx = np.where(rows > max(2, w * 0.002))[0]
    c_idx = np.where(cols > max(2, h * 0.002))[0]
    if len(r_idx) == 0 or len(c_idx) == 0:
        return None
    top, bottom = int(r_idx[0]), int(r_idx[-1])
    left, right = int(c_idx[0]), int(c_idx[-1])
    # Plausibilitaet: ein Crop darf nicht fast alles wegwerfen
    if (bottom - top) < 0.20 * h or (right - left) < 0.20 * w:
        return None
    return left, top, right + 1, bottom + 1


def _deskew(img):
    """Schieflage korrigieren (Projektionsprofil-Maximum, +/-4 Grad)."""
    small = img.convert("L")
    scale = 900 / max(small.size)
    if scale < 1:
        small = small.resize((max(1, int(small.width * scale)),
                              max(1, int(small.height * scale))), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)
    thresh = min(215.0, max(60.0, float(np.percentile(arr, 85)) - 45))
    mask = (arr < thresh).astype(np.float32)
    if mask.sum() < 50:
        return img
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-4.0, 4.01, 0.25):
        rot = Image.fromarray((mask * 255).astype(np.uint8)).rotate(
            angle, resample=Image.BILINEAR, expand=False, fillcolor=0)
        profile = np.asarray(rot, dtype=np.float32).sum(axis=1)
        score = float(np.var(np.diff(profile)))
        if score > best_score:
            best_score, best_angle = score, float(angle)
    if abs(best_angle) < 0.25:
        return img
    return img.rotate(-best_angle, resample=Image.BICUBIC, expand=True,
                      fillcolor="white")


def crop_white_borders(img):
    """Papier finden, grauen Untergrund und weisse Raender wegschneiden."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > 4000:  # grosse Handyfotos etwas verkleinern (Speed)
        img.thumbnail((4000, 4000), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)

    page = _detect_page(img)
    work = img.crop(page) if page else img
    work = _trim(work)
    tilted = _deskew(work)
    if tilted.size != work.size:
        work = _trim(tilted)
    else:
        work = tilted
    return work


def _trim(work):
    """Weisse Raender um den Inhalt wegschneiden."""
    gray = np.asarray(work.convert("L"), dtype=np.float32)
    paper = float(np.percentile(gray, 85))
    thresh = min(215.0, max(60.0, paper - 45))

    bbox = None
    for t in (thresh, thresh * 0.75, thresh * 0.5):
        bbox = _content_bbox(work, t)
        if bbox:
            break
    if not bbox:
        return work

    pad_x = int((bbox[2] - bbox[0]) * 0.015)
    pad_y = int((bbox[3] - bbox[1]) * 0.015)
    left = max(0, bbox[0] - pad_x)
    top = max(0, bbox[1] - pad_y)
    right = min(work.width, bbox[2] + pad_x)
    bottom = min(work.height, bbox[3] + pad_y)
    return work.crop((left, top, right, bottom))


def to_a4(img, margin_mm=MARGIN_MM, dpi=DPI):
    """Setzt das Bild mittig auf eine weisse A4-Seite (Querformat bei Querformat-Bild)."""
    landscape = img.width > img.height * 1.05
    page_mm = A4_LANDSCAPE_MM if landscape else A4_PORTRAIT_MM
    page_w, page_h = _mm2px(page_mm[0], dpi), _mm2px(page_mm[1], dpi)
    m = _mm2px(margin_mm, dpi)
    box_w, box_h = page_w - 2 * m, page_h - 2 * m

    scale = min(box_w / img.width, box_h / img.height)
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    page = Image.new("RGB", (page_w, page_h), "white")
    page.paste(img, ((page_w - new_w) // 2, (page_h - new_h) // 2))
    return page


def images_to_pdf(images, out_path, dpi=DPI):
    """Mehrere A4-Seiten in ein PDF schreiben (JPEG, Qualitaet 90)."""
    pages = [to_a4(crop_white_borders(im), dpi=dpi) for im in images]
    if not pages:
        raise ValueError("keine Bilder")
    w_pt = pages[0].width / dpi * 72
    h_pt = pages[0].height / dpi * 72
    c = canvas.Canvas(str(out_path), pagesize=(w_pt, h_pt))
    for p in pages:
        c.setPageSize((p.width / dpi * 72, p.height / dpi * 72))
        buf = io.BytesIO()
        p.save(buf, format="JPEG", quality=90, optimize=True)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=p.width / dpi * 72,
                    height=p.height / dpi * 72)
        c.showPage()
    c.save()
    return len(pages)


def ocr_pdf(in_path, out_path, container=OCR_CONTAINER, lang=OCR_LANG):
    """Durchsuchbares PDF via ocrmypdf im OCR-Container."""
    job = in_path.parent.name
    rel_in = f"/work/{job}/{in_path.name}"
    rel_out = f"/work/{job}/{out_path.name}"
    cmd = ["docker", "exec", container, "ocrmypdf", "-l", lang,
           "--deskew", "--optimize", "1", "--output-type", "pdf",
           rel_in, rel_out]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0 or not out_path.exists():
        return False, (res.stderr or res.stdout or "ocr failed")[-1500:]
    return True, "ok"


def process_job(images, work_root, out_dir, do_ocr=True):
    """Kompletter Durchlauf. Gibt dict mit Ergebnis-Infos zurueck."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = Path(work_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    raw = job_dir / "raw.pdf"
    pages = images_to_pdf(images, raw)

    ocr_ok, ocr_msg = (False, "deaktiviert")
    text = ""
    if do_ocr:
        ocr_out = job_dir / "ocr.pdf"
        ocr_ok, ocr_msg = ocr_pdf(raw, ocr_out)
        if ocr_ok:
            text = pdf_text(ocr_out)
            raw.unlink(missing_ok=True)
            src = ocr_out
        else:
            src = raw
    else:
        src = raw

    final = Path(out_dir) / f"scan-{job_id}.pdf"
    os.replace(src, final)
    for leftover in job_dir.iterdir():
        leftover.unlink(missing_ok=True)
    try:
        job_dir.rmdir()
    except OSError:
        pass

    return {
        "id": job_id,
        "file": final.name,
        "pages": pages,
        "size_kb": round(final.stat().st_size / 1024),
        "ocr": ocr_ok,
        "ocr_msg": ocr_msg,
        "text": text[:1200],
    }


def pdf_text(path, max_chars=1500):
    """Text aus dem OCR-PDF lesen (PyMuPDF falls vorhanden, sonst pdftotext im Container)."""
    try:
        import pymupdf  # type: ignore
        doc = pymupdf.open(str(path))
        txt = "\n".join(p.get_text() for p in doc)
        doc.close()
        return txt.strip()[:max_chars]
    except Exception:
        pass
    return ""
