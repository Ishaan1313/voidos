"""
modules/error_log.py — Error log with reference codes, friendly error responses, HTTP error handlers.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from collections import deque
from datetime import datetime
from datetime import timezone
from flask import g
from flask import jsonify
from flask import request
from itsdangerous import BadSignature
from itsdangerous import SignatureExpired
import json
import threading
import time
import uuid
from modules import errors, state, winplat
from modules.config import AUDIT_FILE, AUDIT_MAX_ENTRIES, COOKIE_SEC, ERROR_LOG_FILE, SESSION_HRS, SESSIONS_FILE
from modules.web import _COOKIE, _serial, _sessions_lock, app
from modules.activity import _audit
from modules.auth import (
    _cf_country, _describe_ua, _geolocate_and_update_session, _ip, _request_is_https, _sessions_load, _sessions_prune,
    _sessions_save,
)
from modules.users import _get_user_by_id


# ── Error reporting ──────────────────────────────────────────────────
# Every unexpected server error, and every frontend crash a browser reports,
# lands here with a short reference code. The user sees a plain-language
# message plus that code; an admin can paste the code into Admin → Error Log
# to see the real exception, route, user and traceback. Kept as a ring
# buffer in memory and mirrored to error_log.jsonl so it survives restarts.
_ERROR_LOG_MAX = 500
_error_log = deque(maxlen=_ERROR_LOG_MAX)
_error_log_lock = threading.Lock()

def _error_log_load():
    try:
        lines = ERROR_LOG_FILE.read_text(encoding="utf-8").splitlines()[-_ERROR_LOG_MAX:]
    except OSError:
        return
    for ln in lines:
        try:
            _error_log.append(json.loads(ln))
        except ValueError:
            pass

def _record_error(entry: dict) -> None:
    with _error_log_lock:
        _error_log.append(entry)
        try:
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            if ERROR_LOG_FILE.stat().st_size > 2 * 1024 * 1024:  # compact: keep only the in-memory window
                tmp = ERROR_LOG_FILE.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(x) + "\n" for x in _error_log), encoding="utf-8")
                winplat.replace_with_retry(tmp, ERROR_LOG_FILE)
        except OSError:
            pass

_error_log_load()

def _log_exception(e: BaseException, source: str = "server", context: str = "") -> str:
    """Records `e` (with traceback) and returns its reference code."""
    import traceback
    ref = uuid.uuid4().hex[:8]
    status, code, msg, _hint = errors.friendly(e)
    try:
        user = _current_user() if request else None
        route = f"{request.method} {request.path}" if request else ""
    except RuntimeError:  # outside a request (background thread)
        user, route = None, ""
    entry = {"ref": ref, "ts": time.time(), "source": source, "code": code, "status": status,
             "message": msg, "hint": _hint, "reason": errors.reason(e), "where": errors.where(e),
             "category": code, "severity": "error" if status >= 500 else "warning",
             "exception": errors.exc_summary(e), "route": route, "context": context[:300],
             "user": (user or {}).get("username", ""),
             "trace": "".join(traceback.format_exception(type(e), e, e.__traceback__))[-6000:]}
    _record_error(entry)
    app.logger.error(f"[{ref}] {entry['exception']} ({route or context})")
    return ref

def _err(e: Exception, code: int | None = None):
    """Error response for an exception: a specific, plain-language message
    (disk full, file in use, read-only drive, name not allowed...) plus a
    hint and a reference code — never the raw exception text, which can leak
    paths and internals. Expected, recoverable conditions (file gone, name
    taken) aren't written to the error log; real failures are."""
    status, ecode, msg, hint = errors.friendly(e)
    body = {"error": msg, "code": ecode}
    if hint:
        body["hint"] = hint
    if status >= 500 or ecode in ("os_permission", "internal"):
        body["ref"] = _log_exception(e)
    return jsonify(body), (code or status)

def _api_error(message: str, status: int = 400, code: str = "", hint: str = ""):
    """Hand-written error response with the same shape as _err()."""
    body = {"error": message}
    if code: body["code"] = code
    if hint: body["hint"] = hint
    return jsonify(body), status

def _audit_trim():
    """Caller must hold _audit_lock."""
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > AUDIT_MAX_ENTRIES:
            with open(AUDIT_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-AUDIT_MAX_ENTRIES:])
    except Exception:
        pass

def _drain_gate_audit():
    """Fold any new lines from gate_audit.ndjson (written by gate_proxy.py
    subprocesses — a separate process, so this is a file-based bridge rather
    than live IPC) into the main Activity Log. Called whenever an admin
    views the Activity Log, so a gate event shows up the next time anyone
    looks — good enough for visibility, not meant as real-time alerting.
    Best-effort throughout: a gate event that fails to drain is not worth
    breaking the audit log page over."""
    pass  # (shared value now lives in modules/state.py)
    try:
        if not GATE_AUDIT_FILE.exists():
            return
        with open(GATE_AUDIT_FILE, "r", encoding="utf-8") as f:
            f.seek(state._gate_audit_pos)
            new_lines = f.readlines()
            state._gate_audit_pos = f.tell()
        actions = {"success": "gate_unlock", "fail": "gate_fail", "locked_out": "gate_locked_out"}
        for line in new_lines:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            aid = ev.get("app_id", "?")
            result = ev.get("result", "?")
            _audit(actions.get(result, "gate_event"), user=None, target=f"app tile {aid}",
                   detail=f"gate PIN {result}", ip=ev.get("ip", ""))
    except Exception:
        pass

def _audit_read(limit: int = 200, action: str = "", username: str = "") -> list:
    if not AUDIT_FILE.exists(): return []
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line: continue
        try: entry = json.loads(line)
        except Exception: continue
        if action and entry.get("action") != action: continue
        if username and entry.get("username") != username: continue
        out.append(entry)
        if len(out) >= limit: break
    return out

_sid_cache = {"sig": None, "seen": {}}


def _sid_fresh(sid: str) -> bool:
    """True when `sid` is a live session whose last_seen was refreshed within the last minute, judged from a
    cached copy of sessions.json that is re-read only when the file changes (a revoke rewrites it, so it
    is noticed at once). Anything else takes the full path in _current_user."""
    try:
        st = SESSIONS_FILE.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return False
    c = _sid_cache
    if c["sig"] != sig:
        c["seen"] = {s.get("id"): s.get("last_seen_ts", 0) for s in _sessions_load() if s.get("id")}
        c["sig"] = sig
    t = c["seen"].get(sid)
    return t is not None and time.time() - t <= 60


def _current_user() -> dict | None:
    """Resolve authenticated user for this request (cached in Flask g)."""
    if hasattr(g, "_void_user"):
        return g._void_user
    tok = request.cookies.get(_COOKIE)
    if not tok:
        return None
    try:
        payload = _serial.loads(tok, max_age=SESSION_HRS * 3600)
        uid = payload.get("uid")
        sid = payload.get("sid")
        if not uid or not sid:
            # Pre-v3 cookies carried no session id, so "Sign out everywhere" and
            # session revocation could never reach them. Treat as logged out.
            return None
        if _sid_fresh(sid):
            g._void_sid = sid
            g._void_user = _get_user_by_id(uid)
            return g._void_user
        if sid:
            with _sessions_lock:
                sessions = _sessions_load()
                match = next((s for s in sessions if s.get("id") == sid), None)
                if not match:
                    return None  # revoked (or pruned) — force re-login
                g._void_sid = sid
                # Throttle writes: only touch last_seen if it's gone stale.
                if time.time() - match.get("last_seen_ts", 0) > 60:
                    match["last_seen_ts"] = time.time()
                    match["ip"] = _ip()
                    _sessions_save(sessions)
        user = _get_user_by_id(uid)
        g._void_user = user
        return user
    except (BadSignature, SignatureExpired):
        return None

def _set_session(resp, uid: str, client_info: dict | None = None):
    sid = uuid.uuid4().hex
    now = time.time()
    ua = request.headers.get("User-Agent", "")
    ip = _ip()
    ci = client_info if isinstance(client_info, dict) else {}
    # Whitelist + size-cap whatever the browser voluntarily reported — never
    # trust it for anything security-relevant, it's purely cosmetic detail
    # for the admin/superuser session-details view.
    safe_ci = {k: str(ci.get(k, ""))[:64] for k in
               ("timezone", "language", "platform", "screen", "viewport",
                "pixel_ratio", "touch", "color_depth")}
    with _sessions_lock:
        sessions = _sessions_prune(_sessions_load())
        sessions.append({
            "id": sid, "uid": uid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_at_ts": now, "last_seen_ts": now,
            "ip": ip, "user_agent": ua, "label": _describe_ua(ua),
            "country": "", "country_code": "",
            "client_info": safe_ci,
        })
        _sessions_save(sessions)
    resp.set_cookie(_COOKIE, _serial.dumps({"v": 3, "uid": uid, "sid": sid}),
        max_age=SESSION_HRS * 3600, httponly=True,
        secure=COOKIE_SEC or _request_is_https(), samesite="Lax")
    threading.Thread(target=_geolocate_and_update_session, args=(sid, ip, _cf_country()), daemon=True).start()

def _validate_ws_cookie(cookies: dict) -> dict | None:
    """Validate a session token parsed from raw WebSocket handshake cookies.
    Used by the custom WSGIRequestHandler that runs outside Flask's context."""
    tok = cookies.get(_COOKIE, "")
    if not tok:
        return None
    try:
        payload = _serial.loads(tok, max_age=SESSION_HRS * 3600)
        uid = payload.get("uid")
        sid = payload.get("sid")
        if not sid or not any(s.get("id") == sid for s in _sessions_load()):
            return None  # revoked, or a legacy cookie with no session id
        return _get_user_by_id(uid) if uid else None
    except (BadSignature, SignatureExpired):
        return None

def _has_full_file_access(user: dict) -> bool:
    """Superuser is always unrestricted. Admin is unrestricted unless the
    superuser has explicitly turned off their guest_file_view permission."""
    role = user.get("role")
    if role == "superuser":
        return True
    if role == "admin":
        return bool(user.get("permissions", {}).get("guest_file_view", True))
    return False

def _is_admin_level(user: dict) -> bool:
    """True for admin or superuser — the general 'admin panel access' gate.
    Feature-specific restrictions (docker/terminal/guest_file_view) are
    handled separately and only ever apply to the admin role."""
    return user.get("role") in ("admin", "superuser")

def _can_use_terminal(user: dict) -> bool:
    role = user.get("role")
    if role == "superuser":
        return True
    if role == "admin":
        return bool(user.get("permissions", {}).get("terminal", False))
    return bool(user.get("permissions", {}).get("terminal", False))

def _can_use_docker(user: dict) -> bool:
    role = user.get("role")
    if role == "superuser":
        return True
    if role == "admin":
        return bool(user.get("permissions", {}).get("docker", True))
    return False

def _can_use_intercom(user: dict) -> bool:
    """Microphone/speaker access over the internet is at least as
    sensitive as a shell — arguably more, since it's live audio into
    someone's home — so it gets the exact same per-user gating treatment
    as _can_use_terminal above, not just an admin-only gate (FR §7).
    Default False for both ranks; an Admin has to be explicitly granted
    it by the Super User, same as terminal."""
    role = user.get("role")
    if role == "superuser":
        return True
    return bool(user.get("permissions", {}).get("intercom", False))


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global GATE_AUDIT_FILE
    from modules.tunnels import GATE_AUDIT_FILE
