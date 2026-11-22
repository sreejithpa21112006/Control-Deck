"""
Redmi Web-Based Wireless Control Deck
-------------------------------------
Run on the laptop:   python server.py
Open on the phone:   http://<laptop-ip>:5000   (e.g. http://192.168.29.43:5000)

Install deps:        pip install flask pyautogui flask-sock
Optional (Windows):
  Auto keyboard      pip install uiautomation
  Now playing        pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams

Phone and laptop must be on the same Wi-Fi (or use ADB reverse: adb reverse tcp:5000 tcp:5000
and open http://localhost:5000 on the phone).

Shortcuts live in layouts. Each layout has up to 6 pages of 9 buttons and can be tied to
laptop apps (for example code.exe) so the right layout appears by itself. Tap a layout name
on the Shortcuts page to manage them. Saving needs the edit PIN, which is printed in this
console on startup and stored in deck_config.json next to this script.
"""

import asyncio
import base64
import io
import json
import os
import platform
import queue
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import zlib
from urllib.parse import urlparse

import pyautogui
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

IS_WINDOWS = platform.system() == "Windows"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deck_config.json")
SLOTS_PER_PAGE = 9
MAX_PAGES = 6
MAX_PROFILES = 8
SLOT_TYPES = ("hotkey", "url", "command")
MAX_LAUNCH = 8
POWER_ACTIONS = ("lock", "sleep", "screen", "restart", "shutdown", "cancel")
_lock = threading.RLock()

# ---------- Fixed controls (whitelisted) ----------
MEDIA_KEYS = {
    "playpause": "playpause",
    "next": "nexttrack",
    "prev": "prevtrack",
    "mute": "volumemute",
    "volup": "volumeup",
    "voldown": "volumedown",
}
KEY_NAMES = {"backspace", "tab", "enter", "esc", "left", "up", "right", "down", "delete", "space",
             "j", "k", "l", "c", "f", "m"}  # j k l c f m are the video player shortcuts
# Video player shortcuts that need Shift: playback speed down and up
VIDEO_HOTKEYS = {"slow": ("shift", ","), "fast": ("shift", ".")}
# Three-finger swipes on the trackpad
GESTURES = {
    "up": ("win", "tab"),            # Task View
    "down": ("win", "d"),            # Show desktop
    "left": ("alt", "shift", "tab"),  # Previous window
    "right": ("alt", "tab"),         # Next window
}


# ---------- Config: layouts (profiles) made of pages of buttons ----------
def _save(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _blank_page():
    return [None] * SLOTS_PER_PAGE


def _fix_page(page):
    page = page if isinstance(page, list) else []
    return (page + [None] * SLOTS_PER_PAGE)[:SLOTS_PER_PAGE]


def _normalize(cfg):
    """Bring a loaded config to the current shape. Returns True if anything changed.
    Older files with a single list of 6 buttons become the first page of the Default layout."""
    before = json.dumps(cfg, sort_keys=True)
    if not cfg.get("pin"):
        cfg["pin"] = f"{secrets.randbelow(10 ** 6):06d}"
    profiles = cfg.get("profiles")
    if not isinstance(profiles, list):
        profiles = [{"id": "default", "name": "Default", "apps": [], "pages": [cfg.get("slots")]}]
    cfg.pop("slots", None)
    out, seen = [], set()
    for p in profiles:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        pages = p.get("pages") if isinstance(p.get("pages"), list) else []
        pages = [_fix_page(pg) for pg in pages][:MAX_PAGES] or [_blank_page()]
        apps = p.get("apps") if isinstance(p.get("apps"), list) else []
        apps = [str(a).lower() for a in apps if isinstance(a, str)][:6]
        out.append({"id": pid, "name": str(p.get("name") or "Layout")[:16],
                    "apps": [] if pid == "default" else apps, "pages": pages})
    if "default" not in seen:
        out.insert(0, {"id": "default", "name": "Default", "apps": [], "pages": [_blank_page()]})
    out.sort(key=lambda p: p["id"] != "default")  # Default always first
    cfg["profiles"] = out[:MAX_PROFILES]
    tiles = []
    for t in (cfg.get("launch") if isinstance(cfg.get("launch"), list) else []):
        if isinstance(t, dict) and t.get("type") in ("url", "app"):
            tile, err = clean_launch(t.get("label"), t.get("value"))
            if tile and not err:
                tiles.append(tile)
    cfg["launch"] = tiles[:MAX_LAUNCH]
    return json.dumps(cfg, sort_keys=True) != before


def load_config():
    with _lock:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError):
            cfg = {}
        if _normalize(cfg):
            _save(cfg)
        return cfg


_pin_lock = threading.Lock()


def check_pin(pin):
    if secrets.compare_digest(str(pin or ""), load_config()["pin"]):
        return True
    with _pin_lock:  # wrong guesses are served one per second, even across connections
        time.sleep(1)
    return False


# ---------- Validation and execution of custom buttons ----------
def clean_slot(label, kind, value):
    """Return (slot, error). Normalises the value for each button type."""
    label = (label or "").strip()
    value = (value or "").strip()
    if not label or len(label) > 14:
        return None, "Name must be 1 to 14 characters."
    if kind not in SLOT_TYPES:
        return None, "Choose what the button does."
    if not value or len(value) > 300:
        return None, "Enter what the button should run."

    if kind == "hotkey":
        parts = [p.strip().lower() for p in value.split("+") if p.strip()]
        if not parts or len(parts) > 4:
            return None, "Use 1 to 4 keys joined with +, like ctrl+shift+m."
        bad = [p for p in parts if p not in pyautogui.KEYBOARD_KEYS]
        if bad:
            return None, "Unknown key: " + ", ".join(bad)
        value = "+".join(parts)
    elif kind == "url":
        if " " in value:
            return None, "A web address cannot contain spaces."
        if not value.lower().startswith(("http://", "https://")):
            value = "https://" + value

    return {"label": label, "type": kind, "value": value}, None


_URL_LIKE = re.compile(r"^(https?://\S+|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/\S*)?)$")
_PROGRAM_EXT = (".exe", ".lnk", ".bat", ".cmd", ".msi")


def clean_launch(label, value):
    """Return (tile, error) for a quick-launch tile: a website or a program."""
    label = (label or "").strip()
    value = (value or "").strip()
    if not label or len(label) > 14:
        return None, "Name must be 1 to 14 characters."
    if not value or len(value) > 260 or any(ord(c) < 32 for c in value):
        return None, "Enter a website or a program."
    if _URL_LIKE.match(value) and not value.lower().endswith(_PROGRAM_EXT):
        if not value.lower().startswith(("http://", "https://")):
            value = "https://" + value
        return {"label": label, "type": "url", "value": value}, None
    if any(c in value for c in '"&|<>^%'):
        return None, 'A program name cannot contain " & | < > ^ or %.'
    return {"label": label, "type": "app", "value": value}, None


def run_launch(tile):
    if tile["type"] == "url":
        webbrowser.open(tile["value"])
    elif IS_WINDOWS:
        subprocess.Popen('start "" "%s"' % tile["value"], shell=True)  # finds programs, folders and files like the Start menu
    else:
        subprocess.Popen(tile["value"].split())


def power_do(action):
    """Lock, sleep, switch the screen off, restart or shut down the laptop (Windows)."""
    if not IS_WINDOWS:
        print("  Power actions only work on Windows.")
        return
    import ctypes

    def work():
        try:
            if action == "lock":
                ctypes.windll.user32.LockWorkStation()
            elif action == "sleep":
                ctypes.windll.powrprof.SetSuspendState(False, True, False)
            elif action == "screen":
                ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)  # monitor off; moving the mouse wakes it
            elif action == "restart":
                subprocess.Popen(["shutdown", "/r", "/t", "15"])
            elif action == "shutdown":
                subprocess.Popen(["shutdown", "/s", "/t", "15"])
            elif action == "cancel":
                subprocess.Popen(["shutdown", "/a"])
        except Exception as exc:
            print("  Power action failed:", exc)

    threading.Thread(target=work, daemon=True).start()


def clean_profile(name, apps):
    """Return (profile fields, error). Apps are program file names such as code.exe."""
    name = (name or "").strip()
    if not name or len(name) > 16:
        return None, "Name must be 1 to 16 characters."
    raw = apps if isinstance(apps, list) else str(apps or "").split(",")
    out = []
    for a in raw:
        a = str(a).strip().lower()
        if not a:
            continue
        if not re.fullmatch(r"[a-z0-9._ \-]{1,60}", a):
            return None, "App names can use letters, numbers, dots, dashes and spaces."
        if "." not in a:
            a += ".exe"
        if a not in out:
            out.append(a)
    if len(out) > 6:
        return None, "Use at most 6 apps."
    return {"name": name, "apps": out}, None


def run_slot(slot):
    kind, value = slot["type"], slot["value"]
    if kind == "hotkey":
        pyautogui.hotkey(*value.split("+"))
    elif kind == "url":
        webbrowser.open(value)
    elif kind == "command":
        subprocess.Popen(value, shell=True)


def _find_profile(cfg, pid):
    for p in cfg["profiles"]:
        if p["id"] == pid:
            return p
    return None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _locate(cfg, data, need_slot=True):
    """Find (profile, page, index) named in a request, or return an error message."""
    prof = _find_profile(cfg, data.get("profile"))
    page, idx = data.get("page"), data.get("index")
    if prof is None or not _is_int(page) or not 0 <= page < len(prof["pages"]):
        return None, "That page no longer exists."
    if need_slot and (not _is_int(idx) or not 0 <= idx < SLOTS_PER_PAGE):
        return None, "Bad slot."
    return (prof, page, idx), None


def _bad_pin(data):
    if check_pin(data.get("pin")):
        return None
    return jsonify(ok=False, error="Wrong PIN."), 403


# ---------- Routes ----------
@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html", headers={"Cache-Control": "no-cache"})


@app.route("/ping")
def ping():
    return jsonify(ok=True)


@app.route("/action", methods=["POST"])
def action():
    name = (request.get_json(silent=True) or {}).get("name", "")
    if name in MEDIA_KEYS:
        pyautogui.press(MEDIA_KEYS[name])
        return jsonify(ok=True)
    return jsonify(ok=False, error="Unknown action."), 400


@app.route("/api/profiles")
def get_profiles():
    return jsonify(profiles=load_config()["profiles"])


@app.route("/api/press", methods=["POST"])
def press():
    data = request.get_json(silent=True) or {}
    found, err = _locate(load_config(), data)
    if err:
        return jsonify(ok=False, error=err), 400
    prof, page, idx = found
    slot = prof["pages"][page][idx]
    if not slot:
        return jsonify(ok=False, error="This slot is empty."), 400
    try:
        run_slot(slot)
    except Exception as exc:  # report launch failures back to the phone
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True)


@app.route("/api/slot", methods=["POST"])
def save_slot():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    slot, err = clean_slot(data.get("label"), data.get("type"), data.get("value"))
    if err:
        return jsonify(ok=False, error=err), 400
    with _lock:
        cfg = load_config()
        found, err = _locate(cfg, data)
        if err:
            return jsonify(ok=False, error=err), 400
        prof, page, idx = found
        prof["pages"][page][idx] = slot
        _save(cfg)
    return jsonify(ok=True, profiles=cfg["profiles"])


@app.route("/api/slot/delete", methods=["POST"])
def delete_slot():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    with _lock:
        cfg = load_config()
        found, err = _locate(cfg, data)
        if err:
            return jsonify(ok=False, error=err), 400
        prof, page, idx = found
        prof["pages"][page][idx] = None
        _save(cfg)
    return jsonify(ok=True, profiles=cfg["profiles"])


@app.route("/api/launch")
def get_launch():
    return jsonify(launch=load_config()["launch"])


@app.route("/api/launch", methods=["POST"])
def save_launch():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    tile, err = clean_launch(data.get("label"), data.get("value"))
    if err:
        return jsonify(ok=False, error=err), 400
    idx = data.get("index")
    with _lock:
        cfg = load_config()
        tiles = cfg["launch"]
        if _is_int(idx) and 0 <= idx < len(tiles):
            tiles[idx] = tile
        elif len(tiles) < MAX_LAUNCH:
            tiles.append(tile)
        else:
            return jsonify(ok=False, error="Up to %d tiles." % MAX_LAUNCH), 400
        _save(cfg)
    return jsonify(ok=True, launch=cfg["launch"])


@app.route("/api/launch/delete", methods=["POST"])
def delete_launch():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    idx = data.get("index")
    with _lock:
        cfg = load_config()
        if not _is_int(idx) or not 0 <= idx < len(cfg["launch"]):
            return jsonify(ok=False, error="That tile no longer exists."), 400
        del cfg["launch"][idx]
        _save(cfg)
    return jsonify(ok=True, launch=cfg["launch"])


@app.route("/api/page", methods=["POST"])
def change_page():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    with _lock:
        cfg = load_config()
        prof = _find_profile(cfg, data.get("profile"))
        if prof is None:
            return jsonify(ok=False, error="That layout no longer exists."), 400
        if data.get("action") == "add":
            if len(prof["pages"]) >= MAX_PAGES:
                return jsonify(ok=False, error=f"A layout can have {MAX_PAGES} pages at most."), 400
            prof["pages"].append(_blank_page())
        elif data.get("action") == "delete":
            page = data.get("page")
            if len(prof["pages"]) < 2:
                return jsonify(ok=False, error="A layout needs at least one page."), 400
            if not _is_int(page) or not 0 <= page < len(prof["pages"]):
                return jsonify(ok=False, error="That page no longer exists."), 400
            prof["pages"].pop(page)
        else:
            return jsonify(ok=False, error="Unknown change."), 400
        _save(cfg)
    return jsonify(ok=True, profiles=cfg["profiles"])


@app.route("/api/profile", methods=["POST"])
def save_profile():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    fields, err = clean_profile(data.get("name"), data.get("apps"))
    if err:
        return jsonify(ok=False, error=err), 400
    with _lock:
        cfg = load_config()
        prof = _find_profile(cfg, data.get("id")) if data.get("id") else None
        if data.get("id") and prof is None:
            return jsonify(ok=False, error="That layout no longer exists."), 400
        if prof is None:
            if len(cfg["profiles"]) >= MAX_PROFILES:
                return jsonify(ok=False, error=f"You can have {MAX_PROFILES} layouts at most."), 400
            prof = {"id": secrets.token_hex(3), "name": "", "apps": [], "pages": [_blank_page()]}
            cfg["profiles"].append(prof)
        prof["name"] = fields["name"]
        if prof["id"] != "default":
            prof["apps"] = fields["apps"]
        _save(cfg)
    return jsonify(ok=True, id=prof["id"], profiles=cfg["profiles"])


@app.route("/api/profile/delete", methods=["POST"])
def delete_profile():
    data = request.get_json(silent=True) or {}
    denied = _bad_pin(data)
    if denied:
        return denied
    with _lock:
        cfg = load_config()
        pid = data.get("id")
        if pid == "default" or _find_profile(cfg, pid) is None:
            return jsonify(ok=False, error="That layout cannot be deleted."), 400
        cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != pid]
        _save(cfg)
    return jsonify(ok=True, profiles=cfg["profiles"])


# ---------- Full-screen app files (manifest, icons, service worker, keep-awake video) ----------
AWAKE_MP4 = base64.b64decode("AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAANObW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAB9AAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAnh0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAB9AAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAfQAAAAAAABAAAAAAHwbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAAUABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABm21pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAVtzdGJsAAAAu3N0c2QAAAAAAAAAAQAAAKthdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFUxhdmM2MC4zMS4xMDIgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAAMWF2Y0MBQsAK/+EAGGdCwAqmERCbARAAAAMAEAAAAwCg8SJhGAEABmjIQgOSyAAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAAAvIAAALyAAAABhzdHRzAAAAAAAAAAEAAAAKAAAIAAAAABRzdHNzAAAAAAAAAAEAAAABAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAKAAAAAQAAADxzdHN6AAAAAAAAAAAAAAAKAAACkAAAAAoAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAABRzdGNvAAAAAAAAAAEAAAN+AAAAYnVkdGEAAABabWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEAAAABAAAAAExhdmY2MC4xNi4xMDAAAAAIZnJlZQAAAvptZGF0AAACcgYF//9u3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCByMzEwOCAzMWUxOWY5IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTAgcmVmPTE2IGRlYmxvY2s9MTowOjAgYW5hbHlzZT0weDE6MHgxMzEgbWU9dW1oIHN1Ym1lPTEwIHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MjQgY2hyb21hX21lPTEgdHJlbGxpcz0yIDh4OGRjdD0wIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MiBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD0yNTAga2V5aW50X21pbj01IHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NjAgcmM9Y3JmIG1idHJlZT0xIGNyZj00MC4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAFmWIggI+TFAAEW2+++uuuuuuuuuuuvAAAAAGQZocBHwjAAAAB0GaKgEfCMAAAAAHQZo7AR8IwAAAAAdBmkkAR8IwAAAAB0GaWUBHwjAAAAAHQZppgEfCMAAAAAdBmnnAR8IwAAAAB0GaiIAQ8IwAAAAHQZqYkD/CMA==")
SW_JS = (
    "self.addEventListener('install', function () { self.skipWaiting(); });\n"
    "self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });\n"
    "self.addEventListener('fetch', function () {});\n"
)
_icons = {}


def _icon_png(size):
    """A black square with a white 3x3 grid, drawn without any image library."""
    if size in _icons:
        return _icons[size]
    inner = int(size * 0.6)
    off = (size - inner) // 2
    cell = inner // 3
    gap = max(2, cell // 7)
    lo, hi = gap // 2, gap - gap // 2
    black = b"\x00" + b"\x00" * (size * 3)  # leading byte is the PNG row filter
    white = bytearray(black)
    for c in range(3):
        for x in range(off + c * cell + lo, off + (c + 1) * cell - hi):
            white[1 + x * 3:4 + x * 3] = b"\xff\xff\xff"
    raw = bytearray()
    for y in range(size):
        in_tile = off <= y < off + 3 * cell and lo <= (y - off) % cell < cell - hi
        raw += white if in_tile else black

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    _icons[size] = png
    return png


@app.route("/manifest.webmanifest")
def manifest():
    body = {
        "name": "Control Deck", "short_name": "Deck", "start_url": "/", "scope": "/",
        "display": "fullscreen", "display_override": ["fullscreen", "standalone"],
        "orientation": "any", "background_color": "#000000", "theme_color": "#000000",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(body), mimetype="application/manifest+json")


@app.route("/icon-<int:size>.png")
def icon(size):
    if size not in (192, 512):
        return Response(status=404)
    return Response(_icon_png(size), mimetype="image/png", headers={"Cache-Control": "max-age=86400"})


@app.route("/sw.js")
def service_worker():
    return Response(SW_JS, mimetype="text/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/awake.mp4")
def awake_video():
    return send_file(io.BytesIO(AWAKE_MP4), mimetype="video/mp4", conditional=True, max_age=3600)


# ---------- Mouse and keyboard on the laptop ----------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _BTN = {("l", "d"): 0x0002, ("l", "u"): 0x0004, ("r", "d"): 0x0008, ("r", "u"): 0x0010}

    def mouse_move(dx, dy):
        # Direct Windows calls: much faster than pyautogui and no extra pointer acceleration
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        _user32.SetCursorPos(pt.x + dx, pt.y + dy)

    def mouse_button(btn, state):
        _user32.mouse_event(_BTN[(btn, state)], 0, 0, 0, 0)

    def mouse_scroll(dx, dy):
        if dy:
            _user32.mouse_event(0x0800, 0, 0, dy, 0)  # wheel, 120 units = one notch
        if dx:
            _user32.mouse_event(0x01000, 0, 0, dx, 0)  # horizontal wheel

    # Keyboard input through SendInput. Text goes in as Unicode, so any character
    # (accents, symbols, emoji) types correctly in any program.
    _VK = {"backspace": 0x08, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "space": 0x20,
           "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "delete": 0x2E,
           "c": 0x43, "f": 0x46, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D}
    _EXTENDED = {"left", "up", "right", "down", "delete"}

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def _send_keys(events):
        n = len(events)
        arr = (_INPUT * n)()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = 1  # INPUT_KEYBOARD
            arr[i].u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))

    def _key_events(name, times=1):
        ext = 0x0001 if name in _EXTENDED else 0  # KEYEVENTF_EXTENDEDKEY
        events = []
        for _ in range(times):
            events.append((_VK[name], 0, ext))
            events.append((_VK[name], 0, ext | 0x0002))  # KEYEVENTF_KEYUP
        return events

    def press_key(name, times=1):
        _send_keys(_key_events(name, times))

    def type_text(text):
        events = []
        for ch in text:
            if ch == "\n":
                events += _key_events("enter")
            elif ch == "\t":
                events += _key_events("tab")
            elif ch.isprintable():
                data = ch.encode("utf-16-le", "ignore")
                for i in range(0, len(data), 2):
                    unit = int.from_bytes(data[i:i + 2], "little")
                    events.append((0, unit, 0x0004))           # KEYEVENTF_UNICODE
                    events.append((0, unit, 0x0004 | 0x0002))  # ... key up
        if events:
            _send_keys(events)

    # Name of the program that has focus, for switching layouts by app
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def _exe_of_hwnd(hwnd):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            _kernel32.CloseHandle(handle)
        return ""

    def foreground_exe():
        hwnd = _user32.GetForegroundWindow()
        return _exe_of_hwnd(hwnd) if hwnd else ""

    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]

    def visible_windows():
        """(program, title) for every visible window that has a title."""
        found = []

        def visit(hwnd, _):
            try:
                if _user32.IsWindowVisible(hwnd):
                    n = _user32.GetWindowTextLengthW(hwnd)
                    if n:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        _user32.GetWindowTextW(hwnd, buf, n + 1)
                        found.append((_exe_of_hwnd(hwnd), buf.value))
            except Exception:
                pass
            return True

        _user32.EnumWindows(_EnumProc(visit), 0)
        return found
else:
    _scroll_acc = [0, 0]

    def mouse_move(dx, dy):
        pyautogui.moveRel(dx, dy, _pause=False)

    def mouse_button(btn, state):
        name = "left" if btn == "l" else "right"
        (pyautogui.mouseDown if state == "d" else pyautogui.mouseUp)(button=name)

    def mouse_scroll(dx, dy):
        _scroll_acc[0] += dx
        _scroll_acc[1] += dy
        cx, cy = int(_scroll_acc[0] / 120), int(_scroll_acc[1] / 120)
        _scroll_acc[0] -= cx * 120
        _scroll_acc[1] -= cy * 120
        if cy:
            pyautogui.scroll(cy)
        if cx:
            pyautogui.hscroll(cx)

    def press_key(name, times=1):
        pyautogui.press(name, presses=times, interval=0)

    def type_text(text):
        pyautogui.write(text, interval=0)  # plain keyboard characters only on this system

    def foreground_exe():
        return ""

    def visible_windows():
        return []


def _clamp(text, limit):
    return max(-limit, min(limit, int(text)))


def new_state():
    return {"authed": False, "held": set(), "fails": 0, "on": True, "lock": threading.Lock()}


def handle_pad_message(msg, state):
    """One short text message from the phone. Returns a reply string or None.
    a pin   sign in        p t   ping (allowed before sign in)
    q 1|0   phone page showing or hidden      l n   open quick-launch tile n      x action   lock, sleep, screen, restart, shutdown, cancel
    v n   set volume 0-100      o id   switch sound output      m dx dy   move      s dx dy   scroll      g dir   three-finger swipe
    d l|r   button down      u l|r   button up
    t text   type text      k name   press a key      b n   backspace n times"""
    parts = msg.split()
    if not parts:
        return None
    cmd = parts[0]
    held = state["held"]
    try:
        if cmd == "p" and len(msg) < 40:
            return msg
        if cmd == "a":
            if check_pin(msg[2:].strip()):
                state["authed"] = True
                return "a ok"
            state["fails"] += 1
            return "a bad"
        if not state["authed"]:
            return "a need"
        if cmd == "t":
            type_text(msg[2:402])
        elif cmd == "k" and len(parts) == 2 and parts[1] in KEY_NAMES:
            press_key(parts[1])
        elif cmd == "b" and len(parts) == 2:
            n = max(0, min(100, int(parts[1])))
            if n:
                press_key("backspace", n)
        elif cmd == "m" and len(parts) == 3:
            dx, dy = _clamp(parts[1], 1500), _clamp(parts[2], 1500)
            if dx or dy:
                mouse_move(dx, dy)
        elif cmd == "s" and len(parts) == 3:
            dx, dy = _clamp(parts[1], 5000), _clamp(parts[2], 5000)
            if dx or dy:
                mouse_scroll(dx, dy)
        elif cmd == "q" and len(parts) == 2:
            state["on"] = parts[1] == "1"  # the phone page is showing (1) or hidden (0)
        elif cmd == "l" and len(parts) == 2:
            tiles = load_config()["launch"]
            i = int(parts[1])
            if 0 <= i < len(tiles):
                run_launch(tiles[i])
        elif cmd == "x" and len(parts) == 2 and parts[1] in POWER_ACTIONS:
            power_do(parts[1])
        elif cmd == "o" and len(parts) == 2:
            _audio_q.put(("dev", parts[1]))  # switched by the audio thread, only to a device that is listed
        elif cmd == "h" and len(parts) == 2 and parts[1] in VIDEO_HOTKEYS:
            pyautogui.hotkey(*VIDEO_HOTKEYS[parts[1]])
        elif cmd == "v" and len(parts) == 2:
            _audio_q.put(("vol", max(0, min(100, int(parts[1])))))
        elif cmd == "g" and len(parts) == 2 and parts[1] in GESTURES:
            pyautogui.hotkey(*GESTURES[parts[1]])
        elif cmd in ("d", "u") and len(parts) == 2 and parts[1] in ("l", "r"):
            if cmd == "d" and parts[1] not in held:
                held.add(parts[1])
                mouse_button(parts[1], "d")
            elif cmd == "u" and parts[1] in held:
                held.discard(parts[1])
                mouse_button(parts[1], "u")
    except ValueError:
        pass
    return None


# ---------- Live link to the phone (WebSocket) ----------
try:
    from flask_sock import Sock
except ImportError:  # the trackpad stays off until flask-sock is installed
    Sock = None

sock = Sock(app) if Sock else None

_clients = {}  # open phone connections -> their state
_clients_lock = threading.Lock()
# What the laptop is doing right now. New connections are told all of it.
_status = {"focus": False, "app": "", "media": None, "audio": None, "vol": None}


def _send(ws, state, text):
    with state["lock"]:  # the socket is written from more than one thread
        ws.send(text)


def _broadcast(text):
    with _clients_lock:
        items = list(_clients.items())
    for ws, st in items:
        try:
            _send(ws, st, text)
        except Exception:
            pass


def _has_clients():
    with _clients_lock:
        return bool(_clients)


def _has_active():
    """A phone is connected and its page is on screen. The busy checks only run then."""
    with _clients_lock:
        return any(st.get("on", True) for st in _clients.values())


def _hello():
    msgs = ["f 1" if _status["focus"] else "f 0"]
    if _status["app"]:
        msgs.append("w " + _status["app"])
    if _status["media"] is not None:
        msgs.append("n " + json.dumps(_status["media"]))
    if _status["audio"] is not None:
        msgs.append("o " + json.dumps(_status["audio"]))
    if _status["vol"] is not None:
        msgs.append("v %d %d" % _status["vol"])
    return msgs


# Windows only: a text box has focus (opens the phone keyboard)
def _is_text_box(ctl, auto):
    if ctl is None or ctl.ControlType != auto.ControlType.EditControl:
        return False
    try:
        pattern = ctl.GetValuePattern()
        if pattern is not None and pattern.IsReadOnly:
            return False
    except Exception:
        pass
    return True


def _watch_text_focus():
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        candidate, streak = False, 0
        while True:
            if not _has_active():  # no phone showing the deck, so do no work
                candidate, streak = _status["focus"], 0
                time.sleep(1)
                continue
            try:
                now = _is_text_box(auto.GetFocusedControl(), auto)
            except Exception:
                now = False
            if now == candidate:
                streak += 1
            else:
                candidate, streak = now, 1
            if streak >= 2 and candidate != _status["focus"]:  # must hold for two checks, avoids flicker
                _status["focus"] = candidate
                _broadcast("f 1" if candidate else "f 0")
            time.sleep(0.35)


# Windows only: which program has focus (switches the shortcut layout)
def _watch_foreground():
    candidate, streak = "", 0
    while True:
        if not _has_active():
            candidate, streak = "", 0
            time.sleep(1)
            continue
        try:
            now = foreground_exe()
        except Exception:
            now = ""
        if now == candidate:
            streak += 1
        else:
            candidate, streak = now, 1
        if now and streak >= 2 and now != _status["app"]:  # steady for two checks, ignores the Alt+Tab flicker
            _status["app"] = now
            _broadcast("w " + now)
        time.sleep(0.5)


# Windows only: sound output devices (headphones, speakers, ...) and switching the default one.
# Everything runs on one thread because Windows audio objects must stay on the thread that made them.
_audio_q = queue.Queue()


def audio_devices():
    """Active playback devices as [{id, name, cur}]. Needs: pip install pycaw comtypes"""
    from pycaw.pycaw import AudioUtilities
    try:
        spk = AudioUtilities.GetSpeakers()
        cur = (spk.GetId() if hasattr(spk, "GetId") else getattr(spk, "id", "")) if spk else ""
    except Exception:
        cur = ""
    out = []
    for d in AudioUtilities.GetAllDevices():
        dev_id = str(getattr(d, "id", "") or "")
        if not dev_id.startswith("{0.0.0."):  # playback devices only, not microphones
            continue
        if int(getattr(d.state, "value", d.state)) != 1:  # connected and enabled
            continue
        name = str(getattr(d, "FriendlyName", "") or "").strip()
        if re.search(r"hands.?free|microphone|\bmic\b|line in|stereo mix", name, re.I):  # input-style entries
            continue
        if name:
            out.append({"id": dev_id, "name": name, "cur": dev_id == cur})
    out.sort(key=lambda x: x["name"].lower())
    return out


_policy = {}


def set_default_audio(dev_id):
    """Make a device the default for media, apps and calls (the same as the Windows sound menu)."""
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import c_int, c_void_p, c_wchar_p
    if "cls" not in _policy:
        names = ["GetMixFormat", "GetDeviceFormat", "ResetDeviceFormat", "SetDeviceFormat", "GetProcessingPeriod",
                 "SetProcessingPeriod", "GetShareMode", "SetShareMode", "GetPropertyValue", "SetPropertyValue"]

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = tuple(COMMETHOD([], HRESULT, n) for n in names) + (
                COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], c_wchar_p, "id"), (["in"], c_int, "role")),
                COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], c_wchar_p, "id"), (["in"], c_int, "show")),
            )
        _policy["cls"] = IPolicyConfig
    cfg = comtypes.CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), _policy["cls"],
                                    clsctx=comtypes.CLSCTX_ALL)
    for role in (0, 1, 2):  # console, multimedia, communications
        cfg.SetDefaultEndpoint(dev_id, role)


def _endpoint_volume():
    from pycaw.pycaw import AudioUtilities
    spk = AudioUtilities.GetSpeakers()
    if hasattr(spk, "EndpointVolume"):  # newer pycaw
        return spk.EndpointVolume
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    return cast(spk.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))


def volume_get():
    """(level 0-100, muted) of the default sound output."""
    ep = _endpoint_volume()
    return int(round(ep.GetMasterVolumeLevelScalar() * 100)), int(bool(ep.GetMute()))


def volume_set(level):
    ep = _endpoint_volume()
    ep.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100.0, None)
    if level > 0 and ep.GetMute():
        ep.SetMute(0, None)


_audio_seen = set()


def _audio_note(what, exc):
    if (what, str(exc)) not in _audio_seen:
        _audio_seen.add((what, str(exc)))
        print("  %s failed: %s" % (what, exc))


def _audio_tick(list_fn):
    try:
        devs = list_fn()
    except Exception as exc:
        _audio_note("Sound output list", exc)
        return
    if devs != _status["audio"]:
        _status["audio"] = devs
        _broadcast("o " + json.dumps(devs))


def _volume_tick(vol_get):
    try:
        vol = vol_get()
    except Exception as exc:
        _audio_note("Reading the volume", exc)
        return
    if vol != _status["vol"]:
        _status["vol"] = vol
        _broadcast("v %d %d" % vol)


def _audio_loop(list_fn, set_fn, vol_get=None, vol_set=None, stop=None):
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    last_list = 0.0
    while not (stop and stop.is_set()):
        jobs = []
        try:
            jobs.append(_audio_q.get(timeout=0.5 if _has_clients() else 2))
            while True:  # take everything waiting: only the newest volume matters
                jobs.append(_audio_q.get_nowait())
        except queue.Empty:
            pass
        want_dev = want_vol = None
        for kind, val in jobs:
            if kind == "dev":
                want_dev = val
            elif kind == "vol":
                want_vol = val
        if want_dev is not None and want_dev in [d["id"] for d in (_status["audio"] or [])]:  # only a listed device
            try:
                set_fn(want_dev)
            except Exception as exc:
                print("  Could not switch sound output:", exc)
        if want_vol is not None and vol_set:
            try:
                vol_set(want_vol)
            except Exception as exc:
                _audio_note("Setting the volume", exc)
        if jobs or _has_clients():
            if jobs or time.time() - last_list > 2:
                _audio_tick(list_fn)
                last_list = time.time()
            if vol_get:
                _volume_tick(vol_get)


# Windows only: what is playing. The Windows media controls know almost every player (browsers,
# Spotify, VLC, ...). Without the packages for them, Spotify and VLC are still read from their window titles.
def _media_manager_class():
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        return M
    except Exception:
        pass
    try:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        return M
    except Exception:
        return None


def media_from_titles(windows):
    """Spotify shows 'Artist - Song' in its title while playing, VLC shows 'Song - VLC media player'."""
    for exe, title in windows:
        if exe == "spotify.exe" and " - " in title and title not in ("Spotify", "Spotify Free", "Spotify Premium"):
            artist, _, song = title.partition(" - ")
            return {"on": True, "t": song, "a": artist, "p": True, "src": "title"}
        if exe == "vlc.exe" and title.endswith(" - VLC media player"):
            return {"on": True, "t": title[:-len(" - VLC media player")], "a": "", "p": True, "src": "title"}
    return None


async def _winrt_info(manager):
    session = manager.get_current_session()
    if not session:
        return None
    props = await session.try_get_media_properties_async()
    status = session.get_playback_info().playback_status
    playing = int(getattr(status, "value", status)) == 4  # 4 = playing
    return {"on": True, "t": props.title or "", "a": props.artist or "", "p": playing, "src": "winrt"}


async def _media_loop(manager_cls, stop=None):
    manager, note = None, ""
    if manager_cls:
        try:
            try:
                from winrt.runtime import init_apartment  # lets this thread use Windows media; harmless if done
                init_apartment()
            except Exception:
                pass
            manager = await manager_cls.request_async()
        except Exception as exc:
            print("  Now playing: could not open the Windows media controls:", exc)
            note = ("Media controls failed: " + str(exc))[:90]
    else:
        note = "Only Spotify and VLC are detected. See the laptop console for the rest"
    last_err = None
    while stop is None or not stop.is_set():
        if not _has_clients():
            await asyncio.sleep(2)
            continue
        info = None
        if manager:
            try:
                info = await _winrt_info(manager)
                note, last_err = "", None
            except Exception as exc:
                if str(exc) != last_err:  # say it once in the console, keep showing it on the phone
                    print("  Now playing error:", exc)
                    last_err = str(exc)
                note = ("Media error: " + str(exc))[:90]
        if info is None:
            try:
                info = media_from_titles(visible_windows())
            except Exception:
                info = None
        if info is None:
            info = {"on": False, "t": "", "a": "", "p": False, "src": "none"}
            if note:
                info["note"] = note
        if info != _status["media"]:
            _status["media"] = info
            _broadcast("n " + json.dumps(info))
        await asyncio.sleep(1.2)


def _run_media(manager_cls, stop=None):
    try:
        asyncio.run(_media_loop(manager_cls, stop))
    except Exception as exc:
        print("  Now playing stopped:", exc)


def start_watchers():
    """Start the Windows-only background checks. Returns a note for each one that is off."""
    if not sock:
        return ["Trackpad, typing and now playing are off. Run:  pip install flask-sock   then restart."]
    if not IS_WINDOWS:
        return ["Layouts by app, now playing and the auto keyboard only work on Windows."]
    notes = []
    threading.Thread(target=_watch_foreground, daemon=True).start()
    try:
        import uiautomation  # noqa: F401
        threading.Thread(target=_watch_text_focus, daemon=True).start()
    except ImportError:
        notes.append("Auto keyboard is off. Run:  pip install uiautomation   then restart.")
    try:
        import comtypes  # noqa: F401
        import pycaw.pycaw  # noqa: F401
        threading.Thread(target=_audio_loop, args=(audio_devices, set_default_audio, volume_get, volume_set), daemon=True).start()
    except ImportError:
        notes.append("Sound output switching is off. Run:  pip install pycaw comtypes   then restart.")
    manager_cls = _media_manager_class()
    threading.Thread(target=_run_media, args=(manager_cls,), daemon=True).start()
    if not manager_cls:
        notes.append("Now playing only sees Spotify and VLC. For every other player run:  pip install winrt-runtime "
                     "winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections "
                     "winrt-Windows.Storage.Streams   then restart.")
    return notes


if sock:
    @sock.route("/ws")
    def pad_socket(ws):
        # Refuse pages from other sites that try to reach this socket
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname != request.host.rsplit(":", 1)[0].strip("[]"):
            return
        state = new_state()
        with _clients_lock:
            _clients[ws] = state
        try:
            for hello in _hello():
                _send(ws, state, hello)
            while state["fails"] < 5:  # too many wrong PINs ends the connection
                msg = ws.receive()
                if msg is None:
                    break
                reply = handle_pad_message(str(msg), state)
                if reply:
                    _send(ws, state, reply)
        except Exception:
            pass  # connection closed
        finally:
            with _clients_lock:
                _clients.pop(ws, None)
            for b in list(state["held"]):  # never leave a mouse button stuck down
                mouse_button(b, "u")


# ---------- Front end ----------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#000000">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Control Deck</title>
<style>
  :root {
    --bg: #000;
    --surface: #101010;
    --surface-2: #1a1a1a;
    --edge: #3a3a3a;
    --text: #fff;
    --muted: #8c8c8c;
    --gap: 8px;
    --font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html, body {
    height: 100%; width: 100%; overflow: hidden;
    background: var(--bg); color: var(--text); font-family: var(--font);
    touch-action: manipulation; user-select: none; -webkit-user-select: none;
    overscroll-behavior: none;
  }
  input, select { user-select: text; -webkit-user-select: text; }
  button { font-family: var(--font); }
  .hidden { display: none !important; }

  /* connection bar: solid when connected, striped when the laptop cannot be reached */
  /* connection bar: only shows (dashed) while the laptop cannot be reached */
  #status { display: none; position: fixed; top: 0; left: 0; right: 0; height: 4px; z-index: 50; }
  body.offline #status { display: block; background: repeating-linear-gradient(90deg, #fff 0 8px, #000 8px 16px); }

  .deck {
    display: flex; flex-direction: column;
    height: 100vh; height: 100dvh; width: 100vw;
    padding-top: calc(4px + env(safe-area-inset-top));
    transition: opacity .3s;
  }
  body.offline .deck { opacity: .4; }

  /* pages side by side: swipe, or tap a tab */
  .pager {
    flex: 1; min-height: 0; display: flex;
    overflow-x: auto; overflow-y: hidden;
    scroll-snap-type: x mandatory; overscroll-behavior-x: contain;
    scrollbar-width: none; -webkit-overflow-scrolling: touch;
  }
  .pager::-webkit-scrollbar { display: none; }
  .page {
    flex: 0 0 100%; min-width: 0;
    scroll-snap-align: start; scroll-snap-stop: always;
    display: flex; flex-direction: column; gap: var(--gap);
    padding: var(--gap) calc(var(--gap) + env(safe-area-inset-right)) var(--gap) calc(var(--gap) + env(safe-area-inset-left));
  }
  .row { display: grid; gap: var(--gap); flex: 1; min-height: 0; grid-template-columns: repeat(3, 1fr); }
  .transport { grid-template-columns: 1fr 1.5fr 1fr; flex: 1.3; }

  .tabs {
    display: flex; flex: none; box-sizing: content-box; height: 46px;
    padding-bottom: env(safe-area-inset-bottom); border-top: 1px solid var(--edge);
  }
  .tab {
    -webkit-appearance: none; appearance: none; flex: 1; position: relative; min-width: 0;
    background: none; border: none; color: var(--muted); font: 600 14px var(--font);
  }
  .tab.active { color: #fff; }
  .tab.active::after {
    content: ''; position: absolute; top: -1px; left: 28%; right: 28%; height: 3px;
    background: #fff; border-radius: 0 0 3px 3px;
  }
  .tab:focus-visible { outline: 2px solid #fff; outline-offset: -4px; }

  .btn {
    -webkit-appearance: none; appearance: none;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: clamp(5px, 1.4vh, 12px); min-height: 0; min-width: 0; padding: 0 6px;
    background: var(--surface); color: var(--text);
    border: 1px solid var(--edge); border-radius: 20px;
    font: 600 clamp(13px, 3.6vw, 17px)/1.1 var(--font);
    transition: transform .07s ease, background .07s ease, color .07s ease, border-color .07s ease;
  }
  .btn svg {
    width: clamp(24px, min(9vw, 8vh), 50px); height: auto; flex: none;
    fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
  }
  .btn svg .f { fill: currentColor; }
  .btn .label { color: var(--muted); max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn:active { transform: scale(.95); background: #fff; color: #000; border-color: #fff; }
  .btn:active .label { color: #000; }
  .btn:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }

  .btn.primary { background: #fff; color: #000; border-color: #fff; }
  .btn.primary .label { color: #000; }
  .btn.primary:active { background: #cfcfcf; border-color: #cfcfcf; }
  .btn.primary svg { width: clamp(32px, min(12vw, 11vh), 68px); }

  .btn.custom .label { color: var(--text); font-size: clamp(14px, 4vw, 19px); }
  .btn.custom:active .label { color: #000; }
  .btn.empty { background: transparent; border-style: dashed; color: var(--muted); }
  .btn.empty .label { color: var(--muted); }
  .btn.empty:active { background: #fff; color: #000; }

  /* now playing: the biggest thing on the Controls page */
  .ctl { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: var(--gap); }
  #now {
    -webkit-appearance: none; appearance: none; flex: 0 0 clamp(124px, 25%, 210px); min-width: 0;
    display: flex; align-items: center; gap: 18px; padding: 16px 20px; text-align: left; color: #fff;
    background: var(--surface); border: 1px solid #fff; border-radius: 22px;
    transition: background .1s ease, transform .07s ease;
  }
  #now:active { background: var(--surface-2); transform: scale(.985); }
  #now.idle { border-color: var(--edge); }
  .nowText { flex: 1; min-width: 0; }
  #nowTitle {
    font: 700 clamp(22px, 6.6vw, 34px)/1.15 var(--font);
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden;
    overflow-wrap: anywhere;
  }
  #now.idle #nowTitle { color: var(--muted); }
  #nowArtist {
    margin-top: 6px; font: 500 clamp(15px, 4.4vw, 21px)/1.3 var(--font); color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #nowArtist:empty { display: none; }
  /* volume knob */
  .deckKnob { flex: 1; min-height: 0; display: flex; align-items: center; gap: var(--gap); }
  .deckKnob .side { flex: 0 0 clamp(60px, 18%, 96px); height: clamp(64px, 34%, 130px); }
  #knob { flex: 1; min-width: 0; height: 100%; touch-action: none; cursor: pointer; outline: none; -webkit-user-select: none; user-select: none; }
  #knob svg { display: block; width: 100%; height: 100%; }
  #knob .cap { fill: #0a0a0a; stroke: #2c2c2c; stroke-width: 1.5; }
  #knob .capIn { fill: #111; stroke: #1c1c1c; stroke-width: 1; }
  #knob:active .capIn, #knob.turning .capIn { fill: #181818; }
  #knob:focus-visible .cap { stroke: #fff; }
  #knob .tick { stroke: #3a3a3a; stroke-width: 2.4; stroke-linecap: round; }
  #knob .tick.lit { stroke: #fff; }
  #knob.muted .tick.lit { stroke: #777; }
  #dot circle { fill: #fff; }
  #knob.muted #dot circle { fill: #777; }
  #knob .f { fill: #fff; stroke: none; }
  #knob .kl { fill: var(--muted); font: 600 9px var(--font); letter-spacing: .5px; }
  #kNum { fill: var(--muted); font: 600 14px var(--font); }
  @media (min-height: 461px) {  /* upright phone: the knob fills the width, previous and next sit in the bottom corners */
    .deckKnob { position: relative; justify-content: center; }
    .deckKnob .side { position: absolute; bottom: 0; width: 46px; height: 46px; flex: none; border-radius: 23px; }
    .deckKnob .side:first-child { left: 0; }
    .deckKnob .side:last-child { right: 0; }
  }
  #presets { flex: none; display: flex; gap: var(--gap); }
  .chip {
    -webkit-appearance: none; appearance: none; flex: 1; min-height: 48px; padding: 0 8px; color: var(--text);
    background: var(--surface); border: 1px solid var(--edge); border-radius: 14px; font: 600 clamp(15px, 4.2vw, 18px) var(--font);
    -webkit-touch-callout: none; touch-action: manipulation;
  }
  .chip:active { background: var(--surface-2); }
  .chip.on, .chip[aria-pressed="true"] { background: #fff; border-color: #fff; color: #000; }
  .chip.hidden { display: none; }
  /* sound output: a drop-down at the bottom of the Controls page */
  #outs { flex: none; position: relative; }
  #outs.hidden { display: none; }
  #outBtn {
    -webkit-appearance: none; appearance: none; width: 100%; min-height: 56px; display: flex; align-items: center; gap: 12px;
    padding: 9px 16px; text-align: left; color: var(--text); background: var(--surface); border: 1px solid var(--edge); border-radius: 16px;
  }
  #outBtn:active { background: var(--surface-2); }
  #outBtn svg { flex: none; width: 22px; height: 22px; fill: none; stroke: #fff; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  #outBtn .chev { transition: transform .15s ease; }
  #outs.open #outBtn { border-color: #fff; }
  #outs.open .chev { transform: rotate(180deg); }
  .outText { flex: 1; min-width: 0; }
  .outText small, .outText b { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .outText small { font: 500 12px/1.3 var(--font); color: var(--muted); }
  .outText b { font: 600 clamp(15px, 4.2vw, 18px)/1.3 var(--font); }
  #outMenu {
    position: absolute; left: 0; right: 0; bottom: calc(100% + 8px); z-index: 30; max-height: 52vh; overflow-y: auto;
    padding: 6px; background: var(--surface); border: 1px solid #fff; border-radius: 18px; box-shadow: 0 8px 30px rgba(0, 0, 0, .8);
  }
  #outMenu[hidden] { display: none; }
  .opt {
    -webkit-appearance: none; appearance: none; width: 100%; display: flex; align-items: center; gap: 12px; min-height: 52px;
    padding: 8px 12px; text-align: left; color: var(--text); background: transparent; border: none; border-radius: 12px;
  }
  .opt:active { background: var(--surface-2); }
  .opt.cur { background: #fff; color: #000; }
  .opt.cur small { color: #444; }
  .opt .mark { flex: none; width: 22px; height: 22px; border: 1.5px solid var(--edge); border-radius: 6px; display: none; }
  .editing .opt .mark { display: block; }
  .opt .mark.on { background: #fff; border-color: #fff; }
  .opt.cur .mark.on { background: #000; border-color: #000; }
  #outFilter { display: none; padding: 8px 8px 10px; }
  .editing #outFilter { display: block; }
  #outFilter label { display: block; margin-bottom: 6px; font: 500 12px var(--font); color: var(--muted); }
  #outNames { width: 100%; padding: 11px 12px; color: #fff; background: #000; border: 1px solid var(--edge); border-radius: 12px; font: 500 16px var(--font); user-select: text; -webkit-user-select: text; }
  #outNames:focus { outline: none; border-color: #fff; }
  #outEdit { width: 100%; margin-top: 4px; padding: 12px; color: var(--muted); background: transparent; border: none; border-top: 1px solid var(--edge); font: 600 14px var(--font); }
  /* equalizer: moves while music plays, rests when it is paused */
  #eq {
    --eq: clamp(56px, 17vw, 84px);
    flex: none; width: var(--eq); height: var(--eq); display: flex; align-items: flex-end;
    justify-content: center; gap: calc(var(--eq) * .09); padding: calc(var(--eq) * .24) 0;
    background: #000; border: 1px solid var(--edge); border-radius: 20px;
  }
  #eq i {
    width: calc(var(--eq) * .1); height: 100%; background: #fff; border-radius: 3px;
    transform-origin: bottom; transform: scaleY(.3); opacity: .55;
  }
  #eq i:nth-child(2) { transform: scaleY(.55); }
  #eq i:nth-child(3) { transform: scaleY(.4); }
  #eq i:nth-child(4) { transform: scaleY(.22); }
  #now.idle #eq i { transform: scaleY(.12); opacity: .35; }
  #now.playing #eq i { opacity: 1; animation: eq 1.1s ease-in-out infinite; }
  #now.playing #eq i:nth-child(2) { animation-duration: .85s; animation-delay: -.3s; }
  #now.playing #eq i:nth-child(3) { animation-duration: 1.3s; animation-delay: -.7s; }
  #now.playing #eq i:nth-child(4) { animation-duration: .95s; animation-delay: -.5s; }
  @keyframes eq { 0%, 100% { transform: scaleY(.2); } 50% { transform: scaleY(1); } }

  /* shortcuts: layout picker and page buttons */
  #scBar { flex: none; display: flex; gap: var(--gap); height: 44px; }
  #profBtn {
    -webkit-appearance: none; appearance: none; flex: 1; min-width: 0; height: 44px;
    display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 14px;
    background: var(--surface); color: #fff; border: 1px solid var(--edge); border-radius: 14px;
    font: 600 15px var(--font); text-align: left;
  }
  #profName { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #profAuto { flex: none; color: var(--muted); font: 500 12px var(--font); }
  #profBtn:active { background: var(--surface-2); }
  #pageBtns { display: flex; gap: 6px; flex: none; }
  .pgBtn {
    -webkit-appearance: none; appearance: none; width: 42px; height: 44px;
    background: transparent; color: var(--muted); border: 1px solid var(--edge); border-radius: 12px;
    font: 700 15px var(--font);
  }
  .pgBtn.on { background: #fff; color: #000; border-color: #fff; }
  #slotGrid {
    display: grid; flex: 1; min-height: 0; gap: var(--gap);
    grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: repeat(3, minmax(0, 1fr));
  }

  /* trackpad */
  #pad {
    flex: 1; min-height: 0; position: relative; overflow: hidden;
    background: var(--surface); border: 1px solid var(--edge); border-radius: 22px;
    touch-action: none;
  }
  #pad.active { border-color: #fff; }
  #padHint {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    padding: 0 28px; text-align: center; pointer-events: none; transition: opacity .3s;
    color: var(--muted); font: 500 15px/1.6 var(--font);
  }
  #pad.used #padHint { opacity: 0; }
  #lat { position: absolute; right: 14px; bottom: 10px; pointer-events: none; color: var(--muted); font: 500 12px var(--font); }
  #clicks { flex: 0 0 22%; min-height: 0; display: grid; grid-template-columns: 1fr 1fr 1fr; gap: var(--gap); }
  #clicks .btn { touch-action: none; }
  #clicks .btn.on { background: #fff; color: #000; border-color: #fff; }
  #clicks .btn.on .label { color: #000; }
  .modeChip {
    -webkit-appearance: none; appearance: none; position: absolute; top: 10px; left: 10px; z-index: 3; padding: 7px 14px;
    color: var(--muted); background: #000; border: 1px solid var(--edge); border-radius: 999px; font: 600 12px var(--font);
    touch-action: manipulation;
  }
  .modeChip:active { color: #fff; border-color: #fff; }

  .modeSeg {
    position: absolute; top: 10px; left: 10px; z-index: 3; display: flex; padding: 3px;
    background: #000; border: 1px solid var(--edge); border-radius: 999px;
  }
  .modeSeg button {
    -webkit-appearance: none; appearance: none; border: none; background: transparent; color: var(--muted);
    padding: 6px 11px; border-radius: 999px; font: 600 12px var(--font); touch-action: manipulation;
  }
  .modeSeg button[aria-pressed="true"] { background: #fff; color: #000; }

  /* arrow keys, like a remote */
  #remView { display: none; }
  #pageTrackpad.rem #pad, #pageTrackpad.rem #clicks, #pageTrackpad.rem #joyView { display: none; }
  #pageTrackpad.rem #remView {
    display: flex; flex: 1; min-height: 0; gap: var(--gap); flex-direction: column; position: relative;
    background: #151515; border: 1px solid var(--edge); border-radius: 22px; padding: 52px 14px 14px;
  }
  #dpadWrap { flex: 1; min-height: 0; min-width: 0; display: flex; align-items: center; justify-content: center; container-type: size; }
  #dpad {
    width: min(100cqw, 100cqh); height: min(100cqw, 100cqh); display: grid; gap: 5%;
    grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: repeat(3, minmax(0, 1fr));
  }
  .dk {
    -webkit-appearance: none; appearance: none; display: flex; align-items: center; justify-content: center; border: none; color: #fff;
    background: #1b1b1b; border-radius: 24%; touch-action: manipulation; font: 700 clamp(15px, 4.6vw, 22px) var(--font);
    box-shadow: 6px 6px 14px #000, -5px -5px 12px #2b2b2b; transition: box-shadow .08s ease, transform .08s ease;
  }
  .dk svg { width: 44%; height: 44%; fill: none; stroke: #fff; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
  .dk.ok { border-radius: 50%; }
  .dk:active, .dk.on { box-shadow: inset 5px 5px 11px #000, inset -4px -4px 10px #2b2b2b; transform: scale(.97); }
  #remSide { flex: none; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); grid-template-rows: repeat(3, 50px); gap: var(--gap); }
  #remSide .btn { touch-action: manipulation; min-height: 0; }
  #remSide .btn.on { background: #fff; color: #000; border-color: #fff; }
  #remSide .btn.on .label { color: #000; }

  /* joystick view */
  #joyView { display: none; }
  #pageTrackpad.joy #pad, #pageTrackpad.joy #clicks { display: none; }
  #pageTrackpad.joy #joyView {
    display: grid; flex: 1; min-height: 0; gap: var(--gap);
    grid-template-columns: minmax(0, 1fr) 64px; grid-template-rows: minmax(0, 1fr) auto;
    grid-template-areas: "zone scroll" "btns btns";
  }
  #joyZone {
    grid-area: zone; position: relative; min-height: 0; overflow: hidden; touch-action: none; cursor: grab;
    background: var(--surface); border: 1px solid var(--edge); border-radius: 22px;
  }
  #joyZone.on { border-color: #fff; }
  #joyHint { position: absolute; left: 0; right: 0; top: 38%; padding: 0 28px; text-align: center; pointer-events: none; color: var(--muted); font: 500 14px/1.6 var(--font); transition: opacity .3s; }
  #joyZone.used #joyHint { opacity: 0; }
  #joySvg { position: absolute; left: 0; top: 0; display: block; width: var(--jd, 140px); height: var(--jd, 140px); transition: transform .22s ease-out; pointer-events: none; }
  #joyZone.on #joySvg { transition: none; }
  #joySvg .jbase { fill: #0a0a0a; stroke: #444; stroke-width: 1.5; }
  #joySvg .jcross { stroke: #555; stroke-width: 2.4; stroke-linecap: round; fill: none; }
  #joySvg .jtick { stroke: #555; stroke-width: 2.4; stroke-linecap: round; }
  #joySvg .jth { fill: #151515; stroke: #8a8a8a; stroke-width: 2; }
  #joySvg .jthIn { fill: #0b0b0b; stroke: #2a2a2a; stroke-width: 1; }
  #joyZone.on .jth { stroke: #fff; }
  #joyZone.on .jtick { stroke: #fff; }
  #jThumb { transition: transform .14s ease-out; }
  #joyZone.on #jThumb { transition: none; }
  #joyScroll {
    grid-area: scroll; touch-action: none; display: flex; flex-direction: column; align-items: center; padding: 12px 0;
    color: var(--muted); background: var(--surface); border: 1px solid var(--edge); border-radius: 22px;
  }
  #joyScroll.on { border-color: #fff; color: #fff; }
  #joyScroll svg { flex: none; width: 24px; height: 24px; fill: none; stroke: currentColor; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }
  #joyScroll .grip {
    flex: 1; width: 22px; margin: 8px 0; border-radius: 11px; background-color: #000; border: 1px solid var(--edge);
    background-image: repeating-linear-gradient(to bottom, #444 0 2px, transparent 2px 14px); background-position: 0 var(--sy, 0px);
  }
  #joyScroll.on .grip { background-image: repeating-linear-gradient(to bottom, #fff 0 2px, transparent 2px 14px); }
  #joyBtns { grid-area: btns; display: grid; gap: var(--gap); grid-template-columns: repeat(6, minmax(0, 1fr)); grid-template-rows: 84px 52px; }
  #joyBtns .btn { touch-action: none; }
  #joyBtns .btn.on, #joyBtns .btn[aria-pressed="true"] { background: #fff; color: #000; border-color: #fff; }
  #joyBtns .btn.on .label, #joyBtns .btn[aria-pressed="true"] .label { color: #000; }
  #jL, #jR { grid-column: span 3; }
  .jsm { grid-column: span 2; }
  @media (orientation: landscape) and (max-height: 500px) {
    #pageTrackpad.joy #joyView {
      grid-template-columns: minmax(0, 1fr) 60px minmax(0, 32%); grid-template-rows: minmax(0, 1fr);
      grid-template-areas: "zone scroll btns";
    }
    #joyBtns { grid-template-columns: 1fr 1fr; grid-template-rows: 1.6fr 1.3fr 1fr 1fr; min-height: 0; }
    #jL, #jR { grid-column: span 2; }
    .jsm { grid-column: span 1; }
    #jKb { grid-column: span 2; }
    #pageTrackpad.rem #remView { flex-direction: row; padding: 20px 14px 14px; }
    #remSide { grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: repeat(4, minmax(0, 1fr)); width: 52%; height: auto; }
    .modeSeg { top: 8px; left: 8px; }
    #pageTrackpad.rem .modeSeg { position: absolute; }
    #joyBtns .btn { min-height: 0; }
    #pageTrackpad { flex-direction: row; }
    #clicks { flex: 0 0 24%; grid-template-columns: 1fr; grid-template-rows: 1fr 1fr 1fr; }
  }

  /* more: quick launch and power */
  #pageMore, #pageSettings { display: block; overflow-y: auto; -webkit-overflow-scrolling: touch; }
  #tiles, #power { display: grid; gap: var(--gap); grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .tile, .pbtn {
    -webkit-appearance: none; appearance: none; min-width: 0; min-height: 60px; padding: 8px 12px;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    background: var(--surface); color: #fff; border: 1px solid var(--edge); border-radius: 14px;
    font: 600 15px var(--font); touch-action: manipulation;
  }
  .tile { justify-content: flex-start; }
  .tile svg { flex: none; width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .tile span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tile.empty { background: transparent; border-style: dashed; color: var(--muted); }
  .tile:active, .pbtn:active { background: #fff; color: #000; border-color: #fff; }
  .pbtn.wide { grid-column: span 2; }
  @media (orientation: landscape) and (max-height: 500px) {
    #tiles { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    #power { grid-template-columns: repeat(5, minmax(0, 1fr)); }
    .pbtn.wide { grid-column: auto; }
  }

  /* settings */
  #pageSettings { display: block; overflow-y: auto; -webkit-overflow-scrolling: touch; }
  .set { max-width: 560px; margin: 0 auto; padding-bottom: 14px; }
  .setHead { margin: 16px 4px 8px; font: 700 15px var(--font); }
  .set .setHead:first-child { margin-top: 4px; }
  .setRow {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    min-height: 56px; padding: 8px 14px; margin-bottom: 6px;
    background: var(--surface); border: 1px solid var(--edge); border-radius: 14px;
  }
  .setRow .name { font: 600 15px var(--font); }
  .setRow .sub { margin-top: 2px; color: var(--muted); font: 400 12px/1.35 var(--font); }
  .inappOnly { display: none !important; }
  .inapp .inappOnly { display: flex !important; }
  .inapp .fsOnly { display: none !important; }
  .presetIn { display: flex; gap: 6px; flex: none; }
  .presetIn input { width: 54px; padding: 9px 4px; text-align: center; color: #fff; background: #000; border: 1px solid var(--edge); border-radius: 10px; font: 600 16px var(--font); -webkit-user-select: text; user-select: text; }
  .presetIn input:focus { outline: none; border-color: #fff; }
  .setNote { margin: 2px 6px 10px; color: var(--muted); font: 400 12px/1.5 var(--font); }
  .sw {
    -webkit-appearance: none; appearance: none; position: relative; flex: none;
    width: 52px; height: 30px; border-radius: 15px; background: #000; border: 1px solid var(--edge);
    transition: background .15s, border-color .15s;
  }
  .sw::after {
    content: ''; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px; border-radius: 50%;
    background: var(--muted); transition: transform .15s, background .15s;
  }
  .sw[aria-checked="true"] { background: #fff; border-color: #fff; }
  .sw[aria-checked="true"]::after { transform: translateX(22px); background: #000; }
  .sw:focus-visible, .sbtn:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .sbtn {
    -webkit-appearance: none; appearance: none; flex: none; height: 38px; padding: 0 14px;
    background: transparent; color: #fff; border: 1px solid var(--edge); border-radius: 10px;
    font: 600 14px var(--font);
  }
  .sbtn:active { background: #fff; color: #000; }
  .sbtn:disabled { color: var(--muted); }
  #sens { width: 150px; accent-color: #fff; touch-action: none; }

  /* bottom sheets */
  .backdrop {
    position: fixed; inset: 0; z-index: 20; background: rgba(0, 0, 0, .75);
    opacity: 0; pointer-events: none; transition: opacity .2s ease;
  }
  .backdrop.open { opacity: 1; pointer-events: auto; }
  .sheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 30;
    background: var(--surface); border-top: 1px solid #fff; border-radius: 22px 22px 0 0;
    padding: 18px 16px calc(16px + env(safe-area-inset-bottom));
    transform: translateY(105%); transition: transform .22s ease;
    max-height: 92dvh; overflow-y: auto;
  }
  .sheet.open { transform: translateY(0); }
  .sheet h2 { font-size: 18px; font-weight: 700; margin-bottom: 14px; }
  .field { display: block; margin-bottom: 12px; }
  .field span { display: block; font-size: 13px; color: var(--muted); margin-bottom: 5px; font-weight: 600; }
  .field input, .field select {
    -webkit-appearance: none; appearance: none; width: 100%; height: 46px; padding: 0 12px;
    background: #000; color: #fff; border: 1px solid var(--edge); border-radius: 12px; font: 500 16px var(--font);
  }
  .field input:focus, .field select:focus { outline: none; border-color: #fff; }
  .field small { display: block; margin-top: 5px; font-size: 12px; color: var(--muted); line-height: 1.35; }
  .err { min-height: 18px; font-size: 13px; font-weight: 600; margin-bottom: 8px; }
  .actions { display: flex; gap: 8px; }
  .abtn {
    -webkit-appearance: none; appearance: none; flex: 1; height: 48px;
    background: transparent; color: #fff; border: 1px solid var(--edge); border-radius: 12px; font: 700 15px var(--font);
  }
  .abtn.solid { background: #fff; color: #000; border-color: #fff; }
  .abtn:active { transform: scale(.97); }
  .abtn:disabled { color: var(--muted); }
  .pfRow { display: flex; gap: 8px; margin-bottom: 8px; }
  .pfPick {
    -webkit-appearance: none; appearance: none; flex: 1; min-width: 0; min-height: 52px; padding: 8px 14px;
    background: transparent; color: #fff; border: 1px solid var(--edge); border-radius: 12px;
    font: 600 15px var(--font); text-align: left;
  }
  .pfPick.on { border-color: #fff; background: var(--surface-2); }
  .pfPick small { display: block; margin-top: 2px; color: var(--muted); font: 400 12px var(--font); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pfRow .abtn { flex: 0 0 72px; height: auto; }
  .pfActions { margin-top: 12px; }

  /* typing bar: sits just above the phone keyboard */
  #typeBar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 40; display: none;
    background: var(--surface); border-top: 1px solid #fff; padding: 8px;
  }
  #typeBar.open { display: block; }
  .tbRow, .tbKeys { display: flex; gap: 6px; }
  .tbRow { margin-bottom: 6px; }
  #typeInput {
    flex: 1; min-width: 0; height: 44px; padding: 0 12px; pointer-events: none;
    background: #000; color: #fff; border: 1px solid var(--edge); border-radius: 12px; font: 500 16px var(--font);
  }
  .tbBtn {
    -webkit-appearance: none; appearance: none; flex: 1; min-width: 0; height: 42px;
    background: var(--surface-2); color: #fff; border: 1px solid var(--edge); border-radius: 10px; font: 600 13px var(--font);
  }
  .tbBtn.solid { flex: 0 0 80px; height: 44px; background: #fff; color: #000; border-color: #fff; font-size: 15px; }
  .tbBtn:active { background: #fff; color: #000; }
  #kbChip {
    -webkit-appearance: none; appearance: none; display: none;
    position: fixed; left: 50%; transform: translateX(-50%); z-index: 45;
    bottom: calc(124px + env(safe-area-inset-bottom));
    height: 46px; padding: 0 22px; border: none; border-radius: 23px; white-space: nowrap;
    background: #fff; color: #000; box-shadow: 0 4px 18px rgba(0, 0, 0, .7); font: 700 15px var(--font);
  }
  #kbChip.show { display: block; }

  @media (max-height: 460px) {
    .btn .label { display: none; }
    .btn.custom .label { display: block; }
    /* sideways phone: now playing | knob | a column of controls */
    #pageControls {
      display: grid; gap: var(--gap);
      grid-template-columns: minmax(0, 27%) minmax(0, 1fr) minmax(0, 76px) minmax(0, 76px);
      grid-template-rows: repeat(5, minmax(0, 1fr));
      grid-template-areas: "now knob prev next" "now knob mute mute" "now knob p0 p1" "now knob p2 p2" "now knob outs outs";
    }
    #pageControls .ctl, #pageControls .deckKnob, #pageControls #presets { display: contents; }
    #now { grid-area: now; flex: none; flex-direction: column; align-items: flex-start; justify-content: center; gap: 12px; padding: 12px 14px; border-radius: 18px; }
    #eq { --eq: 48px; border-radius: 12px; }
    #nowTitle { font-size: clamp(17px, 3vw, 24px); -webkit-line-clamp: 3; line-clamp: 3; }
    #nowArtist { margin-top: 2px; font-size: 14px; }
    .nowText { flex: none; width: 100%; min-width: 0; }
    #nowTitle, #nowArtist { overflow-wrap: anywhere; }
    #knob { grid-area: knob; width: 100%; height: 100%; min-height: 0; }
    .deckKnob .side { position: static; width: auto; height: auto; min-height: 0; flex: none; border-radius: 14px; }
    .deckKnob .side:first-child { grid-area: prev; }
    .deckKnob .side:last-child { grid-area: next; }
    #muteChip { grid-area: mute; }
    .pre[data-i="0"] { grid-area: p0; }
    .pre[data-i="1"] { grid-area: p1; }
    .pre[data-i="2"] { grid-area: p2; }
    #outs { grid-area: outs; min-width: 0; }
    .chip { min-height: 0; padding: 0 4px; }
    #outBtn { height: 100%; min-height: 0; padding: 4px 10px; gap: 8px; }
    #outBtn .chev { display: none; }
    #outBtn svg:first-child { width: 18px; height: 18px; }
    .outText b { font-size: 14px; }
    #outMenu { left: auto; right: 0; width: min(320px, 62vw); max-height: 70vh; }
    .outText small { display: none; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } #now.playing #eq i { animation: none; } }
</style>
</head>
<body>
<div id="status"></div>

<video id="awakeVideo" muted loop playsinline preload="auto" src="/awake.mp4" style="position:fixed;left:0;top:0;width:2px;height:2px;opacity:.01;pointer-events:none"></video>

<div class="deck">
 <div class="pager" id="pager">

 <section class="page" id="pageControls">
  <button id="now" class="hidden" data-act="playpause" aria-label="Now playing. Tap to play or pause">
    <div id="eq" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
    <div class="nowText"><div id="nowTitle">Nothing playing</div><div id="nowArtist"></div></div>
  </button>
  <div class="ctl">
  <div class="deckKnob">
    <button class="btn side" data-act="prev" aria-label="Previous track">
      <svg viewBox="0 0 24 24"><polygon class="f" points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5"/></svg>
    </button>
    <div id="knob" role="slider" tabindex="0" aria-label="Volume. Turn to change, tap the middle to play or pause" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50">
      <svg viewBox="0 0 200 200" aria-hidden="true">
        <g id="ticks"></g>
        <text class="kl" x="48" y="190" text-anchor="middle">MIN</text>
        <text class="kl" x="152" y="190" text-anchor="middle">MAX</text>
        <circle class="cap" cx="100" cy="100" r="68"/>
        <circle class="capIn" cx="100" cy="100" r="62"/>
        <g id="dot"><circle cx="100" cy="52" r="3.6"/></g>
        <g id="kIcon" transform="translate(100 98)">
          <polygon class="f" id="kPlay" points="-7 -10 9 0 -7 10"/>
          <g id="kPause"><rect class="f" x="-8" y="-10" width="5.5" height="20" rx="1.5"/><rect class="f" x="2.5" y="-10" width="5.5" height="20" rx="1.5"/></g>
        </g>
        <text id="kNum" x="100" y="132" text-anchor="middle">50</text>
      </svg>
    </div>
    <button class="btn side" data-act="next" aria-label="Next track">
      <svg viewBox="0 0 24 24"><polygon class="f" points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></svg>
    </button>
  </div>
  <div id="presets">
    <button class="chip" id="muteChip" aria-pressed="false">Mute</button>
    <button class="chip pre hidden" data-i="0"></button>
    <button class="chip pre hidden" data-i="1"></button>
    <button class="chip pre hidden" data-i="2"></button>
  </div>
  <div id="outs" class="hidden">
    <div id="outMenu" hidden>
      <div id="outFilter"><label for="outNames">Only show names containing</label><input id="outNames" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="headphone, ROXX"></div>
      <div id="outItems" role="listbox" aria-label="Sound output"></div>
      <button id="outEdit">Choose which devices to show</button>
    </div>
    <button id="outBtn" aria-haspopup="listbox" aria-expanded="false">
      <svg viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
      <div class="outText"><small>Sound output</small><b id="outName">Choose</b></div>
      <svg class="chev" viewBox="0 0 24 24"><polyline points="6 15 12 9 18 15"/></svg>
    </button>
  </div>
  </div>
 </section>

 <section class="page" id="pageShortcuts">
  <div id="scBar">
    <button id="profBtn"><span id="profName">Default</span><span id="profAuto">auto</span></button>
    <div id="pageBtns"></div>
  </div>
  <div id="slotGrid"></div>
 </section>

 <section class="page" id="pageTrackpad">
  <div id="pad">
    <div id="padHint">Drag to move the cursor. Tap to click. Two fingers: tap to right-click, drag to scroll. Three fingers: swipe.</div>
    <div id="lat">Connecting</div>
  </div>
  <div id="clicks">
    <button class="btn custom" id="btnL"><span class="label">Left click</span></button>
    <button class="btn custom" id="btnKb">
      <svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>
      <span class="label">Keyboard</span>
    </button>
    <button class="btn custom" id="btnR"><span class="label">Right click</span></button>
  </div>

  <div id="joyView">
    <div id="joyZone" aria-label="Joystick. Push to move the cursor, further is faster">
      <svg id="joySvg" viewBox="0 0 200 200" aria-hidden="true">
        <g id="jTicks"></g>
        <circle class="jbase" cx="100" cy="100" r="80"/>
        <path class="jcross" d="M100 30v13M100 157v13M30 100h13M157 100h13"/>
        <g id="jThumb"><circle class="jth" cx="100" cy="100" r="34"/><circle class="jthIn" cx="100" cy="100" r="25"/></g>
      </svg>
      <div id="joyHint">Touch anywhere here and push. Further is faster.</div>
    </div>
    <div id="joyScroll" aria-label="Scroll. Drag up or down">
      <svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="6 14 12 8 18 14"/></svg>
      <div class="grip"></div>
      <svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="6 10 12 16 18 10"/></svg>
    </div>
    <div id="joyBtns">
      <button class="btn custom" id="jL"><span class="label">Left click</span></button>
      <button class="btn custom" id="jR"><span class="label">Right click</span></button>
      <button class="btn custom jsm" id="jHold" aria-pressed="false"><span class="label">Hold</span></button>
      <button class="btn custom jsm" id="jDbl"><span class="label">Double</span></button>
      <button class="btn custom jsm" id="jKb"><span class="label">Keyboard</span></button>
    </div>
  </div>

  <div id="remView">
    <div id="dpadWrap">
      <div id="dpad" role="group" aria-label="Arrow keys">
        <button class="dk" data-k="up" data-rep="1" style="grid-area:1/2" aria-label="Up"><svg viewBox="0 0 24 24"><polyline points="5 15 12 8 19 15"/></svg></button>
        <button class="dk" data-k="left" data-rep="1" style="grid-area:2/1" aria-label="Left"><svg viewBox="0 0 24 24"><polyline points="15 5 8 12 15 19"/></svg></button>
        <button class="dk ok" data-k="enter" style="grid-area:2/2" aria-label="Enter">OK</button>
        <button class="dk" data-k="right" data-rep="1" style="grid-area:2/3" aria-label="Right"><svg viewBox="0 0 24 24"><polyline points="9 5 16 12 9 19"/></svg></button>
        <button class="dk" data-k="down" data-rep="1" style="grid-area:3/2" aria-label="Down"><svg viewBox="0 0 24 24"><polyline points="5 9 12 16 19 9"/></svg></button>
      </div>
    </div>
    <div id="remSide">
      <button class="btn custom" data-k="j" data-rep="1"><span class="label">-10s</span></button>
      <button class="btn custom" data-k="k"><span class="label">Play</span></button>
      <button class="btn custom" data-k="l" data-rep="1"><span class="label">+10s</span></button>
      <button class="btn custom" data-k="m"><span class="label">Mute</span></button>
      <button class="btn custom" data-h="slow"><span class="label">Slower</span></button>
      <button class="btn custom" data-h="fast"><span class="label">Faster</span></button>
      <button class="btn custom" data-k="c"><span class="label">CC</span></button>
      <button class="btn custom" data-k="f"><span class="label">Full</span></button>
      <button class="btn custom" data-k="esc"><span class="label">Esc</span></button>
      <button class="btn custom" data-k="tab"><span class="label">Tab</span></button>
      <button class="btn custom" data-k="backspace" data-rep="1"><span class="label">Back</span></button>
      <button class="btn custom" data-k="space"><span class="label">Space</span></button>
    </div>
  </div>
 </section>

 <section class="page" id="pageMore">
  <div class="set">
    <div class="setHead">Quick launch</div>
    <div id="tiles"></div>
    <div class="setNote">Tap to open on the laptop. Hold a tile to edit it.</div>
    <div class="setHead">Laptop</div>
    <div id="power">
      <button class="pbtn" data-x="lock">Lock</button>
      <button class="pbtn" data-x="screen">Screen off</button>
      <button class="pbtn" data-x="sleep">Sleep</button>
      <button class="pbtn" data-x="restart">Restart</button>
      <button class="pbtn wide" data-x="shutdown">Shut down</button>
    </div>
    <div class="setNote">Sleep and Shut down end the connection. Wake the laptop with its power button. Screen off wakes when you move the mouse.</div>
  </div>
 </section>

 <section class="page" id="pageSettings">
  <div class="set">
    <div class="setHead">Trackpad</div>
    <div class="setRow">
      <div><div class="name">Cursor speed</div><div class="sub" id="sensVal">1.0x</div></div>
      <input id="sens" type="range" min="0.5" max="2.5" step="0.1" value="1" aria-label="Cursor speed">
    </div>
    <div class="setRow">
      <div><div class="name">Joystick size</div><div class="sub" id="joySizeVal">Medium</div></div>
      <input id="joySize" type="range" min="90" max="230" step="10" value="140" aria-label="Joystick size">
    </div>
    <div class="setRow">
      <div><div class="name">Natural scrolling</div><div class="sub">Content follows your fingers</div></div>
      <button class="sw" id="swNatural" role="switch" aria-checked="true" aria-label="Natural scrolling"></button>
    </div>
    <div class="setNote">Two fingers: drag to scroll, tap to right-click. Three fingers: swipe up for Task View, down to show the desktop, left or right to switch windows.</div>

    <div class="setHead">Keyboard</div>
    <div class="setRow">
      <div><div class="name">Open in text boxes</div><div class="sub">Shows the phone keyboard when you click a text box on the laptop</div></div>
      <button class="sw" id="swAutoKb" role="switch" aria-checked="true" aria-label="Open keyboard in text boxes"></button>
    </div>

    <div class="setHead">Shortcuts</div>
    <div class="setRow">
      <div><div class="name">Layouts</div><div class="sub" id="layoutSub">Default</div></div>
      <button class="sbtn" id="manageLayouts">Manage</button>
    </div>
    <div class="setNote">A layout is a set of shortcut pages. It appears by itself when its app is in front on the laptop.</div>

    <div class="setHead">Display</div>
    <div class="setRow inappOnly">
      <div><div class="name">Laptop address</div><div class="sub" id="addrVal"></div></div>
      <button class="sbtn" id="chgAddr">Change</button>
    </div>
    <div class="setRow inappOnly">
      <div><div class="name">Phone volume buttons</div><div class="sub">Change the laptop volume while the app is open</div></div>
      <button class="sw" id="swHwVol" role="switch" aria-checked="true" aria-label="Phone volume buttons control the laptop"></button>
    </div>
    <div class="setRow inappOnly">
      <div><div class="name">Lock-screen controls</div><div class="sub">Play, pause and skip from the phone's lock screen and notifications</div></div>
      <button class="sw" id="swLockMedia" role="switch" aria-checked="true" aria-label="Lock-screen media controls"></button>
    </div>
    <div class="setRow fsOnly">
      <div><div class="name">Full screen</div><div class="sub">Hides the browser bars</div></div>
      <button class="sbtn" id="fsBtn">Enter</button>
    </div>
    <div class="setRow fsOnly">
      <div><div class="name">Full screen on first tap</div><div class="sub">Re-enters each time the page loads</div></div>
      <button class="sw" id="swFsAuto" role="switch" aria-checked="true" aria-label="Full screen on first tap"></button>
    </div>
    <div class="setRow">
      <div><div class="name">Keep screen on</div><div class="sub" id="awakeNote">Stops the phone from sleeping while this page is open</div></div>
      <button class="sw" id="swAwake" role="switch" aria-checked="true" aria-label="Keep screen on"></button>
    </div>
    <div class="setNote" id="appNote"></div>
    <div class="setNote" id="installHow"></div>

    <div class="setHead">Volume presets</div>
    <div class="setRow">
      <div><div class="name">Three buttons under the knob</div><div class="sub">Volume from 0 to 100. 100 shows as Max</div></div>
      <div class="presetIn">
        <input id="pre0" type="number" inputmode="numeric" min="0" max="100" aria-label="Preset 1">
        <input id="pre1" type="number" inputmode="numeric" min="0" max="100" aria-label="Preset 2">
        <input id="pre2" type="number" inputmode="numeric" min="0" max="100" aria-label="Preset 3">
      </div>
    </div>

    <div class="setHead">Security</div>
    <div class="setRow">
      <div><div class="name">Edit PIN</div><div class="sub" id="pinSub">Saved on this phone</div></div>
      <button class="sbtn" id="forgetPin">Forget</button>
    </div>
  </div>
 </section>

 </div>

 <nav class="tabs" role="tablist">
  <button class="tab active" role="tab" aria-selected="true">Controls</button>
  <button class="tab" role="tab" aria-selected="false">Shortcuts</button>
  <button class="tab" role="tab" aria-selected="false">Trackpad</button>
  <button class="tab" role="tab" aria-selected="false">More</button>
  <button class="tab" role="tab" aria-selected="false">Settings</button>
 </nav>
</div>

<button id="kbChip">Text box active. Tap to type</button>
<div id="typeBar">
  <div class="tbRow">
    <input id="typeInput" type="text" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="send" aria-label="Type to the laptop" placeholder="Typing to laptop">
    <button class="tbBtn solid" id="tbDone">Done</button>
  </div>
  <div class="tbKeys">
    <button class="tbBtn" data-k="tab">Tab</button>
    <button class="tbBtn" data-k="esc">Esc</button>
    <button class="tbBtn" data-k="left">Left</button>
    <button class="tbBtn" data-k="up">Up</button>
    <button class="tbBtn" data-k="down">Down</button>
    <button class="tbBtn" data-k="right">Right</button>
    <button class="tbBtn" data-k="enter">Enter</button>
  </div>
</div>

<div class="backdrop" id="edBackdrop"></div>

<!-- shortcut button editor -->
<div class="sheet" id="edSheet">
  <h2 id="edTitle">New button</h2>
  <label class="field"><span>Name</span>
    <input id="edLabel" type="text" maxlength="14" placeholder="Short name" autocomplete="off">
  </label>
  <label class="field"><span>What it does</span>
    <select id="edType">
      <option value="hotkey">Press a keyboard shortcut</option>
      <option value="url">Open a website</option>
      <option value="command">Run a program or command</option>
    </select>
  </label>
  <label class="field"><span id="edValueName">Shortcut</span>
    <input id="edValue" type="text" autocomplete="off" autocapitalize="off" spellcheck="false">
    <small id="edHelp"></small>
  </label>
  <label class="field" id="edPinWrap"><span>Edit PIN</span>
    <input id="edPin" type="password" inputmode="numeric" autocomplete="off" placeholder="Shown in the laptop console">
  </label>
  <div class="err" id="edError"></div>
  <div class="actions">
    <button class="abtn" id="edCancel">Cancel</button>
    <button class="abtn hidden" id="edDelete">Delete</button>
    <button class="abtn solid" id="edSave">Save</button>
  </div>
</div>

<!-- quick launch tile editor -->
<div class="sheet" id="lnSheet">
  <h2 id="lnTitle">New tile</h2>
  <label class="field"><span>Name</span>
    <input id="lnLabel" type="text" maxlength="14" placeholder="Short name" autocomplete="off">
  </label>
  <label class="field"><span>Website or program</span>
    <input id="lnValue" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="youtube.com or notepad">
    <small>A website like netflix.com, or a program, folder or file on the laptop like chrome, code or C:\Users\you\Downloads.</small>
  </label>
  <label class="field hidden" id="lnPinWrap"><span>Edit PIN</span>
    <input id="lnPin" type="password" inputmode="numeric" autocomplete="off" placeholder="Shown in the laptop console">
  </label>
  <div class="err" id="lnError"></div>
  <div class="actions">
    <button class="abtn" id="lnCancel">Cancel</button>
    <button class="abtn hidden" id="lnDelete">Delete</button>
    <button class="abtn solid" id="lnSave">Save</button>
  </div>
</div>

<!-- power confirm -->
<div class="sheet" id="pwSheet">
  <h2 id="pwTitle">Shut down the laptop?</h2>
  <div class="setNote" id="pwText" style="margin:0 0 14px"></div>
  <div class="actions">
    <button class="abtn" id="pwCancel">Cancel</button>
    <button class="abtn solid" id="pwGo">Shut down</button>
  </div>
</div>

<!-- layouts: pick, add, edit -->
<div class="sheet" id="pfSheet">
  <div id="pfList">
    <h2>Layouts</h2>
    <div id="pfRows"></div>
    <div class="actions pfActions">
      <button class="abtn" id="pfClose">Close</button>
      <button class="abtn solid" id="pfNew">New layout</button>
    </div>
  </div>
  <div id="pfEdit" class="hidden">
    <h2 id="pfTitle">New layout</h2>
    <label class="field"><span>Name</span>
      <input id="pfName" type="text" maxlength="16" placeholder="For example VS Code" autocomplete="off">
    </label>
    <label class="field"><span>Apps that switch to it</span>
      <input id="pfApps" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="code.exe, chrome.exe">
      <small>Program file names, separated by commas. Open the app on the laptop, then use the button below.</small>
    </label>
    <button class="abtn" id="pfUse" style="width:100%;margin-bottom:12px">Active app unknown</button>
    <div class="err" id="pfError"></div>
    <div class="actions">
      <button class="abtn" id="pfBack">Back</button>
      <button class="abtn hidden" id="pfDelete">Delete</button>
      <button class="abtn solid" id="pfSave">Save</button>
    </div>
  </div>
</div>

<script>
  var $ = function (id) { return document.getElementById(id); };
  function store(key, def) { try { var v = localStorage.getItem(key); return v === null ? def : v; } catch (err) { return def; } }
  function save(key, val) { try { localStorage.setItem(key, String(val)); } catch (err) {} }
  function forget(key) { try { localStorage.removeItem(key); } catch (err) {} }
  function buzz(ms) { if (navigator.vibrate) navigator.vibrate(ms); }

  var online = true;
  function setOnline(v) {
    if (v !== online) { online = v; document.body.classList.toggle('offline', !v); }
  }
  function api(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(function (r) {
      setOnline(true);
      return r.json().then(function (j) { return {ok: r.ok, status: r.status, data: j}; });
    }).catch(function () { setOnline(false); return {ok: false, status: 0, data: {error: 'Cannot reach the laptop.'}}; });
  }

  /* ---------- settings kept on this phone ---------- */
  var S = {
    sens: parseFloat(store('deckSens', '1')) || 1,
    natural: store('deckNatural', '1') === '1',
    autoKb: store('deckAutoKb', '1') !== '0',
    fsAuto: store('deckFsAuto', '1') === '1',
    awake: store('deckAwake', '1') === '1',
    hwVol: store('deckHwVol', '1') !== '0',
    lockMedia: store('deckLockMedia', '1') !== '0'
  };
  var SKEY = {natural: 'deckNatural', autoKb: 'deckAutoKb', fsAuto: 'deckFsAuto', awake: 'deckAwake', hwVol: 'deckHwVol', lockMedia: 'deckLockMedia'};
  function bindSwitch(id, key, onChange) {
    var el = $(id);
    function paint() { el.setAttribute('aria-checked', S[key] ? 'true' : 'false'); }
    paint();
    el.addEventListener('click', function () {
      S[key] = !S[key]; save(SKEY[key], S[key] ? '1' : '0'); paint();
      if (onChange) onChange(S[key]);
    });
  }

  var pin = store('deckPin', '');

  /* ---------- pages and tabs ---------- */
  var pager = $('pager'), tabs = document.querySelectorAll('.tab'), activePage = 0;
  pager.addEventListener('scroll', function () {
    activePage = Math.round(pager.scrollLeft / pager.clientWidth);
    if (pager.clientWidth) save('deckPage', activePage);
    tabs.forEach(function (t, i) {
      t.classList.toggle('active', i === activePage);
      t.setAttribute('aria-selected', i === activePage ? 'true' : 'false');
    });
  });
  tabs.forEach(function (t, i) {
    t.addEventListener('click', function () { pager.scrollTo({left: i * pager.clientWidth, behavior: 'smooth'}); });
  });
  window.addEventListener('resize', function () { pager.scrollLeft = activePage * pager.clientWidth; });
  // open on the page that was open last time, so a remote stays a remote
  (function () {
    var want = parseInt(store('deckPage', '0'), 10) || 0;
    if (want > 0 && want < tabs.length) requestAnimationFrame(function () { pager.scrollLeft = want * pager.clientWidth; });
  })();

  /* ---------- media and volume buttons ---------- */
  document.querySelectorAll('[data-act]').forEach(function (el) {
    var name = el.getAttribute('data-act');
    var hold = el.hasAttribute('data-hold');
    var armed = null, fired = false, delay = null, timer = null;
    function stop() { clearTimeout(armed); clearTimeout(delay); clearInterval(timer); armed = delay = timer = null; }
    function fire() { fired = true; api('/action', {name: name}); buzz(15); }
    if (hold) {
      // The first step waits a moment so a page swipe that starts on this button
      // (which cancels the touch) does not change the volume.
      el.addEventListener('pointerdown', function () {
        fired = false;
        armed = setTimeout(function () {
          armed = null; fire();
          delay = setTimeout(function () {
            timer = setInterval(function () { api('/action', {name: name}); }, 100);
          }, 350);
        }, 90);
      });
      el.addEventListener('pointerup', function () { if (armed !== null && !fired) fire(); stop(); });
      ['pointercancel', 'pointerleave'].forEach(function (ev) { el.addEventListener(ev, stop); });
      el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    } else {
      el.addEventListener('click', function () { api('/action', {name: name}); buzz(15); });
    }
  });

  /* ---------- now playing ---------- */
  function onNow(info) {
    var box = $('now');
    window.lastNow = info;
    if (window.deckTellMedia) window.deckTellMedia();
    box.classList.remove('hidden');
    box.classList.toggle('playing', !!(info.on && info.p));
    K.playing = !!(info.on && info.p); renderKnob();
    box.classList.toggle('idle', !info.on);
    $('nowTitle').textContent = info.on && info.t ? info.t : 'Nothing playing';
    var line = [];
    if (info.on && info.a) line.push(info.a);
    if (info.on && !info.p) line.push('Paused');
    if (!info.on && info.note) line.push(info.note);
    $('nowArtist').textContent = line.join('  -  ');
  }

  /* ---------- volume knob ---------- */
  var K = {level: 50, muted: false, known: false, drag: null, timer: 0, userAt: 0, acc: 0, playing: false, lastTick: 0};
  K.userAt = -1e9;
  var PRE = [60, 80, 100];
  try { var sp = JSON.parse(store('deckPresets', '')); if (sp && sp.length === 3) PRE = sp.map(function (v) { return Math.max(0, Math.min(100, v | 0)); }); } catch (err) {}
  var NS = 'http://www.w3.org/2000/svg', TICKS = 28, tickEls = [];
  (function () {
    var g = $('ticks');
    for (var i = 0; i < TICKS; i++) {
      var a = (-135 + 270 * i / (TICKS - 1)) * Math.PI / 180, l = document.createElementNS(NS, 'line');
      l.setAttribute('x1', (100 + 84 * Math.sin(a)).toFixed(2)); l.setAttribute('y1', (100 - 84 * Math.cos(a)).toFixed(2));
      l.setAttribute('x2', (100 + 94 * Math.sin(a)).toFixed(2)); l.setAttribute('y2', (100 - 94 * Math.cos(a)).toFixed(2));
      l.setAttribute('class', 'tick'); g.appendChild(l); tickEls.push(l);
    }
  })();
  function renderKnob() {
    var lv = Math.round(K.level), f = K.level / 100;
    tickEls.forEach(function (t, i) { t.classList.toggle('lit', K.level > 0 && i / (TICKS - 1) <= f + 0.001); });
    $('dot').setAttribute('transform', 'rotate(' + (-135 + 270 * f).toFixed(1) + ' 100 100)');
    $('kNum').textContent = K.muted ? 'Muted' : lv;
    $('knob').classList.toggle('muted', K.muted);
    $('knob').setAttribute('aria-valuenow', lv);
    $('kPlay').style.display = K.playing ? 'none' : ''; $('kPause').style.display = K.playing ? '' : 'none';
    $('muteChip').setAttribute('aria-pressed', K.muted ? 'true' : 'false');
    document.querySelectorAll('.pre').forEach(function (c) {
      var v = PRE[+c.getAttribute('data-i')];
      c.textContent = v >= 100 ? 'Max' : v;
      c.classList.toggle('hidden', !K.known);
      c.classList.toggle('on', !K.muted && Math.round(K.level) === v);
    });
  }
  function pushVolume() {
    if (K.known) {
      if (K.timer) return;
      K.timer = setTimeout(function () { K.timer = 0; if (wsOpen && authed) ws.send('v ' + Math.round(K.level)); }, 45);
    }
  }
  function setLevel(v, send) {
    var old = K.level;
    K.level = Math.max(0, Math.min(100, v)); K.userAt = performance.now();
    if (K.muted && K.level > 0) K.muted = false;
    if (Math.round(K.level / 5) !== Math.round(old / 5)) buzz(6);
    renderKnob();
    if (send) {
      if (K.known) pushVolume();
      else {  // laptop cannot report its volume: send volume steps instead (each is about 2 percent)
        K.acc += K.level - old;
        while (Math.abs(K.acc) >= 2) { api('/action', {name: K.acc > 0 ? 'volup' : 'voldown'}); K.acc -= K.acc > 0 ? 2 : -2; }
      }
    }
  }
  function onVolume(level, muted) {
    K.known = true;
    var busy = (K.drag && K.drag.moved) || performance.now() - K.userAt < 900;
    K.muted = !!muted;
    if (!busy) K.level = level;
    renderKnob();
  }
  var knob = $('knob');
  knob.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  knob.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    try { knob.setPointerCapture(e.pointerId); } catch (err) {}
    var r = knob.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2, size = Math.min(r.width, r.height);
    K.drag = {id: e.pointerId, cx: cx, cy: cy, sx: e.clientX, sy: e.clientY, a: Math.atan2(e.clientX - cx, -(e.clientY - cy)),
              moved: false, t0: performance.now(), inCap: Math.hypot(e.clientX - cx, e.clientY - cy) < size * 0.34};
  });
  knob.addEventListener('pointermove', function (e) {
    var d = K.drag;
    if (!d || e.pointerId !== d.id) return;
    if (!d.moved) {
      if (Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 10) return;
      d.moved = true; knob.classList.add('turning');
    }
    var a = Math.atan2(e.clientX - d.cx, -(e.clientY - d.cy)), da = a - d.a;
    if (da > Math.PI) da -= 2 * Math.PI; else if (da < -Math.PI) da += 2 * Math.PI;
    d.a = a;
    setLevel(K.level + da * 180 / Math.PI * 100 / 270, true);
  });
  function knobEnd(e) {
    var d = K.drag;
    if (!d || e.pointerId !== d.id) return;
    K.drag = null; knob.classList.remove('turning');
    if (d.moved) {
      if (K.timer) { clearTimeout(K.timer); K.timer = 0; }
      if (K.known && wsOpen && authed) ws.send('v ' + Math.round(K.level));
    } else if (e.type === 'pointerup' && d.inCap && performance.now() - d.t0 < 500) {
      api('/action', {name: 'playpause'}); buzz(15);
    }
  }
  knob.addEventListener('pointerup', knobEnd);
  knob.addEventListener('pointercancel', knobEnd);
  knob.addEventListener('keydown', function (e) {
    var s = e.key === 'ArrowUp' || e.key === 'ArrowRight' ? 2 : e.key === 'ArrowDown' || e.key === 'ArrowLeft' ? -2 : 0;
    if (s) { e.preventDefault(); setLevel(K.level + s, true); }
  });
  $('muteChip').addEventListener('click', function () { K.muted = !K.muted; renderKnob(); api('/action', {name: 'mute'}); buzz(10); });
  document.querySelectorAll('.pre').forEach(function (c) {
    var i = +c.getAttribute('data-i');
    c.addEventListener('click', function () { setLevel(PRE[i], true); if (K.known && wsOpen && authed) ws.send('v ' + PRE[i]); });
  });
  [0, 1, 2].forEach(function (i) {
    var inp = $('pre' + i);
    inp.value = PRE[i];
    inp.addEventListener('change', function () {
      var v = parseInt(inp.value, 10);
      if (isNaN(v)) v = PRE[i];
      PRE[i] = Math.max(0, Math.min(100, v)); inp.value = PRE[i];
      save('deckPresets', JSON.stringify(PRE)); renderKnob();
    });
  });
  renderKnob();

  /* ---------- sound output ---------- */
  var outList = [], outEditing = false, outHidden = [];
  try { outHidden = JSON.parse(store('deckOutHidden', '[]')) || []; } catch (err) { outHidden = []; }
  function shortName(n) { var m = /^(.*?)\s*\((.*)\)\s*$/.exec(n); return m && m[1] ? {main: m[1], sub: m[2]} : {main: n, sub: ''}; }
  function setOutMenu(open) {
    $('outs').classList.toggle('open', open);
    $('outMenu').hidden = !open;
    $('outBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) { outEditing = false; $('outMenu').classList.remove('editing'); }
  }
  function nameOk(d) {
    var terms = $('outNames').value.toLowerCase().split(',').map(function (t) { return t.trim(); }).filter(Boolean);
    var n = d.name.toLowerCase();
    return !terms.length || terms.some(function (t) { return n.indexOf(t) >= 0; });
  }
  function renderOutputs() {
    var cur = null, menu = $('outMenu'), items = $('outItems'), shown = 0;
    outList.forEach(function (d) { if (d.cur) cur = d; });
    $('outs').classList.toggle('hidden', !outList.length);
    $('outName').textContent = cur ? shortName(cur.name).main : 'Choose';
    items.textContent = '';
    menu.classList.toggle('editing', outEditing);
    $('outEdit').textContent = outEditing ? 'Done' : 'Choose which devices to show';
    outList.forEach(function (d) {
      var hidden = outHidden.indexOf(d.id) >= 0 || !nameOk(d);
      if (!outEditing && hidden && !d.cur) return;
      shown++;
      var n = shortName(d.name), btn = document.createElement('button');
      btn.className = 'opt' + (d.cur ? ' cur' : '');
      var mark = document.createElement('div'); mark.className = 'mark' + (outHidden.indexOf(d.id) >= 0 ? '' : ' on');
      var t = document.createElement('div'); t.className = 'outText';
      var b = document.createElement('b'); b.textContent = n.main; t.appendChild(b);
      if (n.sub) { var s = document.createElement('small'); s.textContent = n.sub; t.appendChild(s); }
      btn.appendChild(mark); btn.appendChild(t);
      btn.addEventListener('click', function () {
        if (outEditing) {
          var i = outHidden.indexOf(d.id);
          if (i >= 0) outHidden.splice(i, 1); else outHidden.push(d.id);
          save('deckOutHidden', JSON.stringify(outHidden)); renderOutputs(); return;
        }
        if (!authed) { setOutMenu(false); if (wsOpen) askPin(); return; }
        buzz(10);
        outList.forEach(function (o) { o.cur = o.id === d.id; });
        setOutMenu(false); renderOutputs();
        ws.send('o ' + d.id);
      });
      items.appendChild(btn);
    });
    if (!shown) { var e = document.createElement('div'); e.className = 'outText'; e.style.padding = '12px'; e.innerHTML = '<small>No devices match</small>'; items.appendChild(e); }
  }
  $('outNames').value = store('deckOutNames', '');
  $('outNames').addEventListener('input', function () { save('deckOutNames', this.value); renderOutputs(); });
  $('outEdit').addEventListener('click', function () { outEditing = !outEditing; renderOutputs(); });
  function onOutputs(list) { outList = list; renderOutputs(); }
  $('outBtn').addEventListener('click', function () { setOutMenu($('outMenu').hidden); });
  document.addEventListener('pointerdown', function (e) {
    if (!$('outMenu').hidden && !$('outs').contains(e.target)) setOutMenu(false);
  });

  /* ---------- sheets ---------- */
  function setSheet(id, open) {
    $(id).classList.toggle('open', open);
    $('edBackdrop').classList.toggle('open', document.querySelector('.sheet.open') !== null);
  }
  function closeSheets() {
    document.querySelectorAll('.sheet.open').forEach(function (s) { s.classList.remove('open'); });
    $('edBackdrop').classList.remove('open');
  }
  $('edBackdrop').addEventListener('click', closeSheets);

  /* ---------- layouts: profiles made of pages of buttons ---------- */
  var ICONS = {
    hotkey:  '<svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>',
    url:     '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15 15 0 0 1 4 10 15 15 0 0 1-4 10 15 15 0 0 1-4-10 15 15 0 0 1 4-10z"/></svg>',
    command: '<svg viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    empty:   '<svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
  };
  var MAX_PAGES = 6;
  var profiles = [{id: 'default', name: 'Default', apps: [], pages: [[null, null, null, null, null, null, null, null, null]]}];
  var activeExe = '', manualProfile = store('deckProfile', 'auto'), curProfId = 'default', curPage = 0;

  function findProfile(id) {
    for (var i = 0; i < profiles.length; i++) if (profiles[i].id === id) return profiles[i];
    return null;
  }
  function pickProfile() {
    if (manualProfile !== 'auto') {
      var m = findProfile(manualProfile);
      if (m) return m;
      manualProfile = 'auto'; save('deckProfile', 'auto');
    }
    for (var i = 0; i < profiles.length; i++) {
      if (profiles[i].id !== 'default' && profiles[i].apps.indexOf(activeExe) >= 0) return profiles[i];
    }
    return profiles[0];
  }
  function currentProfile() { return findProfile(curProfId) || profiles[0]; }
  function syncProfile() {
    var p = pickProfile();
    if (p.id !== curProfId) { curProfId = p.id; curPage = 0; }
    if (curPage >= p.pages.length) curPage = p.pages.length - 1;
    renderShortcuts();
    $('layoutSub').textContent = profiles.length + (profiles.length === 1 ? ' layout' : ' layouts') + ', showing ' + currentProfile().name;
  }

  function renderShortcuts() {
    var p = currentProfile();
    $('profName').textContent = p.name;
    $('profAuto').textContent = manualProfile === 'auto' ? 'auto' : 'pinned';

    var pb = $('pageBtns'); pb.innerHTML = '';
    p.pages.forEach(function (pg, i) {
      var b = document.createElement('button');
      b.className = 'pgBtn' + (i === curPage ? ' on' : '');
      b.textContent = String(i + 1);
      b.setAttribute('aria-label', 'Page ' + (i + 1));
      bindPageBtn(b, i);
      pb.appendChild(b);
    });
    if (p.pages.length < MAX_PAGES) {
      var add = document.createElement('button');
      add.className = 'pgBtn'; add.textContent = '+'; add.setAttribute('aria-label', 'Add a page');
      add.addEventListener('click', addPage);
      pb.appendChild(add);
    }

    var grid = $('slotGrid'); grid.innerHTML = '';
    p.pages[curPage].forEach(function (slot, i) {
      var b = document.createElement('button');
      b.className = 'btn custom' + (slot ? '' : ' empty');
      b.innerHTML = ICONS[slot ? slot.type : 'empty'];
      var l = document.createElement('span');
      l.className = 'label';
      l.textContent = slot ? slot.label : 'Add button';
      b.appendChild(l);
      bindSlot(b, p.id, curPage, i, slot);
      grid.appendChild(b);
    });
  }

  function bindSlot(el, pid, page, i, slot) {
    var pressTimer = null, longPressed = false;
    function clear() { clearTimeout(pressTimer); pressTimer = null; }
    el.addEventListener('pointerdown', function () {
      longPressed = false;
      if (!slot) return;
      pressTimer = setTimeout(function () { longPressed = true; buzz(30); openEditor(pid, page, i, slot); }, 600);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) { el.addEventListener(ev, clear); });
    el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    el.addEventListener('click', function () {
      if (longPressed) { longPressed = false; return; }
      if (!slot) { openEditor(pid, page, i, null); return; }
      buzz(15);
      api('/api/press', {profile: pid, page: page, index: i}).then(function (r) {
        if (!r.ok && r.status !== 0) buzz([60, 40, 60]);
      });
    });
  }

  function bindPageBtn(b, i) {
    var t = null, long = false;
    function clear() { clearTimeout(t); t = null; }
    b.addEventListener('pointerdown', function () {
      long = false;
      if (currentProfile().pages.length > 1) t = setTimeout(function () { long = true; buzz(30); removePage(i); }, 700);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) { b.addEventListener(ev, clear); });
    b.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    b.addEventListener('click', function () {
      if (long) { long = false; return; }
      curPage = i; renderShortcuts();
    });
  }

  // Edits need the PIN. It is asked for once and then remembered on this phone.
  function withPin(cb) {
    if (!pin) {
      var p = window.prompt('Enter the PIN shown on the laptop');
      if (!p) return;
      pin = p.trim();
    }
    cb(pin);
  }
  function applyProfiles(r) {
    if (r.ok) { save('deckPin', pin); profiles = r.data.profiles; syncProfile(); return true; }
    if (r.status === 403) { pin = ''; forget('deckPin'); updatePinNote(); }
    return false;
  }
  function addPage() {
    var pid = curProfId;
    withPin(function (p) {
      api('/api/page', {pin: p, profile: pid, action: 'add'}).then(function (r) {
        if (applyProfiles(r)) { curPage = currentProfile().pages.length - 1; renderShortcuts(); }
        else window.alert(r.data.error || 'Something went wrong.');
      });
    });
  }
  function removePage(i) {
    if (!window.confirm('Delete page ' + (i + 1) + ' and its buttons?')) return;
    var pid = curProfId;
    withPin(function (p) {
      api('/api/page', {pin: p, profile: pid, action: 'delete', page: i}).then(function (r) {
        if (!applyProfiles(r)) window.alert(r.data.error || 'Something went wrong.');
      });
    });
  }

  function loadProfiles() {
    fetch('/api/profiles').then(function (r) { return r.json(); }).then(function (j) {
      profiles = j.profiles; setOnline(true); syncProfile();
    }).catch(function () { setOnline(false); });
  }

  function onActiveApp(exe) {
    activeExe = exe;
    if (manualProfile === 'auto') syncProfile();
  }

  /* ---------- button editor ---------- */
  var HELP = {
    hotkey:  {name: 'Shortcut', ph: 'ctrl+shift+m', text: 'Keys joined with +, for example ctrl+c, win+d or alt+tab.'},
    url:     {name: 'Website', ph: 'https://example.com', text: 'The page to open in the laptop browser.'},
    command: {name: 'Program or command', ph: 'notepad', text: 'Runs on the laptop, for example notepad or code.'}
  };
  var editTarget = null, deleteArmed = false;

  function applyType() {
    var h = HELP[$('edType').value];
    $('edValueName').firstChild.nodeValue = h.name;
    $('edValue').placeholder = h.ph;
    $('edHelp').textContent = h.text;
  }
  $('edType').addEventListener('change', applyType);
  function showError(msg) { $('edError').textContent = msg || ''; }

  function openEditor(pid, page, i, slot) {
    editTarget = {pid: pid, page: page, index: i}; deleteArmed = false;
    $('edTitle').textContent = slot ? 'Edit button' : 'New button';
    $('edLabel').value = slot ? slot.label : '';
    $('edType').value = slot ? slot.type : 'hotkey';
    $('edValue').value = slot ? slot.value : '';
    $('edPin').value = '';
    $('edPinWrap').classList.toggle('hidden', !!pin);
    $('edDelete').classList.toggle('hidden', !slot);
    $('edDelete').textContent = 'Delete';
    showError('');
    applyType();
    setSheet('edSheet', true);
  }
  function afterSlotSave(r) {
    if (applyProfiles(r)) { setSheet('edSheet', false); return; }
    if (r.status === 403) $('edPinWrap').classList.remove('hidden');
    showError(r.data.error || 'Something went wrong.');
  }
  function editPin() { return pin || $('edPin').value.trim(); }

  $('edSave').addEventListener('click', function () {
    var p = editPin();
    if (!p) { showError('Enter the edit PIN.'); return; }
    pin = p;
    api('/api/slot', {
      profile: editTarget.pid, page: editTarget.page, index: editTarget.index, pin: p,
      label: $('edLabel').value, type: $('edType').value, value: $('edValue').value
    }).then(afterSlotSave);
  });
  $('edDelete').addEventListener('click', function () {
    if (!deleteArmed) { deleteArmed = true; $('edDelete').textContent = 'Tap again to delete'; return; }
    var p = editPin();
    if (!p) { showError('Enter the edit PIN.'); return; }
    pin = p;
    api('/api/slot/delete', {profile: editTarget.pid, page: editTarget.page, index: editTarget.index, pin: p}).then(afterSlotSave);
  });
  $('edCancel').addEventListener('click', function () { setSheet('edSheet', false); });

  /* ---------- quick launch ---------- */
  var tiles = [], lnIndex = -1, lnDeleteArmed = false;
  ICONS.app = '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/></svg>';
  function renderTiles() {
    var box = $('tiles'); box.innerHTML = '';
    tiles.forEach(function (t, i) { box.appendChild(makeTile(t, i)); });
    if (tiles.length < 8) box.appendChild(makeTile(null, -1));
  }
  function makeTile(t, i) {
    var b = document.createElement('button');
    b.className = 'tile' + (t ? '' : ' empty');
    b.innerHTML = ICONS[t ? t.type : 'empty'];
    var l = document.createElement('span'); l.textContent = t ? t.label : 'Add tile'; b.appendChild(l);
    var timer = null, long = false;
    function clear() { clearTimeout(timer); timer = null; }
    b.addEventListener('pointerdown', function () {
      long = false;
      if (t) timer = setTimeout(function () { long = true; buzz(30); openTile(i); }, 600);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) { b.addEventListener(ev, clear); });
    b.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    b.addEventListener('click', function () {
      if (long) { long = false; return; }
      if (!t) { openTile(-1); return; }
      buzz(15); sendBtn('l ' + i);
    });
    return b;
  }
  function loadTiles() {
    fetch('/api/launch').then(function (r) { return r.json(); }).then(function (j) { tiles = j.launch || []; renderTiles(); })
      .catch(function () { renderTiles(); });
  }
  function lnShowError(m) { $('lnError').textContent = m || ''; }
  function openTile(i) {
    lnIndex = i; lnDeleteArmed = false;
    var t = i >= 0 ? tiles[i] : null;
    $('lnTitle').textContent = t ? 'Edit tile' : 'New tile';
    $('lnLabel').value = t ? t.label : ''; $('lnValue').value = t ? t.value : ''; $('lnPin').value = '';
    $('lnPinWrap').classList.toggle('hidden', !!pin);
    $('lnDelete').classList.toggle('hidden', !t); $('lnDelete').textContent = 'Delete';
    lnShowError(''); setSheet('lnSheet', true);
  }
  function afterTileSave(r) {
    if (r.ok) { save('deckPin', pin); tiles = r.data.launch || []; renderTiles(); setSheet('lnSheet', false); return; }
    if (r.status === 403) { pin = ''; forget('deckPin'); $('lnPinWrap').classList.remove('hidden'); }
    lnShowError(r.data.error || 'Something went wrong.');
  }
  function lnPin() { return pin || $('lnPin').value.trim(); }
  $('lnSave').addEventListener('click', function () {
    var p = lnPin(); if (!p) { lnShowError('Enter the edit PIN.'); return; }
    pin = p;
    api('/api/launch', {pin: p, index: lnIndex, label: $('lnLabel').value, value: $('lnValue').value}).then(afterTileSave);
  });
  $('lnDelete').addEventListener('click', function () {
    if (!lnDeleteArmed) { lnDeleteArmed = true; $('lnDelete').textContent = 'Tap again to delete'; return; }
    var p = lnPin(); if (!p) { lnShowError('Enter the edit PIN.'); return; }
    pin = p;
    api('/api/launch/delete', {pin: p, index: lnIndex}).then(afterTileSave);
  });
  $('lnCancel').addEventListener('click', function () { setSheet('lnSheet', false); });
  loadTiles();

  /* ---------- power ---------- */
  var PW = {
    sleep: ['Sleep the laptop?', 'The connection ends until you wake it with its power button.', 'Sleep'],
    restart: ['Restart the laptop?', 'It restarts in 15 seconds. You can stop it until then.', 'Restart'],
    shutdown: ['Shut down the laptop?', 'It shuts down in 15 seconds. You can stop it until then.', 'Shut down']
  };
  var pwAct = null, pwTimer = 0;
  function pwStop() { clearInterval(pwTimer); pwTimer = 0; }
  document.querySelectorAll('[data-x]').forEach(function (b) {
    b.addEventListener('click', function () {
      var a = b.getAttribute('data-x');
      if (!PW[a]) { buzz(20); sendBtn('x ' + a); return; }
      pwAct = a; pwStop();
      $('pwTitle').textContent = PW[a][0]; $('pwText').textContent = PW[a][1]; $('pwGo').textContent = PW[a][2];
      $('pwGo').classList.remove('hidden'); $('pwCancel').textContent = 'Cancel';
      setSheet('pwSheet', true);
    });
  });
  $('pwGo').addEventListener('click', function () {
    sendBtn('x ' + pwAct);
    if (pwAct === 'sleep') { setSheet('pwSheet', false); return; }
    var left = 15;
    $('pwGo').classList.add('hidden'); $('pwCancel').textContent = 'Stop';
    function paint() { $('pwText').textContent = (pwAct === 'restart' ? 'Restarting' : 'Shutting down') + ' in ' + left + ' s'; }
    paint();
    pwTimer = setInterval(function () { left--; if (left <= 0) { pwStop(); setSheet('pwSheet', false); } else paint(); }, 1000);
  });
  $('pwCancel').addEventListener('click', function () {
    if (pwTimer) { pwStop(); sendBtn('x cancel'); }
    setSheet('pwSheet', false);
  });

  /* ---------- layout manager ---------- */
  var pfEditId = null;
  function showPf(view) {
    $('pfList').classList.toggle('hidden', view !== 'list');
    $('pfEdit').classList.toggle('hidden', view !== 'edit');
  }
  function openProfiles() { renderProfileList(); showPf('list'); setSheet('pfSheet', true); }

  function renderProfileList() {
    var box = $('pfRows'); box.innerHTML = '';
    function row(id, name, sub, editable) {
      var wrap = document.createElement('div'); wrap.className = 'pfRow';
      var pick = document.createElement('button');
      pick.className = 'pfPick' + (manualProfile === id ? ' on' : '');
      pick.appendChild(document.createTextNode(name));
      var s = document.createElement('small'); s.textContent = sub; pick.appendChild(s);
      pick.addEventListener('click', function () {
        manualProfile = id; save('deckProfile', id); syncProfile(); setSheet('pfSheet', false);
      });
      wrap.appendChild(pick);
      if (editable) {
        var ed = document.createElement('button'); ed.className = 'abtn'; ed.textContent = 'Edit';
        ed.addEventListener('click', function () { openProfileEditor(id); });
        wrap.appendChild(ed);
      }
      box.appendChild(wrap);
    }
    row('auto', 'Auto', 'Follows the app in front on the laptop' + (activeExe ? ' (now ' + activeExe + ')' : ''), false);
    profiles.forEach(function (p) {
      row(p.id, p.name, p.id === 'default' ? 'Used when no other layout matches' : (p.apps.length ? p.apps.join(', ') : 'No apps yet'), p.id !== 'default');
    });
  }

  function openProfileEditor(id) {
    pfEditId = id;
    var p = id ? findProfile(id) : null;
    $('pfTitle').textContent = p ? 'Edit layout' : 'New layout';
    $('pfName').value = p ? p.name : '';
    $('pfApps').value = p ? p.apps.join(', ') : '';
    $('pfError').textContent = '';
    $('pfDelete').classList.toggle('hidden', !p);
    var use = $('pfUse');
    use.textContent = activeExe ? 'Add ' + activeExe : 'Active app unknown';
    use.disabled = !activeExe;
    showPf('edit');
  }
  $('pfUse').addEventListener('click', function () {
    if (!activeExe) return;
    var cur = $('pfApps').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (cur.indexOf(activeExe) < 0) cur.push(activeExe);
    $('pfApps').value = cur.join(', ');
  });
  $('pfNew').addEventListener('click', function () { openProfileEditor(null); });
  $('pfBack').addEventListener('click', function () { renderProfileList(); showPf('list'); });
  $('pfClose').addEventListener('click', function () { setSheet('pfSheet', false); });
  $('pfSave').addEventListener('click', function () {
    withPin(function (p) {
      api('/api/profile', {pin: p, id: pfEditId, name: $('pfName').value, apps: $('pfApps').value}).then(function (r) {
        if (applyProfiles(r)) { renderProfileList(); showPf('list'); }
        else $('pfError').textContent = r.data.error || 'Something went wrong.';
      });
    });
  });
  $('pfDelete').addEventListener('click', function () {
    if (!window.confirm('Delete this layout and all its pages?')) return;
    var gone = pfEditId;
    withPin(function (p) {
      api('/api/profile/delete', {pin: p, id: gone}).then(function (r) {
        if (applyProfiles(r)) {
          if (manualProfile === gone) { manualProfile = 'auto'; save('deckProfile', 'auto'); syncProfile(); }
          renderProfileList(); showPf('list');
        } else $('pfError').textContent = r.data.error || 'Something went wrong.';
      });
    });
  });
  $('profBtn').addEventListener('click', openProfiles);
  $('manageLayouts').addEventListener('click', openProfiles);

  /* ---------- trackpad ---------- */
  var SCROLL_GAIN = 4;   // wheel units per pixel (120 units = one notch)

  var ws = null, wsOpen = false, authed = false, wsTimer = null, wsDelay = 500, everOpen = false, gotNow = false, nowTimer = null;
  var lat = $('lat');

  function wsConnect() {
    try {
      ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    } catch (err) { return; }
    ws.onopen = function () {
      wsOpen = true; everOpen = true; wsDelay = 500; authed = false;
      clearTimeout(nowTimer);
      nowTimer = setTimeout(function () {  // the laptop always answers within a second or two
        if (!gotNow) onNow({on: false, note: 'Not running. Check the laptop console'});
      }, 4000);
      if (pin) { lat.textContent = 'Signing in'; ws.send('a ' + pin); }
      else { lat.textContent = 'Tap the pad to enter the PIN'; }
    };
    ws.onmessage = function (ev) {
      var d = ev.data;
      if (typeof d !== 'string') return;
      if (d === 'a ok') {
        authed = true; lat.textContent = 'Connected'; save('deckPin', pin); updatePinNote(); tellActive();
      } else if (d === 'a bad') {
        authed = false; pin = ''; forget('deckPin'); updatePinNote();
        lat.textContent = 'Wrong PIN. Tap the pad to try again';
      } else if (d.charAt(0) === 'f') {
        onTextFocus(d === 'f 1');
      } else if (d.charAt(0) === 'w') {
        onActiveApp(d.slice(2));
      } else if (d.charAt(0) === 'n') {
        gotNow = true;
        try { onNow(JSON.parse(d.slice(2))); } catch (err) {}
      } else if (d.charAt(0) === 'v') {
        var vp = d.split(' '); onVolume(+vp[1], vp[2] === '1');
      } else if (d.charAt(0) === 'o') {
        try { onOutputs(JSON.parse(d.slice(2))); } catch (err) {}
      } else if (d.charAt(0) === 'p' && authed) {
        lat.textContent = Math.round(performance.now() - parseFloat(d.slice(2))) + ' ms';
      }
    };
    ws.onclose = function () {
      wsOpen = false; authed = false;
      lat.textContent = everOpen ? 'Reconnecting' : 'Not connected';
      clearTimeout(wsTimer);
      wsTimer = setTimeout(wsConnect, wsDelay);
      wsDelay = Math.min(wsDelay * 2, 5000);
    };
    ws.onerror = function () { try { ws.close(); } catch (err) {} };
  }
  wsConnect();
  setInterval(function () { if (wsOpen) ws.send('p ' + performance.now()); }, 2000);
  // when the page is hidden the laptop stops its busy checks, which saves its battery
  function tellActive() { if (wsOpen && authed) ws.send('q ' + (document.hidden ? '0' : '1')); }
  document.addEventListener('visibilitychange', tellActive);

  // Movement is collected as it happens and sent once per screen frame.
  var pdx = 0, pdy = 0, psx = 0, psy = 0;
  function flushNow() {
    if (!wsOpen || !authed) { pdx = pdy = psx = psy = 0; return; }
    var mx = Math.trunc(pdx), my = Math.trunc(pdy);
    if (mx || my) { ws.send('m ' + mx + ' ' + my); pdx -= mx; pdy -= my; }
    var sx = Math.trunc(psx), sy = Math.trunc(psy);
    if (sx || sy) { ws.send('s ' + sx + ' ' + sy); psx -= sx; psy -= sy; }
  }
  function frame() { flushNow(); requestAnimationFrame(frame); }
  requestAnimationFrame(frame);

  function sendBtn(text) { flushNow(); if (wsOpen && authed) ws.send(text); }
  function askPin() {
    var p = window.prompt('Enter the PIN shown on the laptop');
    if (p && wsOpen) { pin = p.trim(); ws.send('a ' + pin); }
  }

  var pad = $('pad'), pointers = {}, count = 0, g = null, dragging = false;
  var lastTap = {t: 0, x: 0, y: 0};

  pad.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  pad.addEventListener('mousedown', function (e) { e.preventDefault(); });  // keep the phone keyboard open

  function newGesture(now) {
    return {t0: now, moved: 0, max: 1, sx: 0, sy: 0, last2: null};
  }

  pad.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    if (!authed) { if (wsOpen) setTimeout(askPin, 0); return; }
    try { pad.setPointerCapture(e.pointerId); } catch (err) {}
    pointers[e.pointerId] = {x: e.clientX, y: e.clientY};
    count = Object.keys(pointers).length;
    pad.classList.add('active', 'used');
    var now = performance.now();
    if (count === 1) {
      g = newGesture(now);
      var near = Math.abs(e.clientX - lastTap.x) + Math.abs(e.clientY - lastTap.y) < 60;
      if (now - lastTap.t < 300 && near) g.dragIntent = true;  // tap, then drag: the button goes down once the finger moves
    } else if (g) {
      g.max = Math.max(g.max, count);
      g.last2 = null;
    }
  });

  // Two fingers always scroll (no pinch, so the two never fight each other).
  function twoFinger() {
    var ids = Object.keys(pointers), a = pointers[ids[0]], b = pointers[ids[1]];
    if (!a || !b) return;
    var cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
    var sgn = S.natural ? 1 : -1;
    if (g.last2) {
      psy += sgn * (cy - g.last2.cy) * SCROLL_GAIN;
      psx += -sgn * (cx - g.last2.cx) * SCROLL_GAIN;
    }
    g.last2 = {cx: cx, cy: cy};
  }

  pad.addEventListener('pointermove', function (e) {
    var p = pointers[e.pointerId];
    if (!p || !g) return;
    var dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    g.moved += Math.abs(dx) + Math.abs(dy);
    if (g.max >= 3) {  // three fingers: collect the swipe, decide when they lift
      if (count >= 3) { g.sx += dx / count; g.sy += dy / count; }
      return;
    }
    if (count === 1) {
      if (g.max > 1) return;  // after a two-finger gesture, wait until all fingers lift
      if (g.dragIntent && !dragging && g.moved >= 4) { dragging = true; sendBtn('d l'); }
      if (g.moved < 4 && !dragging) return;  // ignore the small wobble of a tap
      var v = Math.sqrt(dx * dx + dy * dy);
      var gain = S.sens * (1.2 + Math.min(v, 40) * 0.06);  // faster swipe, longer reach
      pdx += dx * gain; pdy += dy * gain;
    } else if (count === 2) {
      twoFinger();
    }
  });

  function endPointer(e, cancelled) {
    if (!pointers[e.pointerId]) return;
    delete pointers[e.pointerId];
    count = Object.keys(pointers).length;
    if (count > 0 || !g) { if (g) g.last2 = null; return; }
    pad.classList.remove('active');
    var dur = performance.now() - g.t0;
    if (dragging) {
      sendBtn('u l'); dragging = false; lastTap.t = 0;
    } else if (!cancelled && g.max >= 3) {
      var ax = Math.abs(g.sx), ay = Math.abs(g.sy);
      if (Math.max(ax, ay) > 40) {
        var dir = ax > ay ? (g.sx > 0 ? 'right' : 'left') : (g.sy > 0 ? 'down' : 'up');
        sendBtn('g ' + dir); buzz(15);
      }
    } else if (!cancelled && g.moved < 10 && dur < 300) {
      if (g.max === 1) {
        sendBtn('d l'); sendBtn('u l'); buzz(10);
        lastTap = {t: performance.now(), x: e.clientX, y: e.clientY};
      } else if (g.max === 2) {
        sendBtn('d r'); sendBtn('u r'); buzz(10);
      }
    }
    g = null;
  }
  pad.addEventListener('pointerup', function (e) { endPointer(e, false); });
  pad.addEventListener('pointercancel', function (e) { endPointer(e, true); });

  // Left and right buttons: hold to drag or keep a button pressed
  [['btnL', 'l'], ['btnR', 'r'], ['jL', 'l'], ['jR', 'r']].forEach(function (b) {
    var el = $(b[0]), down = false;
    el.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      try { el.setPointerCapture(e.pointerId); } catch (err) {}
      down = true; el.classList.add('on'); buzz(10); sendBtn('d ' + b[1]);
    });
    function up() { if (!down) return; down = false; el.classList.remove('on'); sendBtn('u ' + b[1]); }
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
    el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  });


  /* ---------- joystick ---------- */
  // Push the stick and the cursor moves for as long as you hold it there. A small push is slow and
  // precise, a full push is fast. The left thumb steers, the right thumb clicks, so dragging is easy.
  var JDEAD = 0.12, JMAX = 1500;  // dead zone, and cursor pixels per second at full push (times Cursor speed)
  var J = {id: null, vx: 0, vy: 0, raf: 0, last: 0, cx: 0, cy: 0, R: 1};
  var jZone = $('joyZone'), jThumb = $('jThumb'), jSvg = $('joySvg');
  var JD = Math.max(90, Math.min(230, parseInt(store('deckJoySize', '140'), 10) || 140));
  function paintJoySize() {
    jSvg.style.setProperty('--jd', JD + 'px');
    $('joySize').value = JD;
    $('joySizeVal').textContent = JD < 120 ? 'Small' : JD < 175 ? 'Medium' : 'Large';
  }
  // Resting place of the stick: near the thumb, at the bottom of the area
  function jRest() {
    if (J.id !== null) return;
    var w = jZone.clientWidth, h = jZone.clientHeight;
    if (!w) return;
    var land = window.matchMedia('(orientation: landscape) and (max-height: 500px)').matches;
    var x = land ? Math.min(16, w - JD) : (w - JD) / 2, y = Math.max(0, h - JD - 16);
    jSvg.style.transform = 'translate(' + x.toFixed(0) + 'px,' + y.toFixed(0) + 'px)';
  }
  paintJoySize();
  window.addEventListener('resize', jRest);
  (function () {
    var g = $('jTicks');
    for (var i = 0; i < 24; i++) {
      var a = i * 15 * Math.PI / 180, l = document.createElementNS(NS, 'line');
      l.setAttribute('x1', (100 + 88 * Math.sin(a)).toFixed(2)); l.setAttribute('y1', (100 - 88 * Math.cos(a)).toFixed(2));
      l.setAttribute('x2', (100 + 95 * Math.sin(a)).toFixed(2)); l.setAttribute('y2', (100 - 95 * Math.cos(a)).toFixed(2));
      l.setAttribute('class', 'jtick'); g.appendChild(l);
    }
  })();
  function jThumbTo(dx, dy) { jThumb.style.transform = 'translate(' + (dx * 56).toFixed(1) + 'px,' + (dy * 56).toFixed(1) + 'px)'; }
  function jAim(e) {
    var dx = (e.clientX - J.cx) / J.R, dy = (e.clientY - J.cy) / J.R, m = Math.hypot(dx, dy);
    if (m > 1) { dx /= m; dy /= m; m = 1; }
    jThumbTo(dx, dy);
    if (m < JDEAD) { J.vx = J.vy = 0; return; }
    var d = (m - JDEAD) / (1 - JDEAD), v = JMAX * S.sens * Math.pow(d, 1.9);
    J.vx = dx / m * v; J.vy = dy / m * v;
  }
  function jTick(now) {
    if (J.id === null) { J.raf = 0; return; }
    var dt = Math.min(now - J.last, 50) / 1000;
    J.last = now; pdx += J.vx * dt; pdy += J.vy * dt;
    flushNow();
    J.raf = requestAnimationFrame(jTick);
  }
  function jRelease() {
    if (J.id === null) return;
    J.id = null; J.vx = J.vy = 0; jZone.classList.remove('on'); jThumbTo(0, 0); jRest();
  }
  jZone.addEventListener('pointerdown', function (e) {
    if (J.id !== null || e.target.closest('.modeSeg')) return;
    e.preventDefault();
    if (!authed) { if (wsOpen && !pin) askPin(); return; }
    try { jZone.setPointerCapture(e.pointerId); } catch (err) {}
    // the stick appears under the thumb, wherever it lands (kept inside the area)
    var zr = jZone.getBoundingClientRect(), half = JD / 2;
    var lx = Math.max(half, Math.min(zr.width - half, e.clientX - zr.left)), ly = Math.max(half, Math.min(zr.height - half, e.clientY - zr.top));
    jSvg.style.transform = 'translate(' + (lx - half).toFixed(1) + 'px,' + (ly - half).toFixed(1) + 'px)';
    J.cx = zr.left + lx; J.cy = zr.top + ly; J.R = JD * 0.28;
    J.id = e.pointerId; jZone.classList.add('on', 'used'); jAim(e); buzz(8);
    J.last = performance.now(); if (!J.raf) J.raf = requestAnimationFrame(jTick);
  });
  jZone.addEventListener('pointermove', function (e) { if (e.pointerId === J.id) jAim(e); });
  function jEnd(e) { if (e.pointerId === J.id) jRelease(); }
  jZone.addEventListener('pointerup', jEnd);
  jZone.addEventListener('pointercancel', jEnd);
  jZone.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  // Scroll strip: drag up or down
  var jScroll = $('joyScroll'), sc = {id: null, y: 0, sy: 0};
  jScroll.addEventListener('pointerdown', function (e) {
    if (sc.id !== null) return;
    e.preventDefault();
    if (!authed) { if (wsOpen && !pin) askPin(); return; }
    try { jScroll.setPointerCapture(e.pointerId); } catch (err) {}
    sc.id = e.pointerId; sc.y = e.clientY; jScroll.classList.add('on');
  });
  jScroll.addEventListener('pointermove', function (e) {
    if (e.pointerId !== sc.id) return;
    var dy = e.clientY - sc.y; sc.y = e.clientY; sc.sy = (sc.sy + dy) % 14;
    jScroll.style.setProperty('--sy', sc.sy.toFixed(1) + 'px');
    psy += (S.natural ? 1 : -1) * dy * SCROLL_GAIN * 1.5;
  });
  function scEnd(e) { if (e.pointerId === sc.id) { sc.id = null; jScroll.classList.remove('on'); } }
  jScroll.addEventListener('pointerup', scEnd);
  jScroll.addEventListener('pointercancel', scEnd);
  jScroll.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  // Hold keeps the left button down so the stick drags things; Double is a quick double click
  var jHold = $('jHold'), holding = false;
  function setHold(on) {
    if (holding === on) return;
    holding = on; jHold.setAttribute('aria-pressed', on ? 'true' : 'false'); sendBtn(on ? 'd l' : 'u l');
  }
  jHold.addEventListener('click', function () { if (!authed) { if (wsOpen && !pin) askPin(); return; } buzz(12); setHold(!holding); });
  $('jDbl').addEventListener('click', function () {
    if (!authed) { if (wsOpen && !pin) askPin(); return; }
    buzz(12); sendBtn('d l'); sendBtn('u l'); sendBtn('d l'); sendBtn('u l');
  });

  // Touchpad, joystick or arrow keys
  var MODES = [['pad', 'Touchpad'], ['joy', 'Joystick'], ['rem', 'Arrows']];
  function setPadMode(mode) {
    jRelease(); setHold(false);
    S.padMode = mode; save('deckPadMode', mode);
    var pt = $('pageTrackpad');
    pt.classList.toggle('joy', mode === 'joy'); pt.classList.toggle('rem', mode === 'rem');
    document.querySelectorAll('.modeSeg button').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-mode') === mode ? 'true' : 'false'); });
    jRest();
  }
  [$('pad'), $('joyZone'), $('remView')].forEach(function (host) {
    var seg = document.createElement('div'); seg.className = 'modeSeg';
    ['pointerdown', 'pointerup', 'pointercancel', 'pointermove', 'touchstart'].forEach(function (t) { seg.addEventListener(t, function (e) { e.stopPropagation(); }); });
    MODES.forEach(function (m) {
      var b = document.createElement('button'); b.textContent = m[1]; b.setAttribute('data-mode', m[0]);
      b.addEventListener('click', function () { buzz(10); setPadMode(m[0]); });
      seg.appendChild(b);
    });
    host.appendChild(seg);
  });
  S.padMode = store('deckPadMode', 'pad');
  if (['pad', 'joy', 'rem'].indexOf(S.padMode) < 0) S.padMode = 'pad';
  setPadMode(S.padMode);

  // Arrow keys: tap to press, hold to repeat
  document.querySelectorAll('#remView [data-k], #remView [data-h]').forEach(function (el) {
    var msg = el.hasAttribute('data-h') ? 'h ' + el.getAttribute('data-h') : 'k ' + el.getAttribute('data-k'), rep = el.getAttribute('data-rep') === '1', delay = 0, timer = 0, down = false;
    function stop() { clearTimeout(delay); clearInterval(timer); delay = timer = 0; if (down) { down = false; el.classList.remove('on'); } }
    el.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      if (!authed) { if (wsOpen && !pin) askPin(); return; }
      try { el.setPointerCapture(e.pointerId); } catch (err) {}
      down = true; el.classList.add('on'); buzz(8); sendBtn(msg);
      if (rep) delay = setTimeout(function () { timer = setInterval(function () { sendBtn(msg); }, 70); }, 380);
    });
    ['pointerup', 'pointercancel'].forEach(function (t) { el.addEventListener(t, stop); });
    el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  });

  /* ---------- typing: phone keyboard to laptop ---------- */
  // The field keeps a few invisible characters in front of what you type, so Backspace
  // always has something to delete and every change can be mirrored to the laptop.
  var FILL = String.fromCharCode(8203, 8203, 8203, 8203);
  var typeInput = $('typeInput'), typeBar = $('typeBar'), chip = $('kbChip');
  var typed = '', composing = false, kbOpen = false, kbAuto = false, kbSeen = false, textActive = false;
  var vv = window.visualViewport;
  var baseH = vv ? vv.height : window.innerHeight;

  function typeSend(prefix, payload) { if (wsOpen && authed) ws.send(prefix + ' ' + payload); }
  function sendText(s) { for (var i = 0; i < s.length; i += 150) typeSend('t', s.slice(i, i + 150)); }
  function setCaretEnd() { var n = typeInput.value.length; try { typeInput.setSelectionRange(n, n); } catch (err) {} }
  function resetInput() { typed = ''; typeInput.value = FILL; setCaretEnd(); }
  function commonPrefix(a, b) {
    var n = Math.min(a.length, b.length), i = 0;
    while (i < n && a.charCodeAt(i) === b.charCodeAt(i)) i++;
    return i;
  }

  typeInput.addEventListener('compositionstart', function () { composing = true; });
  typeInput.addEventListener('compositionend', function () { composing = false; });
  typeInput.addEventListener('focus', setCaretEnd);
  typeInput.addEventListener('select', setCaretEnd);
  typeInput.addEventListener('input', function () {
    var prev = FILL + typed, v = typeInput.value;
    var cp = commonPrefix(prev, v);
    var del = prev.length - cp, ins = v.slice(cp);
    if (del > 0) typeSend('b', Math.min(del, 100));
    if (ins) sendText(ins);
    var intact = v.indexOf(FILL) === 0;
    typed = intact ? v.slice(FILL.length) : ins;
    if (!composing && (!intact || typed.length > 300)) {
      if (typed.length > 300) typed = typed.slice(-100);
      typeInput.value = FILL + typed; setCaretEnd();
    }
  });
  typeInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.keyCode === 13) { e.preventDefault(); typeSend('k', 'enter'); resetInput(); }
  });

  typeBar.querySelectorAll('[data-k]').forEach(function (b) {
    var k = b.getAttribute('data-k');
    b.addEventListener('mousedown', function (e) { e.preventDefault(); });
    b.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      typeSend('k', k); buzz(8);
      if (k === 'enter') resetInput();
      typeInput.focus({preventScroll: true});
    });
  });

  function positionBar() {
    var off = vv ? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : 0;
    typeBar.style.bottom = off + 'px';
  }
  function kbVisible() { return vv ? baseH - vv.height > 120 : document.activeElement === typeInput; }

  function openTypeBar(auto) {
    chip.classList.remove('show');
    if (kbOpen) { if (!auto) kbAuto = false; typeInput.focus({preventScroll: true}); return; }
    kbOpen = true; kbAuto = !!auto; kbSeen = false;
    resetInput();
    typeBar.classList.add('open');
    positionBar();
    typeInput.focus({preventScroll: true});
  }
  function closeTypeBar() {
    if (!kbOpen) return;
    kbOpen = false; kbAuto = false;
    typeBar.classList.remove('open');
    typeInput.blur();
  }

  if (vv) {
    var onViewport = function () {
      var ae = document.activeElement;
      if (!(ae && (ae.tagName === 'INPUT' || ae.tagName === 'SELECT'))) baseH = vv.height;
      if (!kbOpen) return;
      positionBar();
      if (kbVisible()) { kbSeen = true; chip.classList.remove('show'); }
      else if (kbSeen) closeTypeBar();  // the keyboard was dismissed
    };
    vv.addEventListener('resize', onViewport);
    vv.addEventListener('scroll', positionBar);
  }

  // The laptop reports when a text box gets or loses focus.
  function onTextFocus(on) {
    textActive = on;
    if (!S.autoKb || !authed) return;
    if (on) {
      if (kbOpen) return;
      buzz(20);
      openTypeBar(true);  // Android usually allows this for a few seconds after a tap
      setTimeout(function () {
        // if the keyboard did not appear, offer a button that is allowed to open it
        if (textActive && kbOpen && kbAuto && !kbVisible()) chip.classList.add('show');
      }, 400);
    } else {
      chip.classList.remove('show');
      if (kbOpen && kbAuto) closeTypeBar();
    }
  }

  chip.addEventListener('click', function () { openTypeBar(true); });
  $('tbDone').addEventListener('mousedown', function (e) { e.preventDefault(); });
  $('tbDone').addEventListener('click', closeTypeBar);
  ['btnKb', 'jKb'].forEach(function (id) {
    $(id).addEventListener('click', function () {
      if (!authed) { if (wsOpen) askPin(); return; }
      if (kbOpen && !kbAuto) closeTypeBar(); else openTypeBar(false);
    });
  });

  /* ---------- settings page ---------- */
  $('joySize').addEventListener('input', function () {
    JD = parseInt($('joySize').value, 10) || 140; save('deckJoySize', JD); paintJoySize(); jRest();
  });
  function paintSens() { $('sensVal').textContent = S.sens.toFixed(1) + 'x'; }
  $('sens').value = String(S.sens); paintSens();
  $('sens').addEventListener('input', function () {
    S.sens = parseFloat($('sens').value) || 1; save('deckSens', S.sens); paintSens();
  });
  bindSwitch('swNatural', 'natural');
  bindSwitch('swAutoKb', 'autoKb', function (on) {
    if (!on) { chip.classList.remove('show'); if (kbOpen && kbAuto) closeTypeBar(); }
  });

  function updatePinNote() {
    $('pinSub').textContent = pin ? 'Saved on this phone' : 'Not saved. You will be asked when needed';
    $('forgetPin').disabled = !pin;
  }
  updatePinNote();
  $('forgetPin').addEventListener('click', function () {
    pin = ''; forget('deckPin'); authed = false; updatePinNote();
    lat.textContent = 'Tap the pad to enter the PIN';
    if (ws) { try { ws.close(); } catch (err) {} }
  });

  /* ---------- full screen and keeping the screen on ---------- */
  function fsSupported() { var el = document.documentElement; return !!(el.requestFullscreen || el.webkitRequestFullscreen); }
  function isFs() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
  function enterFs() {
    var el = document.documentElement, f = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!f) return;
    try { var r = f.call(el, {navigationUI: 'hide'}); if (r && r.catch) r.catch(function () {}); } catch (err) {}
  }
  function exitFs() { var f = document.exitFullscreen || document.webkitExitFullscreen; if (f) f.call(document); }
  function paintFs() {
    var b = $('fsBtn');
    if (!fsSupported()) { b.textContent = 'Not available'; b.disabled = true; return; }
    b.textContent = isFs() ? 'Exit' : 'Enter';
  }
  paintFs();
  document.addEventListener('fullscreenchange', paintFs);
  document.addEventListener('webkitfullscreenchange', paintFs);
  $('fsBtn').addEventListener('click', function () { if (isFs()) exitFs(); else enterFs(); });
  bindSwitch('swFsAuto', 'fsAuto');

  var wake = null, video = $('awakeVideo');
  function keepAwake() {
    var note = $('awakeNote');
    if (!S.awake) return;
    if (navigator.wakeLock && navigator.wakeLock.request) {
      navigator.wakeLock.request('screen').then(function (lock) {
        wake = lock;
        lock.addEventListener('release', function () { wake = null; });
        note.textContent = 'On. The phone will not sleep while this page is open';
      }).catch(function () { videoAwake(); });
    } else {
      videoAwake();
    }
  }
  function videoAwake() {
    // Browsers only offer the real screen lock on secure pages. Playing a tiny muted video does the same job.
    var p = video.play();
    if (p && p.then) {
      p.then(function () { $('awakeNote').textContent = 'On, using a small hidden video'; })
       .catch(function () { $('awakeNote').textContent = 'Starts after your first tap'; });
    }
  }
  function stopAwake() {
    if (wake) { try { wake.release(); } catch (err) {} wake = null; }
    video.pause();
    $('awakeNote').textContent = 'Off';
  }
  bindSwitch('swAwake', 'awake', function (on) { if (on) keepAwake(); else stopAwake(); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && S.awake && !wake) keepAwake(); });
  keepAwake();

  // A page may only go full screen from a tap. So every tap while not full screen tries again,
  // which brings it back after a keyboard, a swipe from the edge or a screen rotation drops it.
  var inApp = /DeckApp/.test(navigator.userAgent) && !!window.DeckApp;
  var asApp = inApp || window.matchMedia('(display-mode: fullscreen), (display-mode: standalone)').matches;
  if (inApp) {
    document.documentElement.classList.add('inapp');
    try { $('addrVal').textContent = window.DeckApp.address().replace('http://', ''); } catch (err) {}
    $('chgAddr').addEventListener('click', function () { try { window.DeckApp.changeAddress(); } catch (err) {} });
    var tellVol = function () { try { window.DeckApp.volumeKeys(S.hwVol); } catch (err) {} };
    bindSwitch('swHwVol', 'hwVol', tellVol); tellVol();
    window.deckVol = function (dir) { if (S.hwVol) { setLevel(K.level + dir * 2, true); } };
    // lock-screen controls: the app shows what is playing and sends play, pause and skip back here
    window.deckTellMedia = function () {
      var i = window.lastNow;
      try {
        if (!S.lockMedia || !i || !i.on) window.DeckApp.nowPlaying('', '', false, false);
        else window.DeckApp.nowPlaying(i.t || 'Playing', i.a || '', !!i.p, true);
      } catch (err) {}
    };
    window.deckMedia = function (name) { api('/action', {name: name}); };
    bindSwitch('swLockMedia', 'lockMedia', window.deckTellMedia); window.deckTellMedia();
  }
  document.addEventListener('pointerup', function (e) {
    var onBtn = e.target.closest && e.target.closest('#fsBtn');
    if (S.fsAuto && !asApp && !onBtn && !isFs()) enterFs();
    if (S.awake && video.paused && !wake) videoAwake();
  });
  // The back gesture should not leave the deck by accident
  try {
    history.pushState({deck: 1}, '');
    window.addEventListener('popstate', function () { history.pushState({deck: 1}, ''); });
  } catch (err) {}

  $('installHow').textContent = asApp ? '' :
    'To install it as an app with no bars: in Chrome open chrome://flags, search for Insecure origins treated as secure, set it to Enabled, type ' +
    location.origin + ' in its box, tap Relaunch, open this page again, then menu and Install app.';
  // Say plainly how this page is open, and how to get rid of the browser bars for good
  (function () {
    var n = $('appNote');
    if (inApp) n.textContent = '';
    else if (asApp) n.textContent = 'Running as an installed app. There are no browser bars.';
    else if (!window.isSecureContext) n.textContent = 'Chrome hides Install app for this address because it is not https. The steps below switch it on.';
    else n.textContent = 'Use the Chrome menu and choose Install app to remove the browser bars for good.';
  })();

  if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(function () {}); }

  /* ---------- connection heartbeat ---------- */
  var wasOffline = false;
  setInterval(function () {
    fetch('/ping', {cache: 'no-store'})
      .then(function () { if (wasOffline) { wasOffline = false; loadProfiles(); } setOnline(true); })
      .catch(function () { wasOffline = true; setOnline(false); });
  }, 3000);

  renderShortcuts();
  loadProfiles();
</script>
</body>
</html>
"""


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _startup_file():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "ControlDeck.vbs")


def install_startup():
    """Start the deck with Windows, with no console window."""
    script = os.path.abspath(__file__)
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    vbs = ('Set sh = CreateObject("WScript.Shell")\r\n'
           'sh.CurrentDirectory = "%s"\r\n'
           'sh.Run """%s"" ""%s""", 0, False\r\n') % (os.path.dirname(script), exe, script)
    path = _startup_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(vbs)
    return path


def remove_startup():
    path = _startup_file()
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


if __name__ == "__main__":
    if "--install-startup" in sys.argv or "--remove-startup" in sys.argv:
        if not IS_WINDOWS:
            print("Starting with the computer is set up for Windows only.")
        elif "--install-startup" in sys.argv:
            print("Done. The deck will start with Windows, with no window. Written to:\n  " + install_startup())
            print("To stop it now, open Task Manager and end pythonw.exe. To undo, run:  python server.py --remove-startup")
        else:
            print("Removed." if remove_startup() else "It was not set up.")
        sys.exit(0)
    if sys.stdout is None or sys.stderr is None:  # started without a window: keep what it says in a file
        _log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deck.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _log
    cfg = load_config()
    print(f"\n  Control Deck running. Open on your phone:  http://{local_ip()}:5000")
    print(f"  Edit PIN for custom buttons and typing: {cfg['pin']}")
    print("  To start with Windows and run without a window:  python server.py --install-startup")
    print("  No browser bars: on the phone open chrome://flags in Chrome, enable 'Insecure origins treated as secure',")
    print(f"  add http://{local_ip()}:5000 in its box, tap Relaunch, open the page again, then menu > Install app.")
    for note in start_watchers():
        print("  " + note)
    print()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
