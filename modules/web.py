"""
modules/web.py — The Flask app, WebSocket support, live-session registry, security headers, HTTP error handlers.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import Flask
from flask import Response
from flask import jsonify
from flask import render_template
from flask import request
from flask_sock import Sock
from itsdangerous import URLSafeTimedSerializer
from pathlib import Path
import json
import os
import re
import tempfile
import threading
import time
import uuid
from modules.config import COOKIE_SEC, MAX_UPLOAD, SECRET_KEY, TERM_EN, VOID_ROOT
from modules import winplat


# ── Flask ─────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder=str(VOID_ROOT / "templates"), static_folder=str(VOID_ROOT / "static"))

# Cache-busting for the page's own CSS/JS: templates link them as
# /static/app.js?v={{ asset_v('app.js') }}. The value is a short hash of the
# file's contents (recomputed only when its modification time changes), so a
# browser can cache the files indefinitely yet always fetches the new ones
# after an update — new server code is never paired with stale JavaScript.
_asset_cache = {}
def asset_v(name: str) -> str:
    import hashlib
    path = VOID_ROOT / "static" / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "0"
    hit = _asset_cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _asset_cache[name] = (mtime, digest)
    return digest
app.jinja_env.globals["asset_v"] = asset_v
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True  # re-read index.html on change without a full restart

# Reverse-proxy header trust — OFF by default. Without this, request.url_root
# (used to build the Dropbox/Google Drive OAuth redirect URI, and anything
# else that infers scheme/host from the request) reports whatever scheme
# Void OS itself was actually reached on — which, sitting behind an
# HTTPS-terminating reverse proxy (nginx, Caddy, a Cloudflare Tunnel — the
# recommended production setup per this README's own "HTTPS / Public
# Access" section), is plain http, even though every real client is on
# https. That mismatch is exactly what breaks Dropbox/Google Drive OAuth
# with an "Invalid redirect_uri... must start with https://" error: Void
# OS was building and sending an http:// redirect URI without realizing
# the outside world never sees it that way.
# Blindly trusting X-Forwarded-* headers is a real trust-boundary change
# (anyone who can reach Void OS directly, bypassing the proxy, could spoof
# them) — same reasoning as every other explicit opt-in flag in this app
# (rclone/Bluetooth/PipeWire capability checks, mount privilege escalation).
# Only turn this on when Void OS is genuinely only reachable through your
# proxy, never directly.
if os.environ.get("VOID_TRUST_PROXY_HEADERS", "false").lower() == "true":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
sock = Sock(app)

# Requires a thread-capable WSGI server that flask-sock actually supports:
# Werkzeug's own dev server, or Gunicorn with the gthread worker class.
# Waitress does NOT support this (confirmed: no WebSocket hijacking hook),
# which is why this project runs under Gunicorn, not Waitress — see the
# "Performance & limits" section in README.md for the full reasoning.
_serial = URLSafeTimedSerializer(SECRET_KEY)
_share_serial = URLSafeTimedSerializer(SECRET_KEY, salt="void-share-v1")
_cloud_serial = URLSafeTimedSerializer(SECRET_KEY, salt="void-cloud-oauth-v1")
# Separate salt (not a separate key) is enough for itsdangerous to guarantee a token minted
# for one purpose fails signature verification under the other's loads() — cheap, real isolation.
_COOKIE = "void_sess"
_fails  = {}
_zip_jobs = {}  # job_id -> {processed,total,done,error,tmp_path,user,created} — multi-file download compression progress
_upload_sessions = {}  # upload_id -> {user_id,dest_dir,filename,declared_size,chunk_size,
                        #               total_chunks,received_chunks,bytes_received,tmp_path,created}
_upload_sessions_lock = threading.RLock()  # same read-modify-write discipline as the other in-memory job dicts —
                                            # a chunk request and a status-poll can land at the same instant

# ── Live-session registry ────────────────────────────────────────────
# Generic registry for any persistent per-user connection (currently: the
# /terminal WebSocket and the /ws/control push channel every browser tab
# opens). Exists to solve one problem: a normal HTTP request always sees
# fresh permissions (Flask re-checks on every request), but a long-lived
# connection made its access decision once, at connect time, and then just
# keeps running — revoking access afterwards updates the stored user
# record but does nothing to a socket that's already open. This registry
# is how an admin action (permission change, demotion, deletion, superuser
# transfer) reaches into an already-open tab/socket and enforces the
# change immediately instead of on next reload.
#
# user_id -> {live_session_id -> {"kind": "terminal"|"control", "kill": fn, "push": fn|None}}
_live_sessions = {}
_live_sessions_lock = threading.RLock()

def _register_live_session(user_id: str, kind: str, kill_fn, push_fn=None, sid: str | None = None) -> str:
    """`sid` is the login session this connection belongs to. It is what lets revoking ONE session
    (a device) end exactly that device's terminal / call / live channel, not only the account's."""
    lsid = uuid.uuid4().hex
    with _live_sessions_lock:
        _live_sessions.setdefault(user_id, {})[lsid] = {"kind": kind, "kill": kill_fn, "push": push_fn, "sid": sid}
    _ensure_live_sweeper()
    return lsid

def _unregister_live_session(user_id: str, lsid: str):
    with _live_sessions_lock:
        bucket = _live_sessions.get(user_id)
        if bucket is not None:
            bucket.pop(lsid, None)
            if not bucket:
                _live_sessions.pop(user_id, None)

def _kill_live_sessions(user_id: str, kinds=None):
    """Forcibly end matching live connections for this user right now —
    for terminal, this closes the socket and tears down the shell/pty the
    same way a clean disconnect already does (existing cleanup code, just
    triggered from outside instead of by the browser)."""
    with _live_sessions_lock:
        matches = [rec["kill"] for rec in _live_sessions.get(user_id, {}).values()
                   if kinds is None or rec["kind"] in kinds]
    for kill in matches:
        try: kill()
        except Exception: pass

def _push_to_live_sessions(user_id: str, message: dict, kinds=None):
    """Best-effort push to every matching open connection for this user —
    used to tell an already-loaded browser tab its permissions changed
    without waiting for its next request or a reload."""
    with _live_sessions_lock:
        matches = [rec["push"] for rec in _live_sessions.get(user_id, {}).values()
                   if rec.get("push") and (kinds is None or rec["kind"] in kinds)]
    for push in matches:
        try: push(message)
        except Exception: pass

def _end_live_for_sids(sids, reason: str = "session_revoked"):
    """A login session was revoked, signed out or has expired: tell the page on it and close every live
    connection it holds (terminal, intercom, live channel) right now, instead of when it next reconnects."""
    sids = {s for s in (sids or ()) if s}
    if not sids:
        return
    with _live_sessions_lock:
        hits = [(rec.get("push"), rec["kill"]) for bucket in _live_sessions.values() for rec in bucket.values()
                if rec.get("sid") in sids]
    for push, _ in hits:
        if push:
            try: push({"type": "force_logout", "reason": reason})
            except Exception: pass
    for _, kill in hits:
        try: kill()
        except Exception: pass


_sweeper_started = False
_sweeper_cache = {"sig": None, "ids": set(), "at": 0.0}

def _live_sweep_once():
    """Ends any live connection whose login session no longer exists. This is the safety net behind
    the direct calls in the revoke / sign-out / password-change routes: however a session disappears
    (another route, an expiry, a hand-edited sessions.json) the connection is gone within a couple of seconds."""
    with _live_sessions_lock:
        sids = {rec.get("sid") for bucket in _live_sessions.values() for rec in bucket.values() if rec.get("sid")}
    if not sids:
        return
    from modules.config import SESSIONS_FILE
    from modules.auth import _sessions_load
    try:
        st = SESSIONS_FILE.stat(); sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    c = _sweeper_cache
    now = time.time()
    if sig != c["sig"] or now - c["at"] > 15:
        c["ids"] = {s.get("id") for s in _sessions_load() if s.get("id")}
        c["sig"], c["at"] = sig, now
    gone = sids - c["ids"]
    if gone:
        _end_live_for_sids(gone)

def _live_sweeper_loop():
    while True:
        time.sleep(2)
        try: _live_sweep_once()
        except Exception: pass

def _ensure_live_sweeper():
    global _sweeper_started
    if _sweeper_started:
        return
    with _live_sessions_lock:
        if _sweeper_started:
            return
        _sweeper_started = True
    threading.Thread(target=_live_sweeper_loop, daemon=True, name="live-session-sweeper").start()


def _broadcast_live(message: dict, roles=None, kinds=("control",)):
    """Push `message` to every open page (or only those of accounts whose role is in `roles`).
    For changes that affect more than one person: a setting, a shared tile, a drive."""
    allowed = None
    if roles:
        try:
            from modules.users import _users_load
            allowed = {u.get("id") for u in _users_load() if u.get("role") in roles}
        except Exception:
            allowed = None
    with _live_sessions_lock:
        pushes = [rec["push"] for uid, bucket in _live_sessions.items() if allowed is None or uid in allowed
                  for rec in bucket.values() if rec.get("push") and rec["kind"] in kinds]
    for push in pushes:
        try: push(message)
        except Exception: pass


_coalesce_timers: dict = {}
_coalesce_lock = threading.Lock()

def _broadcast_coalesced(key: str, message: dict, roles=None, delay: float = 0.25):
    """Like _broadcast_live, but a burst of saves (a tunnel status flapping, a reorder) becomes one push."""
    with _coalesce_lock:
        if key in _coalesce_timers:
            return
        def _go():
            with _coalesce_lock:
                _coalesce_timers.pop(key, None)
            _broadcast_live(message, roles=roles)
        t = threading.Timer(delay, _go)
        t.daemon = True
        _coalesce_timers[key] = t
        t.start()


# Background work started by an account (copies, zips) runs with the access it had when it began.
# These hooks are how a change of that account's access reaches work that is already running.
_job_cancellers: list = []
_last_access: dict = {}

def _register_job_canceller(fn):
    _job_cancellers.append(fn)

def _cancel_user_jobs(user_id: str, why: str = "Stopped because your access changed."):
    for fn in list(_job_cancellers):
        try: fn(user_id, why)
        except Exception: pass


def _resync_all_live_users():
    """After the user list is replaced wholesale (a backup import): every open page gets its account's
    current access, and the pages of accounts that no longer exist are signed out."""
    from modules.users import _get_user_by_id
    with _live_sessions_lock:
        uids = list(_live_sessions.keys())
    for uid in uids:
        u = _get_user_by_id(uid)
        if u is None:
            _notify_permissions_changed(uid, force_logout=True, reason="account_deleted")
        else:
            _notify_permissions_changed(uid, u)


def _root_state() -> dict:
    try:
        from modules import rootguard
        s = rootguard.status()
        return {"ok": s["ok"], "reason": s["reason"], "path": s["path"]}
    except Exception:
        return {"ok": True, "reason": "", "path": ""}


def _access_payload(user_obj: dict | None) -> dict:
    """What the page needs to draw the right navigation and controls for this account. One definition,
    used for the live push and for the page's own re-check, so the two can never disagree."""
    role = (user_obj or {}).get("role")
    return {
        "role": role,
        "can_terminal": bool((_can_use_terminal(user_obj) if user_obj else False) and TERM_EN),
        "full_files": _has_full_file_access(user_obj) if user_obj else False,
        "can_docker": _can_use_docker(user_obj) if user_obj else False,
        "can_intercom": _can_use_intercom(user_obj) if user_obj else False,
        "can_admin": role in ("admin", "superuser"),
        "is_superuser": role == "superuser",
        "root": _root_state(),
    }


def _notify_permissions_changed(user_id: str, user_obj: dict | None = None,
                                 force_logout: bool = False, reason: str = ""):
    """Called from every endpoint that can change what a user is allowed
    to do: the permissions PATCH, promote/demote, delete-user, and
    superuser-transfer. Pushes a live update to any open tab (so nav/tabs
    update without a reload) and kills any live connection this user no
    longer qualifies for (currently: terminal) — right now, not next time
    they happen to reconnect."""
    if force_logout:
        _push_to_live_sessions(user_id, {"type": "force_logout", "reason": reason})
        _kill_live_sessions(user_id)  # every kind — terminal and control both
        _cancel_user_jobs(user_id, "Stopped because you were signed out.")
        _last_access.pop(user_id, None)
        return
    payload = _access_payload(user_obj)
    scope = (payload["role"], payload["full_files"])
    if _last_access.get(user_id) != scope:
        # what this account may read or write changed: copies and zips already running stop now
        _cancel_user_jobs(user_id)
    _last_access[user_id] = scope
    can_term = payload["can_terminal"]
    can_intercom = payload["can_intercom"]
    _push_to_live_sessions(user_id, {"type": "permissions_changed", **payload}, kinds=["control"])
    if not can_term:
        _kill_live_sessions(user_id, kinds=["terminal"])
    if not can_intercom:
        # Cuts a live call immediately if the flag is revoked mid-session —
        # a real requirement per the FR (§8), not hypothetical: an admin
        # who suspects a call session is being misused needs to be able to
        # end it right now, not wait for the browser tab to close.
        _kill_live_sessions(user_id, kinds=["intercom"])

def _count_live_sessions(kind: str, user_id: str | None = None) -> int:
    """Used by the terminal concurrency caps below — counts real, currently-
    open connections (not stored job records), so this is always exact."""
    with _live_sessions_lock:
        if user_id is not None:
            return sum(1 for rec in _live_sessions.get(user_id, {}).values() if rec["kind"] == kind)
        return sum(1 for bucket in _live_sessions.values() for rec in bucket.values() if rec["kind"] == kind)

# Guards the read-modify-write cycle for each JSON store, so two requests
# mutating the same store at the same instant can't race and silently drop
# one of the changes (e.g. two permission edits landing at once). RLock
# since a couple of routes call helper functions that also take the lock.
_users_lock    = threading.RLock()
_sessions_lock = threading.RLock()
_trash_lock    = threading.RLock()
_audit_lock    = threading.RLock()
_apps_lock     = threading.RLock()  # apps.json — was unlocked/non-atomic before multi-tunnel routing; two admins
                                     # adding/editing/deleting tiles at once (or a tunnel-status update racing an
                                     # edit) could otherwise drop one write. Same class of bug already fixed for
                                     # users/sessions/trash/audit — see HANDOFF.md §2.
_shares_lock   = threading.RLock()  # shares.json (public share links) — same read-modify-write discipline from day one
_stacks_lock   = threading.RLock()  # app_stacks.json (App Store installs) — same discipline from day one

def _atomic_write_json(path: Path, data, private: bool = False) -> None:
    """Write via a temp file + os.replace so a crash mid-write can never
    leave the real file half-written/corrupted. private=True makes the file
    readable by the owner only (secrets such as cloud login tokens)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if private:
            winplat.restrict_to_owner(tmp)  # before the swap, so the final file never exists with wider access
        winplat.replace_with_retry(tmp, path)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise

# ── Security headers ──────────────────────────────────────────────────
@app.after_request
def sec_hdrs(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"]        = "SAMEORIGIN"
    r.headers["X-XSS-Protection"]       = "1; mode=block"
    r.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    # The whole frontend is one inline <script>/<style> block (no external
    # app JS/CSS files), so 'unsafe-inline' is a pragmatic reality here, not
    # full nonce-based hardening — that would need a much larger template
    # refactor. Even with 'unsafe-inline', this still meaningfully blocks the
    # common XSS follow-through moves: pulling in a second-stage payload from
    # an attacker-controlled domain (script-src has an explicit allowlist),
    # or exfiltrating data to one (connect-src/img-src restricted to 'self').
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src 'self' https://www.youtube-nocookie.com; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    if COOKIE_SEC:
        # Only emitted when VOID_COOKIE_SECURE=true — i.e. the operator has
        # told us this deployment runs behind HTTPS. Emitting it unconditionally
        # would break plain-HTTP/localhost setups that haven't opted into
        # HTTPS-only mode (a browser that gets this header will refuse to
        # connect over HTTP at all for the next year).
        r.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/app-icon") or request.path.startswith("/api/file-icon"):
        # Third-party SVGs served same-origin: if someone opens one directly
        # (not via <img>, where scripts never run anyway), a sandboxed,
        # script-less policy stops any embedded <script> from running with
        # this origin's cookies.
        r.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
    if _serves_user_content(request.path) and r.mimetype not in ("application/json", "application/pdf"):
        # User-uploaded bytes served from THIS origin. The page CSP above
        # allows inline script, so an uploaded .svg (served inline as an
        # image) or .html (served inline over WebDAV GET) opened directly in
        # a tab ran with the viewer's session -- a guest could plant one in
        # Shared and take over an admin who clicked it. A sandboxed, script-
        # less policy keeps every legitimate use working (<img>/<video>/
        # <audio> src, downloads) while making such a page inert. PDFs are
        # exempt: Chrome refuses to render a PDF inside a sandboxed document.
        r.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'")
        r.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.path in ("/login", "/auth", "/logout"):
        r.headers["Cache-Control"] = "no-store"
    if request.path.startswith("/s/"):
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
    return r

_SHARE_DL_RE = re.compile(r"^/s/[^/]+/download$")
def _serves_user_content(path: str) -> bool:
    return (path in ("/api/file", "/api/file/download")
            or path == WEBDAV_PREFIX or path.startswith(WEBDAV_PREFIX + "/")
            or bool(_SHARE_DL_RE.match(path)))

@app.errorhandler(413)
def too_large(_):
    return _api_error(f"That's larger than the {MAX_UPLOAD} MB upload limit.", 413, "too_large",
                      "Use the regular upload (it sends large files in pieces) or ask the server owner to "
                      "raise VOID_MAX_UPLOAD_MB.")

_HTTP_MESSAGES = {
    400: ("bad_request", "The request was malformed.", "Reload the page and try again."),
    401: ("unauthorized", "You're not signed in (or your session expired).", "Sign in again."),
    403: ("forbidden", "You don't have permission to do that.", ""),
    405: ("method_not_allowed", "That action isn't supported here.", "Reload the page — it may be out of date."),
    409: ("conflict", "That conflicts with the current state.", "Refresh and try again."),
    429: ("rate_limited", "Too many requests.", "Wait a moment and try again."),
}

from werkzeug.exceptions import HTTPException as _HTTPException

@app.errorhandler(_HTTPException)
def _http_error(e):
    if not request.path.startswith("/api/") or e.code == 404:
        return e if e.code != 404 else not_found(e)
    code, msg, hint = _HTTP_MESSAGES.get(e.code, ("http_error", e.description or e.name, ""))
    return _api_error(msg, e.code, code, hint)

@app.errorhandler(Exception)
def _unhandled(e):
    """Last line of defence: a bug anywhere in a route now produces a JSON
    error with a reference code (and an Error Log entry) instead of
    Werkzeug's HTML 500 page — which the frontend couldn't parse, so the
    action simply failed with no message at all."""
    if isinstance(e, _HTTPException):
        return _http_error(e)
    if request.path.startswith("/api/"):
        return _err(e, 500)
    ref = _log_exception(e)
    return Response(f"Something went wrong on the server (reference {ref}).", 500, mimetype="text/plain")

@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    # The 404 page is always the purple Void OS look (it carries its own colours).
    return render_template("404.html"), 404


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global WEBDAV_PREFIX, _api_error, _can_use_docker, _can_use_intercom, _can_use_terminal
    global _current_user, _err, _get_user_settings, _log_exception, _has_full_file_access
    from modules.webdav import WEBDAV_PREFIX
    from modules.error_log import (
        _api_error, _can_use_docker, _can_use_intercom, _can_use_terminal, _current_user, _err,
        _has_full_file_access, _log_exception,
    )
    from modules.users import _get_user_settings
