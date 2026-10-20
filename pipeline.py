"""Bild -> A4-zugeschnitten -> OCR -> PDF Pipeline.

Papiererkennung: Papier ist hell UND farbneutral, Holz/Tisch ist gesaettigt.
Groesste papierfarbene Flaeche = Blatt -> freistellen, begradigen (Projektions-
profil), Rest-Untergrund weiss uebermalen, dann auf A4 setzen und OCR.
"""
import io
import os
import subprocess
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

try:
    import cv2
except ImportError:  # pragma: no cover - Fallback ohne OpenCV
    cv2 = None

DPI = 300
A4_PORTRAIT_MM = (210, 297)
A4_LANDSCAPE_MM = (297, 210)
MARGIN_MM = 3          # weisser Rand um das Dokument auf der A4-Seite
JPEG_QUALITY = 80
OCR_CONTAINER = "scan2pdf-ocr"
OCR_LANG = "deu+eng"


def _mm2px(mm, dpi=DPI):
    return int(round(mm / 25.4 * dpi))


def _otsu(gray_u8):
    """Otsu-Schwelle auf einem uint8-Graustufenbild (nur Fallback ohne cv2)."""
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


def _order_quad(pts):
    """4 Punkte in Reihenfolge [oben-links, oben-rechts, unten-rechts, unten-links]."""
    pts = np.array(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _paper_mask(bgr):
    """Binaermaske des Papiers: hell UND farbneutral (Holz ist braun = gesaettigt)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    v = cv2.GaussianBlur(hsv[:, :, 2], (5, 5), 0)
    s = cv2.GaussianBlur(hsv[:, :, 1], (5, 5), 0)
    mask = ((v > 135) & (s < 60)).astype(np.uint8) * 255
    # Text loechert das Papier -> schliessen, dann Stoerungen entfernen
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((11, 11), np.uint8))
    return mask


def _largest_contour(mask):
    cnts = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
    if not cnts:
        return None
    return max(cnts, key=cv2.contourArea)


def _is_scan(img_bgr):
    """Foto oder schon ein Scan? (Scan = Rand hell UND farbneutral)"""
    small = cv2.resize(img_bgr, (320, 320))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    d = max(2, int(min(h, w) * 0.05))
    edges = np.concatenate([hsv[:d].reshape(-1, 3), hsv[-d:].reshape(-1, 3),
                            hsv[:, :d].reshape(-1, 3), hsv[:, -d:].reshape(-1, 3)])
    return float(np.median(edges[:, 2])) > 200 and float(np.median(edges[:, 1])) < 35


def border_quality(img, strip=0.03):
    """Wie sauber ist der Blattrand? Median-Helligkeit + Anteil holzartiger Pixel."""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    h, w = arr.shape[:2]
    d = max(2, int(min(h, w) * strip))
    edges = np.concatenate([arr[:d].reshape(-1, 3), arr[-d:].reshape(-1, 3),
                            arr[:, :d].reshape(-1, 3), arr[:, -d:].reshape(-1, 3)])
    hsv = cv2.cvtColor(edges.reshape(-1, 1, 3), cv2.COLOR_RGB2HSV).reshape(-1, 3)
    bright = float(np.median(hsv[:, 2]))
    wood = float(((hsv[:, 1] > 85) & (hsv[:, 2] < 215)).mean())
    return bright, wood


def _whiteout_background(img):
    """Holzartige (gesaettigte, mittelhelle) Pixel weiss machen — Rest-Untergrund killen."""
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    s = cv2.GaussianBlur(hsv[:, :, 1], (5, 5), 0)
    v = cv2.GaussianBlur(hsv[:, :, 2], (5, 5), 0)
    wood = ((s > 85) & (v < 215)).astype(np.uint8)
    wood = cv2.dilate(wood, np.ones((7, 7), np.uint8))
    if wood.mean() > 0.75:            # fast alles "Holz" -> Fehlalarm, nicht anfassen
        return img
    out = bgr.copy()
    out[wood > 0] = (255, 255, 255)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


def _whiteout_border(img, frac=0.035):
    """Whiteout NUR im aeusseren Randstreifen (Holzreste), Innenflaeche/Text bleibt unberuehrt."""
    out = _whiteout_background(img)
    if out is img:
        return img
    a, b = np.asarray(img.convert("RGB")).copy(), np.asarray(out.convert("RGB"))
    h, w = a.shape[:2]
    d = max(4, int(min(h, w) * frac))
    a[:d], a[-d:], a[:, :d], a[:, -d:] = b[:d], b[-d:], b[:, :d], b[:, -d:]
    return Image.fromarray(a)


def _mask_polygon(img, bgr=None):
    """Papierflaeche als gefuellte Maske (Blattform, kein Viereck-Zwang)."""
    if bgr is None:
        bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    mask = _paper_mask(bgr)
    cnt = _largest_contour(mask)
    if cnt is None or cv2.contourArea(cnt) < 0.12 * h * w:
        return None
    poly = np.zeros((h, w), np.uint8)
    cv2.fillPoly(poly, [cnt], 255)
    # leicht nach innen: Blattrand anschneiden statt Untergrund mitnehmen
    d = max(3, int(min(h, w) * 0.004))
    poly = cv2.erode(poly, np.ones((d * 2 + 1, d * 2 + 1), np.uint8))
    if poly.sum() == 0:
        return None
    return poly


def _deskew(img, max_angle=4.0, step=0.25, expand=True):
    """Schieflage korrigieren: Projektionsprofil-Maximum, grob -> fein.

    Vorzeichen-unabhaengig (es wird nur die Ausrichtung bewertet), daher auch
    fuer groessere Winkel geeignet.
    """
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
    src = Image.fromarray((mask * 255).astype(np.uint8))

    def score(angle):
        rot = np.asarray(src.rotate(angle, resample=Image.BILINEAR, fillcolor=0),
                         dtype=np.float32).sum(axis=1)
        return float(np.var(np.diff(rot)))

    coarse = np.arange(-max_angle, max_angle + 1e-6, max(2.0, step * 8))
    best = max(coarse, key=score)
    fine = np.arange(max(-max_angle, best - 2.0),
                     min(max_angle, best + 2.0) + 1e-6, step)
    best = max(fine, key=score)
    if abs(best) < 0.25:
        return img
    # Vorzeichen: der Score-Peak liegt bei dem Winkel, um den gedreht werden muss
    return img.rotate(float(best), resample=Image.BICUBIC, expand=expand,
                      fillcolor="white")


def _straighten(img):
    """Auch groessere Schieflagen (bis ~14 Grad) begradigen."""
    return _deskew(img, max_angle=14.0, step=0.25, expand=False)


def _page_occupancy(img, bgr=None):
    """Anteil der Bildflaeche, den das erkannte Blatt einnimmt."""
    if bgr is None:
        bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    mask = _paper_mask(bgr)
    cnt = _largest_contour(mask)
    if cnt is None:
        return 0.0, 1.0
    x, y, cw, ch = cv2.boundingRect(cnt)
    ratio = (cw / ch) if ch else 1.0
    return cv2.contourArea(cnt) / (h * w), ratio


def _find_quad(bgr, min_area=0.30):
    """Blatt als konvexes Viereck (nur wenn das Blatt komplett im Bild ist)."""
    if cv2 is None:
        return None
    h, w = bgr.shape[:2]
    scale = min(1.0, 1400.0 / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                       interpolation=cv2.INTER_AREA) if scale < 1 else bgr
    mask = _paper_mask(small)
    cnt = _largest_contour(mask)
    if cnt is None or cv2.contourArea(cnt) < min_area * small.shape[0] * small.shape[1]:
        return None
    peri = cv2.arcLength(cnt, True)
    corners = None
    for eps in (0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(cnt, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            corners = approx.reshape(4, 2).astype(np.float32)
            break
    if corners is None:
        return None
    corners = np.array(_order_quad(corners / scale), dtype=np.float32)
    center = corners.mean(axis=0)
    corners = center + (corners - center) * 0.996   # 0.4 % nach innen
    return np.clip(corners, [0, 0], [w - 1, h - 1]).astype(np.float32)


def _edge_contrast(small, quad, band=0.012):
    """Pro Viereckskante: Median(innen heller + farbneutraler als aussen).

    Echte Blattkanten -> klar positiv (Papier heller als Untergrund). Vierecke am
    Bildrand oder quer ueber Holzmaserung -> ~0 oder negativ. -1 = Kante liegt am Bildrand.
    """
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.float32)
    h, w = small.shape[:2]
    d = max(4, int(min(h, w) * band))
    q = _order_quad(quad)
    c = q.mean(axis=0)
    vals = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        n = max(10, int(np.linalg.norm(b - a) / 6))
        t = np.linspace(0.1, 0.9, n)
        pts = a[None, :] * (1 - t[:, None]) + b[None, :] * t[:, None]
        vec = pts - c
        vec /= (np.linalg.norm(vec, axis=1, keepdims=True) + 1e-6)
        pin = (pts - vec * d).astype(int)
        pout = (pts + vec * d).astype(int)
        ok = (pout[:, 0] >= 0) & (pout[:, 0] < w) & (pout[:, 1] >= 0) & (pout[:, 1] < h)
        if ok.sum() < n * 0.7:
            vals.append(-1.0)
            continue
        pin, pout = pin[ok], pout[ok]
        vin, vout = hsv[pin[:, 1], pin[:, 0]], hsv[pout[:, 1], pout[:, 0]]
        vals.append(float(np.median((vin[:, 2] - vout[:, 2]) + 0.6 * (vout[:, 1] - vin[:, 1]))))
    return vals


def _find_quad_edges(bgr, min_area=0.15, max_area=0.98, min_contrast=30.0):
    """Blatt per KANTEN finden (Canny -> Vierecke -> Kantenkontrast pruefen).

    Unabhaengig von der Untergrundfarbe (auch helles Holz/Parkett). Gewinner = groesstes
    Viereck, dessen vier Kanten alle klar Papier-gegen-Untergrund sind.
    """
    h, w = bgr.shape[:2]
    scale = min(1.0, 1000.0 / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                       interpolation=cv2.INTER_AREA) if scale < 1 else bgr
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    best, best_score = None, 0.0
    for blur in (5, 9):
        g = cv2.GaussianBlur(gray, (blur, blur), 0)
        for lo, hi in ((20, 60), (40, 120), (60, 180)):
            e = cv2.Canny(g, lo, hi)
            e = cv2.dilate(e, np.ones((3, 3), np.uint8), iterations=2)
            e = cv2.morphologyEx(e, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
            for c in cv2.findContours(e, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[-2]:
                if not (min_area <= cv2.contourArea(c) / (sh * sw) <= max_area):
                    continue
                hull = cv2.convexHull(c)
                peri = cv2.arcLength(hull, True)
                for eps in (0.02, 0.03, 0.04, 0.06):
                    ap = cv2.approxPolyDP(hull, eps * peri, True)
                    if len(ap) != 4:
                        continue
                    quad = ap.reshape(4, 2).astype(np.float32)
                    ec = _edge_contrast(small, quad)
                    if min(ec) < min_contrast:
                        break
                    area = cv2.contourArea(quad) / (sh * sw)
                    score = area * min(1.0, min(ec) / 80.0)
                    if score > best_score:
                        best, best_score = quad, score
                    break
    if best is None:
        return None
    corners = np.array(_order_quad(best / scale), dtype=np.float32)
    center = corners.mean(axis=0)
    corners = center + (corners - center) * 0.996
    return np.clip(corners, [0, 0], [w - 1, h - 1]).astype(np.float32)


def _warp_quad(bgr, quad):
    """Perspektivisch entzerren: Viereck -> Rechteck."""
    tl, tr, br, bl = quad
    out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if out_w < 200 or out_h < 200:
        return None
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1],
                    [0, out_h - 1]], dtype=np.float32)
    mat = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    warped = cv2.warpPerspective(bgr, mat, (out_w, out_h), flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_CONSTANT,
                                 borderValue=(255, 255, 255))
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))


def _extract_page(img):
    """Papier freistellen, begradigen, Untergrund entfernen."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > 4000:            # grosse Handyfotos verkleinern (Speed)
        img.thumbnail((4000, 4000), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1, preserve_tone=False)

    if cv2 is None:
        return _deskew(_trim_bright(img))

    bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    if _is_scan(bgr):                   # schon sauber gescannt -> nichts freistellen
        return img

    occ, ratio = _page_occupancy(img, bgr)
    # 1) Bestfall: Blatt komplett sichtbar -> per KANTEN finden (farbunabhaengig,
    #    klappt auch auf hellem Holz), Fallback: Farbmaske (hell + farbneutral)
    work = None
    quad = _find_quad_edges(bgr)
    if quad is None:
        quad = _find_quad(bgr)
    else:
        # Belegung/Seitenverhaeltnis aus dem Kanten-Viereck statt aus der Farbmaske
        qw = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
        qh = (np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2
        occ = cv2.contourArea(quad) / float(bgr.shape[0] * bgr.shape[1])
        ratio = (qw / qh) if qh else 1.0
    # Blatt fast komplett im Bild -> ganze Seite behalten (Original-Layout!)
    full_page = occ >= 0.45 and (0.55 <= ratio <= 0.88 or 1.13 <= ratio <= 1.60)
    if quad is not None:
        work = _warp_quad(bgr, quad)

    # 2) sonst: Blattform maskieren, Untergrund weissmalen
    if work is None:
        poly = _mask_polygon(img, bgr)
        if poly is not None:
            x, y, pw, ph = cv2.boundingRect(poly)
            pad = max(2, int(min(bgr.shape[:2]) * 0.002))
            box = (max(0, x - pad), max(0, y - pad),
                   min(bgr.shape[1], x + pw + pad), min(bgr.shape[0], y + ph + pad))
            if (box[2] - box[0]) >= 0.35 * bgr.shape[1] and \
               (box[3] - box[1]) >= 0.35 * bgr.shape[0]:
                keep = poly[box[1]:box[3], box[0]:box[2]]
                cut = bgr[box[1]:box[3], box[0]:box[2]].copy()
                cut[keep == 0] = (255, 255, 255)
                work = Image.fromarray(cv2.cvtColor(cut, cv2.COLOR_BGR2RGB))
    if work is None:
        work = img

    work = _straighten(work)
    # Rest-Untergrund (Holz): bei sauberem Viereck-Zuschnitt nur im Randstreifen weissmalen,
    # sonst frisst der Schritt dunklen/farbigen Text und Unterschriften.
    cut_clean = quad is not None
    work = _whiteout_border(work) if cut_clean else _whiteout_background(work)
    work = _flatten_illumination(work)  # Schatten weg -> weisses Papier, harter Text

    if not full_page:                   # kleines Blatt -> auf den Inhalt zuschneiden
        fixed = _trim_bright(work)
        if fixed.size != work.size:
            work = fixed
    else:
        # nur die beim Drehen entstandenen weissen Ecken abschneiden
        work = _trim_white_edges(work)
    work = _whiteout_border(work) if cut_clean else _whiteout_background(work)
    return work


def _flatten_illumination(img):
    """Ausleuchtung normalisieren: Schatten/Flecken -> gleichmaessig weisses Papier.

    Papier wird durch sein eigenes, stark weichgezeichnetes Hintergrundbild
    geteilt. Dadurch wird die Seite ueberall gleich hell und der Text bekommt
    deutlich mehr Kontrast (entscheidend fuer OCR).
    """
    if cv2 is None:
        return img
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    h, w = arr.shape[:2]
    sigma = max(15, int(min(h, w) * 0.03))
    bg = cv2.GaussianBlur(arr, (0, 0), sigmaX=sigma, sigmaY=sigma)
    bg = np.maximum(bg, 1).astype(np.float32)
    norm = np.clip(arr.astype(np.float32) * (250.0 / bg), 0, 255).astype(np.uint8)
    return Image.fromarray(norm)


def _trim_white_edges(img, thresh=254):
    """Nur reinweisse Ränder (Dreh-Ecken) abschneiden, Inhalt bleibt unangetastet."""
    arr = np.asarray(img.convert("L"), dtype=np.uint8)
    mask = arr < thresh
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return img
    return img.crop((int(cols[0]), int(rows[0]),
                     int(cols[-1]) + 1, int(rows[-1]) + 1))


def _brightness_mask(img):
    """Maske der dunklen Bildinhalte (Text/Stempel) auf dem Papier."""
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    paper = float(np.percentile(gray, 80))
    thresh = min(215.0, max(60.0, paper - 45))
    mask = gray < thresh
    return mask, gray


def _content_bbox(img):
    """Bounding-Box des Inhalts (dunkle Pixel) relativ zur Blatthelligkeit."""
    mask, gray = _brightness_mask(img)
    h, w = mask.shape
    rows = mask.sum(axis=1)
    cols = mask.sum(axis=0)
    r_idx = np.where(rows > max(2, w * 0.002))[0]
    c_idx = np.where(cols > max(2, h * 0.002))[0]
    if len(r_idx) == 0 or len(c_idx) == 0:
        return None
    top, bottom = int(r_idx[0]), int(r_idx[-1])
    left, right = int(c_idx[0]), int(c_idx[-1])
    if (bottom - top) < 0.15 * h or (right - left) < 0.15 * w:
        return None
    return left, top, right + 1, bottom + 1


def _trim_bright(work):
    """Weisse Raender um den Inhalt wegschneiden."""
    bbox = _content_bbox(work)
    if not bbox:
        return work
    pad_x = max(4, int((bbox[2] - bbox[0]) * 0.02))
    pad_y = max(4, int((bbox[3] - bbox[1]) * 0.02))
    left = max(0, bbox[0] - pad_x)
    top = max(0, bbox[1] - pad_y)
    right = min(work.width, bbox[2] + pad_x)
    bottom = min(work.height, bbox[3] + pad_y)
    return work.crop((left, top, right, bottom))


def _unused_old_deskew_removed():
    pass


# alte API beibehalten
def crop_white_borders(img):
    return _extract_page(img)


def to_a4(img, margin_mm=MARGIN_MM, dpi=DPI):
    """Setzt das Bild mittig auf eine weisse A4-Seite (Querformat bei Querformat)."""
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
    """Mehrere A4-Seiten in ein PDF schreiben."""
    pages = [to_a4(_extract_page(im), dpi=dpi) for im in images]
    if not pages:
        raise ValueError("keine Bilder")
    c = canvas.Canvas(str(out_path), pagesize=(595.2, 841.9))
    for p in pages:
        c.setPageSize((p.width / dpi * 72, p.height / dpi * 72))
        buf = io.BytesIO()
        # verlustfrei in die Zwischen-PDF: bessere OCR, ocrmypdf komprimiert danach
        p.save(buf, format="PNG", optimize=True)
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
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0 or not out_path.exists():
        return False, (res.stderr or res.stdout or "ocr failed")[-1500:]
    return True, "ok"


def _docker(job_id, args, timeout=900):
    """Kommando im OCR-Container ausfuehren (Pfade unter /work/<job_id>)."""
    cmd = ["docker", "exec", OCR_CONTAINER] + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ocr_page_pdf(job_id, png_name, page):
    """Eine Seite OCRen: Tesseract schreibt PDF mit unsichtbarem Textlayer."""
    base = png_name.rsplit(".", 1)[0]
    res = _docker(job_id, ["tesseract", f"/work/{job_id}/{png_name}",
                           f"/work/{job_id}/{base}", "-l", OCR_LANG, "--psm", "6", "pdf"])
    if res.returncode != 0:
        return None, (res.stderr or res.stdout or "tesseract fehlgeschlagen")[-1200:]
    return page / f"{base}.pdf", "ok"


def _merge_pdfs(parts, out_path):
    """Teil-PDFs zu einem Dokument zusammenfassen (PyMuPDF)."""
    import pymupdf
    doc = pymupdf.open()
    for part in parts:
        with pymupdf.open(str(part)) as d:
            doc.insert_pdf(d)
    doc.set_metadata({"producer": "Scan2PDF", "creator": "Scan2PDF"})
    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()


def _compress(job_id, in_name, out_name, dpi=200):
    """PDF verkleinern (Bilder als JPEG), Textlayer bleibt erhalten."""
    res = _docker(job_id, [
        "gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
        "-dPDFSETTINGS=/prepress",
        "-dAutoFilterColorImages=false", "-dColorImageFilter=/DCTEncode",
        "-dColorImageDownsampleType=/Bicubic", f"-dColorImageResolution={dpi}",
        "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/DCTEncode",
        "-dGrayImageDownsampleType=/Bicubic", f"-dGrayImageResolution={dpi}",
        "-dJPEGQ=85", "-dEmbedAllFonts=true", "-dSubsetFonts=true",
        f"-sOutputFile=/work/{job_id}/{out_name}", f"/work/{job_id}/{in_name}"],
        timeout=600)
    if res.returncode != 0:
        return False, (res.stderr or "ghostscript fehlgeschlagen")[-800:]
    return True, "ok"


def process_job(images, work_root, out_dir, do_ocr=True):
    """Kompletter Durchlauf. Gibt dict mit Ergebnis-Infos zurueck."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = Path(work_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    pages = [to_a4(_extract_page(im)) for im in images]
    if not pages:
        raise ValueError("keine Bilder")

    ocr_ok, ocr_msg, text = False, "deaktiviert", ""
    if do_ocr:
        parts, errors = [], []
        for idx, page in enumerate(pages):
            png = f"p{idx}.png"
            page.save(job_dir / png, dpi=(DPI, DPI))
            part, msg = _ocr_page_pdf(job_id, png, job_dir)
            if part is None:
                errors.append(msg)
                ocr_ok = False
                break
            parts.append(part)
        if parts:
            merged = job_dir / "merged.pdf"
            _merge_pdfs(parts, merged)
            small = job_dir / "final.pdf"
            ok, msg = _compress(job_id, "merged.pdf", "final.pdf")
            src = small if ok else merged
            text = pdf_text(src, max_chars=4000)
            ocr_ok, ocr_msg = True, "ok"
        else:
            ocr_msg = errors[-1] if errors else "ocr fehlgeschlagen"
            fallback = job_dir / "raw.pdf"
            images_to_pdf(images, fallback)
            src = fallback
    else:
        fallback = job_dir / "raw.pdf"
        images_to_pdf(images, fallback)
        src = fallback

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
        "pages": len(pages),
        "size_kb": round(final.stat().st_size / 1024),
        "ocr": ocr_ok,
        "ocr_msg": ocr_msg,
        "text": text[:1200],
    }


def pdf_text(path, max_chars=1500):
    """Text aus dem OCR-PDF lesen (PyMuPDF, sonst pdftotext im Container)."""
    try:
        import pymupdf  # type: ignore
        doc = pymupdf.open(str(path))
        txt = "\n".join(p.get_text() for p in doc)
        doc.close()
        return txt.strip()[:max_chars]
    except Exception:
        pass
    return ""
