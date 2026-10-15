"""
modules/file_helpers.py — File-type detection, file icons, App Store icon proxy.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import Response
from flask import request
from flask import send_file
from pathlib import Path
from urllib.parse import urlparse
import json
import re
import os
import threading
import time
import urllib.request
from modules.config import APPS_FILE, ICON_CACHE_DIR, STATIC_ICON_DIR, _ICON_EXT_MAP, _ICON_EXT_RE
from modules.web import _apps_lock, _atomic_write_json, app
from modules.error_log import _current_user
from modules.decorators import require_auth
from modules import winplat


# ══════════════════════════════════════════════════════════════════════
# FILE HELPERS
# ══════════════════════════════════════════════════════════════════════
_TX = {"txt","md","markdown","py","js","ts","jsx","tsx","html","htm","css","scss","sass",
    "json","xml","yaml","yml","toml","ini","cfg","conf","sh","bash","zsh","fish","sql",
    "r","go","rs","java","c","cpp","h","hpp","cs","php","rb","swift","kt","dart","vue",
    "env","log","csv","tsv","lock","tf","hcl","lua","ps1","bat","cmd","asm","s",
    "tex","bib","rst","adoc","org","vtt","srt","nfo","properties","gitignore",
    "dockerfile","makefile","editorconfig"}
_IM={"jpg","jpeg","png","gif","webp","svg","bmp","ico","tiff","tif","avif"}
_VD={"mp4","webm","ogg","mov","m4v"}
_AU={"mp3","wav","ogg","flac","aac","m4a","opus","wma"}
_PL={"dockerfile","makefile",".gitignore",".env",".editorconfig",".htaccess"}

def _ft(p:Path)->str:
    e=p.suffix.lstrip(".").lower(); n=p.name.lower()
    if e in _TX or n in _PL: return "text"
    if e in _IM: return "image"
    if e in _VD: return "video"
    if e in _AU: return "audio"
    if e=="pdf": return "pdf"
    if e in {"zip","tar","gz","bz2","7z","rar","xz","zst","deb","rpm"}: return "archive"
    return "binary"

_ICON_FETCH_MAX_BYTES = 100_000  # these are small optimized SVGs; anything bigger
                                  # isn't a legitimate icon response and shouldn't
                                  # be written to disk with an svg+xml content-type
def _fetch_and_cache_icon(ext: str) -> None:
    """Best-effort live fetch for an extension outside the bundled set (see
    STATIC_ICON_DIR's comment — this should be the rare case in practice,
    since the bundled set already covers everything vscode-icons knew about
    as of build time). Never raises: on any failure this simply doesn't
    create the cache file, and the route below falls through to a 404 the
    same as an extension nobody's ever looked up. Only the server makes
    this call, never the browser directly — see the route's own comment
    for why that boundary matters here specifically."""
    iconname = _ICON_EXT_MAP.get(ext, ext)  # best-effort guess when ext isn't in the
                                             # map at all: try the extension itself as
                                             # the icon name, which happens to be right
                                             # for a meaningful chunk of simple extensions
    # vscode-icons isn't published to npm under a name that mirrors this repo
    # layout, so this goes through jsdelivr's GitHub-mirror path instead of
    # its npm one (still the same trusted jsdelivr origin already allowed by
    # this project's CSP — see STATIC_ICON_DIR's comment).
    url = f"https://cdn.jsdelivr.net/gh/vscode-icons/vscode-icons@master/icons/file_type_{iconname}.svg"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read(_ICON_FETCH_MAX_BYTES + 1)
    except Exception:
        return
    if len(data) > _ICON_FETCH_MAX_BYTES:
        return
    head = data[:100].lstrip()
    if not (head.startswith(b"<svg") or head.startswith(b"<?xml")):
        return  # doesn't look like an SVG — never trust/write arbitrary fetched bytes
    try:
        ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ICON_CACHE_DIR / f".{ext}.svg.tmp{os.getpid()}"
        tmp.write_bytes(data)
        tmp.replace(ICON_CACHE_DIR / f"{ext}.svg")
    except Exception:
        pass

_ICON_MISS: dict = {}            # extension -> when a fetch for it last failed
_ICON_CACHE_MAX_FILES = 3000

@app.route("/api/file-icon/<ext>")
def api_file_icon(ext):
    """Per-extension file-type icon. Deliberately NOT auth-gated the same
    way file content routes are — these are static, non-sensitive icon
    glyphs (same visual for every user, everywhere), and gating them would
    only add a session check to something the browser fetches purely for
    decoration. Checks the bundled set first (the common case, never
    touches the network), then a small on-disk cache fed by a one-time
    live fetch per never-before-seen extension — see STATIC_ICON_DIR's
    comment above for why that fetch happens server-side rather than the
    browser calling the icon host directly."""
    ext = ext.lower()
    if not _ICON_EXT_RE.match(ext):
        return "", 404
    bundled = STATIC_ICON_DIR / f"{ext}.svg"
    if bundled.exists():
        return send_file(bundled, mimetype="image/svg+xml", max_age=31536000)  # part of
            # the app itself, never changes without a redeploy
    cached = ICON_CACHE_DIR / f"{ext}.svg"
    if not cached.exists():
        # Only a signed-in person can cause a download, a failed one is remembered for an hour, and the
        # cache is capped: this route is otherwise open to anyone who can reach the server.
        if _current_user() is None:
            return "", 404
        now = time.time()
        if now - _ICON_MISS.get(ext, 0) < 3600:
            return "", 404
        try:
            cached_n = sum(1 for _ in ICON_CACHE_DIR.glob("*.svg")) if ICON_CACHE_DIR.exists() else 0
        except OSError:
            cached_n = 0
        if cached_n >= _ICON_CACHE_MAX_FILES:
            return "", 404
        _fetch_and_cache_icon(ext)
        if not cached.exists():
            if len(_ICON_MISS) > 5000:
                _ICON_MISS.clear()
            _ICON_MISS[ext] = now
    try:
        resp = send_file(cached, mimetype="image/svg+xml")
    except FileNotFoundError:
        return "", 404  # never fetched successfully (or vanished between the check above
                         # and here) — same as an ordinary cache miss, not a hard error
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # never changes once cached
    return resp

# ── App icons (App Store catalog + installed tiles) ─────────────────────
# App Store icons ship inside the catalog package and are served from
# /api/appstore/icon/<id>. A tile may also use an https image URL typed in
# by an admin; the CSP's img-src is 'self' data:, so the browser can't load
# those directly. Same answer as the file-icon cache above: only the server
# fetches, from an exact host allowlist, once per URL, and the browser loads
# it same-origin.
_APP_ICON_HOSTS = {"cdn.jsdelivr.net", "raw.githubusercontent.com"}
_CATALOG_ICON_RE = re.compile(r"/api/appstore/icon/[a-z0-9][a-z0-9_-]{0,63}(\?v=\d{1,12})?")
_APP_ICON_MAX_BYTES = 512 * 1024
_APP_ICON_TYPES = {"image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpg",
                   "image/webp": "webp", "image/gif": "gif", "image/x-icon": "ico",
                   "image/vnd.microsoft.icon": "ico"}
APP_ICON_CACHE_DIR = ICON_CACHE_DIR / "apps"

def _sniff_icon_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if data[:4] == b"\x00\x00\x01\x00": return "image/x-icon"
    head = data[:300].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2000].lower()):
        return "image/svg+xml"
    return None

def _clean_tile_icon(v) -> str:
    """Tile icon = an emoji/short text, or an https image URL on the same
    allowlist /api/app-icon serves from. The old flat [:16] cut silently
    mangled App Store tiles' icon URLs the first time anyone hit Save in
    the Edit dialog."""
    v = str(v or "").strip()
    if _CATALOG_ICON_RE.fullmatch(v):
        return v
    try:
        pu = urlparse(v)
        if pu.scheme == "https" and pu.hostname in _APP_ICON_HOSTS and len(v) <= 512:
            return v
    except Exception:
        pass
    return v[:16] or "🔗"

_known_icons_cache = {"t": 0.0, "urls": frozenset()}
def _known_icon_urls() -> frozenset:
    """Only icons Void OS itself references (App Store catalog entries and
    installed tiles) may be fetched. jsDelivr mirrors every public GitHub
    repo, so a bare host allowlist still let any logged-in account make the
    server download and cache arbitrary files without limit."""
    if time.time() - _known_icons_cache["t"] > 60:
        urls = set()
        try:
            urls |= {str(e.get("icon")) for e in _load_catalog() if e.get("icon")}
        except Exception:
            pass
        try:
            urls |= {str(a.get("icon")) for a in _aload() if a.get("icon")}
        except Exception:
            pass
        _known_icons_cache.update(t=time.time(), urls=frozenset(urls))
    return _known_icons_cache["urls"]

@app.route("/api/app-icon")
@require_auth
def api_app_icon():
    import hashlib
    url = request.args.get("u", "")
    try:
        pu = urlparse(url)
    except Exception:
        return "", 404
    if pu.scheme != "https" or pu.hostname not in _APP_ICON_HOSTS or pu.port not in (None, 443) or pu.username:
        return "", 404
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
    if not any(APP_ICON_CACHE_DIR.glob(key + ".*")) and url not in _known_icon_urls():
        return "", 404
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
    APP_ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    hit = next(iter(APP_ICON_CACHE_DIR.glob(key + ".*")), None)
    if hit is None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "VoidOS-icon-cache"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                if urlparse(resp.geturl()).hostname not in _APP_ICON_HOSTS:
                    return "", 404  # redirected off the allowlist
                data = resp.read(_APP_ICON_MAX_BYTES + 1)
        except Exception:
            return "", 404
        ctype = _sniff_icon_type(data) if len(data) <= _APP_ICON_MAX_BYTES else None
        if not ctype:
            return "", 404  # type comes from the bytes themselves, never the remote header
        hit = APP_ICON_CACHE_DIR / f"{key}.{_APP_ICON_TYPES[ctype]}"
        tmp = APP_ICON_CACHE_DIR / f".{key}.tmp{os.getpid()}{threading.get_ident()}"
        try:
            tmp.write_bytes(data)
            winplat.replace_with_retry(tmp, hit)
        except OSError:
            try: tmp.unlink()
            except OSError: pass
            return Response(data, mimetype=ctype)
    ext = hit.suffix.lstrip(".")
    mime = next((m for m, e in _APP_ICON_TYPES.items() if e == ext), "application/octet-stream")
    resp = send_file(hit, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=604800"
    return resp

def _aload():
    with _apps_lock:
        if APPS_FILE.exists():
            try:
                data = json.loads(APPS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []
def _asave(d):
    with _apps_lock:
        _atomic_write_json(APPS_FILE, d)
    from modules.web import _broadcast_coalesced
    _broadcast_coalesced("apps", {"type": "apps_changed"})      # every open dashboard redraws its tiles


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _load_catalog
    from modules.app_store import _load_catalog
