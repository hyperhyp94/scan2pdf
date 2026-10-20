"""Neuen Chat im Hermes-WebUI anlegen und mit einer ersten Nachricht starten.

Das WebUI-Passwort bleibt auf dem Host (Env-Datei des WebUI-Dienstes) und wird weder
geloggt noch weitergegeben.
"""
import json
import os
import urllib.error
import urllib.request

WEBUI_URL = os.environ.get("WEBUI_URL", "http://127.0.0.1:5150")
WEBUI_PUBLIC = os.environ.get("WEBUI_PUBLIC", "http://192.168.1.134:5150")
ENV_FILE = os.path.expanduser("~/.config/hermes-webui/env")
WORKSPACE = "/home/sven/workspace"


def _password():
    pw = os.environ.get("HERMES_WEBUI_PASSWORD", "").strip()
    if pw:
        return pw
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            key, _, val = line.partition("=")
            if key.strip() == "HERMES_WEBUI_PASSWORD":
                return val.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _call(path, body, cookie=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(WEBUI_URL + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:300].decode("utf-8", "replace")
        return exc.code, exc.headers, {"error": raw}


def _login():
    pw = _password()
    if not pw:
        raise RuntimeError("WebUI-Passwort nicht gefunden")
    status, headers, _ = _call("/api/auth/login", {"password": pw})
    cookie = (headers.get("Set-Cookie") or "").split(";")[0]
    if status != 200 or not cookie:
        raise RuntimeError(f"WebUI-Login fehlgeschlagen (HTTP {status})")
    return cookie


def create_chat(title, message):
    """Legt Sitzung an, setzt den Titel, startet die erste Runde. -> (session_id, url)."""
    cookie = _login()
    status, _, js = _call("/api/session/new", {"workspace": WORKSPACE}, cookie)
    session = js.get("session") or {}
    sid = session.get("session_id")
    if status != 200 or not sid:
        raise RuntimeError(f"Sitzung anlegen fehlgeschlagen (HTTP {status}): {js}")
    try:
        _call("/api/session/rename", {"session_id": sid, "title": title[:80]}, cookie)
        status, _, js = _call("/api/chat/start", {"session_id": sid, "message": message}, cookie)
        if status != 200:
            raise RuntimeError(f"Chat starten fehlgeschlagen (HTTP {status}): {js}")
    except Exception:
        _call("/api/session/delete", {"session_id": sid}, cookie)   # keine leere Hülle hinterlassen
        raise
    return sid, f"{WEBUI_PUBLIC}/session/{sid}"


def delete_chat(session_id):
    cookie = _login()
    return _call("/api/session/delete", {"session_id": session_id}, cookie)[0]
