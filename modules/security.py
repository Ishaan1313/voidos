"""
modules/security.py — First-run filesystem layout and the cross-site request guard.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import Response
from flask import jsonify
from flask import redirect
from flask import request
from urllib.parse import urlparse
import os
import json
from modules.config import (
    BLUETOOTH_FILE, CLOUD_SYNC_FILE, EXTERNAL_DIR, FILE_ROOT, LEGACY_LAYOUT_MAP, PORT, SHARED_DIR, SHARES_FILE,
    USERS_DIR, _void_path,
)
from modules.web import _atomic_write_json, app
from modules.activity import _audit
from modules.auth import _ip
from modules.users import _needs_onboarding, _users_load
from modules import consolelog, intercom


# ══════════════════════════════════════════════════════════════════════
# FILESYSTEM & FIRST-RUN INIT
# ══════════════════════════════════════════════════════════════════════
def _rewrite_layout_paths():
    """After the folders were renamed (home/shared -> Shared, home/personal -> Users,
    external -> Drives), fix the paths other files remember: share links, continuous-sync
    pairs, Recycle Bin records and the staging registry. Safe to run on every start."""
    if not LEGACY_LAYOUT_MAP:
        return
    base = str(FILE_ROOT)

    def fix_rel(s):
        if isinstance(s, str):
            for old, new in LEGACY_LAYOUT_MAP:
                if s == old or s.startswith(old + "/"):
                    return new + s[len(old):]
        return s

    def fix_abs(s):
        if isinstance(s, str):
            for old, new in LEGACY_LAYOUT_MAP:
                for sep in ("/", os.sep):
                    o = base + os.sep + old.replace("/", os.sep)
                    if s == o or s.startswith(o + sep):
                        return base + os.sep + new.replace("/", os.sep) + s[len(o):]
        return s

    def rewrite(path, keys=None, absolute=False):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        changed = False
        if absolute and isinstance(data, list):
            new = [fix_abs(x) for x in data]
            changed = new != data
            data = new
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for k in keys or ():
                        if k in item:
                            nv = fix_rel(item[k])
                            if nv != item[k]:
                                item[k] = nv
                                changed = True
        if changed:
            try:
                _atomic_write_json(path, data)
            except OSError:
                pass

    from modules.file_access import TRASH_META
    rewrite(SHARES_FILE, ("path",))
    rewrite(CLOUD_SYNC_FILE, ("local_path",))
    rewrite(TRASH_META, ("original_path",))
    rewrite(_void_path("VOID_STAGING_REGISTRY", "staging_registry.json"), absolute=True)

def _init_filesystem():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)  # mounted USB/SMB/NFS storage — see External Storage
    except OSError as e:                                 # never stop the server over this folder
        consolelog.warn(f"Couldn't create {EXTERNAL_DIR} ({e}); USB and network drives can't be mounted until it is fixed.", "files")
    _rewrite_layout_paths()
    readme = SHARED_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Shared folder\n"
            "─────────────\n"
            "This folder is accessible to all users.\n"
            "Admins and guests can read and write here.\n",
            encoding="utf-8",  # the "─" rule line isn't representable in cp1252 — this
                                # used to crash first launch on Windows
        )

def _needs_setup() -> bool:
    """True when no accounts exist yet — triggers the onboarding wizard
    at /setup instead of silently creating an admin with a console-only
    password."""
    return len(_users_load()) == 0

_init_filesystem()
if _needs_setup():
    consolelog.blank()
    consolelog.say(f"No accounts yet. Open http://localhost:{PORT}/setup in your browser to create the admin account.", "setup")


# Intercom: wire the module up to this process's own path/audit helpers (see
# modules/intercom.py's configure()), then say in one line whether it can run.
intercom.configure(BLUETOOTH_FILE, _atomic_write_json, _audit)
_ic = intercom.startup_summary()
if not _ic["ok"]:
    consolelog.warn("Intercom isn't ready on this host: " + (_ic["error"] or "no audio engine found."), "intercom")
    consolelog.say("The Intercom page opens and shows how to fix it.", "intercom")

# ── Cross-site request / WebSocket hijacking guard ──────────────────────
# The session cookie is SameSite=Lax, which stops *cross-site* POSTs from
# carrying it -- but not *same-site* ones. Any page on a sibling subdomain
# (another app you host at app.example.com while Void OS is at
# void.example.com -- exactly what named app tunnels set up) is "same
# site", so before this guard such a page could, with the user's cookie:
# POST to admin endpoints with a text/plain body (no CORS preflight), or
# open wss://void.example.com/terminal or /ws/intercom (WebSocket
# handshakes are never preflighted) and drive a shell or a live microphone
# after the user had unlocked them. Browsers mark every request with
# Sec-Fetch-Site; anything state-changing (or a WebSocket upgrade) that
# isn't same-origin is refused. Old browsers without Sec-Fetch-Site fall
# back to an Origin == Host comparison. Requests with neither header are
# non-browser clients (WebDAV, curl, scripts), which never carry ambient
# cookies and authenticate explicitly instead.
# VOID_ALLOWED_ORIGINS=https://a.example.com,https://b.example.com opts
# specific extra origins in (e.g. a separate front-end you trust).
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_EXTRA_ORIGINS = {o.strip().rstrip("/").lower()
                  for o in os.environ.get("VOID_ALLOWED_ORIGINS", "").split(",") if o.strip()}

def _own_origin(origin: str) -> bool:
    """True when `origin` is an address that serves THIS dashboard, learned from the setup itself so
    nobody has to list it in VOID_ALLOWED_ORIGINS:
      - the built-in Cloudflare tunnel address, and
      - the host the browser really asked for, when a proxy on this machine or Cloudflare (the only
        peers whose forwarding headers are believed) says so in X-Forwarded-Host. A reverse proxy or
        forwarder that rewrites Host is the usual reason a legitimate page looked cross-site.
    Per-app tunnel and custom-domain addresses are deliberately NOT trusted: they serve other apps, and
    a page on one of those is exactly the sibling-site case this guard exists to stop."""
    if not origin or origin == "null":
        return False
    from modules import state
    tun = (getattr(state, "TUNNEL_URL", None) or "").strip().rstrip("/").lower()
    if tun and origin == tun:
        return True
    xfh = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip().lower()
    if xfh and urlparse(origin).netloc == xfh:
        from modules.auth import _is_cloudflare_ip
        peer = request.remote_addr or ""
        if peer in ("127.0.0.1", "::1") or _is_cloudflare_ip(peer):
            return True
    return False


@app.before_request
def _cross_site_guard():
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    if request.method in _SAFE_METHODS and not is_ws:
        return
    origin = (request.headers.get("Origin") or "").strip().rstrip("/").lower()
    if origin and origin in _EXTRA_ORIGINS:
        return
    site = request.headers.get("Sec-Fetch-Site", "").lower()
    if site in ("same-origin", "none"):
        return
    # The browser itself says "another site / a sibling subdomain": never excused by the own-address rule.
    if site not in ("cross-site", "same-site") and _own_origin(origin):
        return
    if not site:
        if not origin:
            return  # non-browser client
        if origin != "null" and urlparse(origin).netloc == request.host.lower():
            return
    _audit("cross_site_blocked", None, target=request.path,
           detail=(f"{request.method} from {origin or site or 'unknown origin'} "
                   f"[host={request.host} site={site or '-'} xfh={request.headers.get('X-Forwarded-Host', '-')}]"),
           ip=_ip())
    if request.path.startswith("/api/") or is_ws:
        return jsonify({"error": "Cross-site request blocked"}), 403
    return Response("Cross-site request blocked", 403, mimetype="text/plain")

@app.before_request
def _force_setup():
    """While no accounts exist, funnel every request to the setup wizard
    instead of the (unusable) login page."""
    p = request.path
    if p.startswith("/static") or p == "/manifest.json" or p == "/api/health":
        return
    if _needs_setup():
        if p in ("/setup", "/api/setup", "/api/setup/status"):
            return
        if p.startswith("/api/") or p.startswith("/webdav"):
            return jsonify({"error": "setup_required"}), 503
        return redirect("/setup")
    elif p == "/setup" and not _needs_onboarding():
        return redirect("/login")
