"""
modules/account_routes.py — Page rendering, setup wizard, account settings, notification settings, health and error-log routes.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from datetime import datetime
from datetime import timezone
from flask import g
from flask import jsonify
from flask import make_response
from flask import redirect
from flask import render_template
from flask import request
from flask import send_file
from itsdangerous import BadSignature
from itsdangerous import SignatureExpired
from pathlib import Path
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash
import json
import os
import re
import secrets
import threading
import time
import uuid
from modules import state, winplat
from modules.config import (
    APP_DATA_DIR, COOKIE_SEC, DRIVES_REL, ERROR_LOG_FILE, FILE_ROOT, GUEST_QUOTA_MB, MAX_UPLOAD, OS_NAME,
    SESSION_HRS, SHARED_REL, TERM_EN, USERS_REL, VOID_ROOT,
)
from modules.activity import (
    INAPP_FILE, MAJOR_EVENTS, _NOTIFY_DEFAULTS, _audit, _inapp_load, _inapp_lock,
    _notify_dispatch, _notify_load, _notify_lock, _notify_record, _notify_save,
    _notify_status_load,
)
from modules.users import (
    ENV_FILE, TERM_PW_HASH, VALID_BLOB_SPEEDS, VALID_TIME_FORMATS, VALID_THEMES, VALID_UI_MODES, WALLPAPER_PRESETS,
    _get_user_by_username, _get_user_settings, _mark_onboarding_complete,
    _mark_terminal_unlocked, _needs_onboarding, _resolve_ui_mode, _safe_user,
    _safe_wallpaper_filename, _save_user_settings, _server_settings_load, _server_settings_lock,
    _server_settings_save, _terminal_session_unlocked, _ui_mode_default, _user_wallpaper_dir,
    _users_save, _wallpaper_url, _write_env_updates,
)
from modules.web import (
    _COOKIE, _access_payload, _atomic_write_json, _broadcast_live, _end_live_for_sids, _serial, _sessions_lock, app,
)
from modules import twofactor as _twofa
from modules.error_log import (
    _can_use_docker, _can_use_intercom, _can_use_terminal, _current_user, _error_log,
    _has_full_file_access,
    _error_log_lock, _record_error, _set_session,
)
from modules.auth import (
    _acct_clear, _acct_fail, _acct_locked, _clear_fails, _fail, _ip, _is_locked, _password_ok,
    _sessions_load, _sessions_save,
)
from modules.security import _needs_setup
from modules.file_access import _quota_used_bytes
from modules.decorators import require_admin, require_auth, require_superuser


# ══════════════════════════════════════════════════════════════════════
# TEMPLATE HELPER
# ══════════════════════════════════════════════════════════════════════
# Size of each piece a large upload is sent in. Was documented (README,
# .env sample) but never actually read — the page hard-coded 8 MB. Capped
# below the per-request limit so a chunk can never be rejected as too large.
UPLOAD_CHUNK_MB = max(1, min(int(os.environ.get("VOID_UPLOAD_CHUNK_MB", "8") or 8), max(1, MAX_UPLOAD - 1)))

def _render_app():
    user = _current_user()
    safe = _safe_user(user)
    safe["settings"] = _get_user_settings(user)
    safe["settings"]["ui_mode_resolved"] = _resolve_ui_mode(safe["settings"])
    safe["settings"]["wallpaper_url"] = _wallpaper_url(user, safe["settings"]["wallpaper"])
    return render_template("index.html", view="app",
        os_name=OS_NAME, docker_ok=state.DOCKER_OK, docker_error=state.DOCKER_ERROR,
        file_root=str(FILE_ROOT), term_enabled=TERM_EN,
        current_user=safe,
        can_terminal=_can_use_terminal(user) and TERM_EN,   # no Terminal button when the whole server has it off
        can_docker=_can_use_docker(user),
        can_intercom=_can_use_intercom(user),
        full_files=_has_full_file_access(user),
        shared_path=SHARED_REL, users_path=USERS_REL, drives_path=DRIVES_REL,
        upload_chunk_mb=UPLOAD_CHUNK_MB)

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": OS_NAME,
        "short_name": OS_NAME,
        "description": "Multi-user self-hosted home server dashboard",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0a0a12",
        "theme_color": "#0a0a12",
        "orientation": "any",
        "icons": [
            {"src": "/static/logo-64.png",  "sizes": "64x64",   "type": "image/png"},
            {"src": "/static/logo-128.png", "sizes": "128x128", "type": "image/png"},
            {"src": "/static/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
        ],
    })

# ══════════════════════════════════════════════════════════════════════
# ROUTES — AUTH
# ══════════════════════════════════════════════════════════════════════
@app.route("/setup")
def setup_page():
    if _needs_setup():
        return render_template("index.html", view="setup", os_name=OS_NAME,
            current_user=None, can_terminal=False, can_intercom=False,
            docker_ok=False, docker_error="", file_root="", term_enabled=False)
    if _needs_onboarding():
        # Admin account already exists (created in a previous visit) but the
        # rest of the wizard (storage paths, terminal password, optional
        # integrations) wasn't finished — resume it, authenticated as
        # whichever superuser is currently logged in, rather than showing
        # login or silently skipping the remaining steps forever.
        user = _current_user()
        if not user or user.get("role") != "superuser":
            return redirect("/login")
        return render_template("index.html", view="onboarding", os_name=OS_NAME,
            current_user=_safe_user(user), can_terminal=False, can_intercom=False,
            docker_ok=False, docker_error="", file_root="", term_enabled=False)
    return redirect("/login")

@app.route("/api/setup", methods=["POST"])
def api_setup():
    if not _needs_setup():
        return jsonify({"error": "Setup has already been completed"}), 403
    d = request.get_json(silent=True) or {}
    uname = str(d.get("username", "")).strip().lower()
    pw    = str(d.get("password", ""))
    if not re.match(r"^[a-z0-9_-]{3,32}$", uname):
        return jsonify({"error": "Username must be 3-32 characters: letters, numbers, - or _"}), 400
    if len(pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not _needs_setup():  # race guard against a second concurrent submit
        return jsonify({"error": "Setup has already been completed"}), 403
    admin = {
        "id": str(uuid.uuid4()),
        "username": uname,
        "password_hash": generate_password_hash(pw),
        "role": "superuser",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _users_save([admin])
    resp = make_response(jsonify({"ok": True, "needs_onboarding": True}))
    _set_session(resp, admin["id"], d.get("client_info"))
    _audit("setup_completed", admin, target=admin["username"])
    return resp

# ── Onboarding wizard (Steps 2+: everything else .env can configure) ────
@app.route("/api/setup/status")
def api_setup_status():
    """No @require_admin — deliberately reachable before/without a session,
    same as /setup itself, since this is what the frontend's very first
    boot-time check calls to decide which of setup/onboarding/login/app to
    render. Only exposes booleans and non-secret current values, never
    anything from .env that's actually sensitive."""
    out = {"needs_setup": _needs_setup(), "needs_onboarding": _needs_onboarding()}
    _viewer = _current_user()
    # Open to everyone for the two booleans (the page asks before anyone is signed
    # in), but file paths, quotas and what is configured are for the Super User only.
    if not _needs_setup() and _viewer and _viewer.get("role") == "superuser":
        out["defaults"] = {
            "name": OS_NAME, "file_root": str(FILE_ROOT), "app_data_dir": str(APP_DATA_DIR),
            "guest_quota_mb": GUEST_QUOTA_MB, "max_upload_mb": MAX_UPLOAD,
            "terminal_enabled": TERM_EN, "terminal_password_set": bool(TERM_PW_HASH),
            "ui_mode_default": _ui_mode_default(), "cookie_secure": COOKIE_SEC,
            "dropbox_configured": _cloud_provider_config("dropbox") is not None,
            "gdrive_configured": _cloud_provider_config("gdrive") is not None,
        }
    return jsonify(out)

@app.route("/api/setup/configure", methods=["POST"])
@require_admin
def api_setup_configure():
    """Writes the wizard's remaining steps to .env. Super-User-only (this
    changes server-wide config, same gate as /api/server-settings), and
    deliberately not limited to only-during-onboarding — a superuser can
    come back and re-run this from Settings later too, same route."""
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can configure server settings"}), 403
    d = request.get_json(silent=True) or {}
    updates = {}

    def _set_if_present(key, env_key, cast=str):
        if key in d and d[key] not in (None, ""):
            try:
                updates[env_key] = str(cast(d[key]))
            except (TypeError, ValueError):
                pass

    _set_if_present("name", "VOID_NAME")
    _set_if_present("file_root", "VOID_FILE_ROOT")
    _set_if_present("app_data_dir", "VOID_APP_DATA_DIR")
    _set_if_present("guest_quota_mb", "VOID_GUEST_QUOTA_MB", int)
    _set_if_present("max_upload_mb", "VOID_MAX_UPLOAD_MB", int)
    if "terminal_enabled" in d:
        updates["VOID_TERMINAL_ENABLED"] = "true" if d["terminal_enabled"] else "false"
    if "cookie_secure" in d:
        updates["VOID_COOKIE_SECURE"] = "true" if d["cookie_secure"] else "false"
    if "ui_mode_default" in d and d["ui_mode_default"] in ("classic", "home"):
        updates["VOID_UI_MODE_DEFAULT"] = d["ui_mode_default"]
    term_pw = str(d.get("terminal_password", ""))
    if term_pw:
        if len(term_pw) < 6:
            return jsonify({"error": "Terminal password must be at least 6 characters"}), 400
        updates["VOID_TERMINAL_PASSWORD_HASH"] = generate_password_hash(term_pw)
    elif d.get("terminal_password_clear"):
        updates["VOID_TERMINAL_PASSWORD_HASH"] = None
    for provider_key, env_pair in (("dropbox", CLOUD_PROVIDER_ENV["dropbox"]),
                                    ("gdrive", CLOUD_PROVIDER_ENV["gdrive"])):
        cid = d.get(f"{provider_key}_client_id")
        csec = d.get(f"{provider_key}_client_secret")
        if cid:
            updates[env_pair[0]] = str(cid).strip()
        if csec:
            updates[env_pair[1]] = str(csec).strip()

    if updates:
        _write_env_updates(updates)
    _audit("onboarding_configured", user, detail=", ".join(sorted(updates.keys())))
    return jsonify({"ok": True, "written": sorted(updates.keys()), "restart_required": bool(updates)})

def _env_example_path() -> str:
    return str(VOID_ROOT / "first setup" / ".env.example")


@app.route("/api/setup/env-schema")
@require_admin
def api_setup_env_schema():
    """Every setting in "first setup/.env.example", grouped, with the value it has now.
    Super User only: it describes the server's configuration. Secrets are never sent back."""
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can view server settings"}), 403
    from modules import envfile, envschema
    schema = envschema.load(_env_example_path())
    for grp in schema["groups"]:
        for st in grp["settings"]:
            cur = envfile.get(ENV_FILE, st["key"])
            st["active"] = bool(cur)
            if st["secret"]:
                st["is_set"] = bool(cur)
                st["value"] = ""
            else:
                st["value"] = cur if cur != "" else st["default"]
    return jsonify(schema)


@app.route("/api/setup/env", methods=["POST"])
@require_admin
def api_setup_env():
    """Writes the settings the wizard changed. Only keys listed in the example file are accepted,
    each checked for its type. A value equal to the default of a setting that is not switched on
    is left alone, so the file keeps showing it as an untouched default."""
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can configure server settings"}), 403
    from modules import envfile, envschema
    known = envschema.flat(_env_example_path())
    values = (request.get_json(silent=True) or {}).get("values") or {}
    if not isinstance(values, dict):
        return jsonify({"error": "values must be an object"}), 400
    updates, errors = {}, []
    for key, raw in values.items():
        st = known.get(str(key))
        if st is None:
            errors.append(f"{key}: not a Void OS setting.")
            continue
        val, err = envschema.validate(st, raw)
        if err:
            errors.append(err)
            continue
        active = envfile.get(ENV_FILE, key)
        if not active and (val == st["default"] or (val == "" and not st["default"])):
            continue                                    # untouched default: stays commented out
        if active == val:
            continue
        if val == "" and not st["default"] and st["type"] == "text":
            updates[key] = None                         # cleared: remove the line
            continue
        updates[key] = val
    if errors:
        return jsonify({"error": " ".join(errors[:4])}), 400
    updates = {k: v for k, v in updates.items() if not (v is None and not envfile.get(ENV_FILE, k))}
    if updates:
        _write_env_updates(updates)
    _audit("onboarding_configured", user, detail=", ".join(sorted(updates.keys())))
    return jsonify({"ok": True, "written": sorted(updates.keys()), "restart_required": bool(updates)})

@app.route("/api/setup/finish", methods=["POST"])
@require_admin
def api_setup_finish():
    """Marks onboarding done — called whether the admin filled every step
    or hit 'Skip for now' on all of them; either way there's no reason to
    keep prompting once they've explicitly moved past it, and everything
    it would have configured stays changeable later from Settings anyway."""
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can finish setup"}), 403
    _mark_onboarding_complete()
    _audit("onboarding_completed", user)
    return jsonify({"ok": True})

@app.route("/api/admin/restart", methods=["POST"])
@require_admin
def api_admin_restart():
    """Exits the current process so a process manager (systemd's
    Restart=always, Docker's own restart policy, etc.) brings it back up
    reading a fresh .env — see README's "Performance & limits" section for how
    this is meant to be run. Started directly at a terminal (`python
    voidos.py`) with nothing supervising it, this just stops the process;
    the frontend's messaging says so explicitly rather than implying a
    guaranteed seamless restart this app has no way to promise across
    every possible way people run it."""
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can restart the server"}), 403
    _audit("server_restart_requested", user)
    # Restarts the whole server so a changed .env and any new code take
    # effect: stops Gunicorn's master, not just its worker, and — when nothing
    # supervises Void OS — first starts a fresh copy that takes over the port
    # (see winplat.restart_server / restart_mode).
    respawn = winplat.restart_mode() == "respawn"
    def _delayed_exit():
        time.sleep(0.5)  # let the HTTP response actually reach the browser first
        # stop helper processes explicitly first — they'd otherwise outlive us
        try: _cleanup_all_tunnels()
        except Exception: pass
        try:
            for _pid in list(_sync_supervisors.keys()): _sync_stop_supervisor(_pid)
        except Exception: pass
        winplat.restart_server(str(VOID_ROOT / "voidos.py"), str(VOID_ROOT))
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return jsonify({"ok": True, "respawn": respawn})

# ── Terminal password unlock ─────────────────────────────────────────────
@app.route("/api/terminal/lock-status")
@require_auth
def api_terminal_lock_status():
    sid = getattr(g, "_void_sid", None)
    return jsonify({"locked": bool(TERM_PW_HASH) and not _terminal_session_unlocked(sid)})

@app.route("/api/terminal/unlock", methods=["POST"])
@require_auth
def api_terminal_unlock():
    ip = _ip()
    locked, rem = _is_locked(ip)
    if locked:
        return jsonify({"ok": False, "error": "locked", "retry_after": rem}), 429
    if not TERM_PW_HASH:
        return jsonify({"ok": True})  # nothing configured — always unlocked
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if not check_password_hash(TERM_PW_HASH, pw):
        _fail(ip); time.sleep(0.3)
        _audit("terminal_unlock_failed", _current_user())
        return jsonify({"ok": False, "error": "invalid"}), 403  # signed in, wrong terminal password — not 401
    _clear_fails(ip)
    sid = getattr(g, "_void_sid", None)
    if sid:
        _mark_terminal_unlocked(sid)
    _audit("terminal_unlocked", _current_user())
    return jsonify({"ok": True})


@app.route("/login")
def login_page():
    if _current_user(): return redirect("/")
    return render_template("index.html", view="login", os_name=OS_NAME,
        current_user=None, can_terminal=False,
        docker_ok=False, docker_error="", file_root="", term_enabled=False)

@app.route("/auth", methods=["POST"])
def auth_check():
    ip = _ip(); locked, rem = _is_locked(ip)
    if locked: return jsonify({"ok":False,"error":"locked","retry_after":rem}), 429
    d = request.get_json(silent=True) or {}
    uname = str(d.get("username","")).strip().lower()
    pw    = str(d.get("password",""))
    if not uname or not pw:
        return jsonify({"ok":False,"error":"invalid"}), 401
    a_locked, a_rem = _acct_locked(uname)
    if a_locked:                      # too many wrong passwords for THIS account, from anywhere
        return jsonify({"ok":False,"error":"locked","retry_after":a_rem}), 429
    user = _get_user_by_username(uname)
    if not _password_ok(user, pw):
        _fail(ip); _acct_fail(uname); time.sleep(0.3)
        _audit("login_failed", None, target=uname)
        return jsonify({"ok":False,"error":"invalid"}), 401
    if _twofa.is_enabled(user):
        # Password was right; the second step is a code from the authenticator
        # app (or a backup code). Asking for it isn't a failure, a wrong one is.
        code = str(d.get("code", "")).strip()
        if not code:
            return jsonify({"ok":False,"error":"2fa_required"}), 401
        ok, why = _twofa.check_code(user["id"], code)
        if not ok:
            if why == "locked":
                return jsonify({"ok":False,"error":"2fa_locked","retry_after":_twofa.locked_for(user["id"])}), 429
            if why == "unreadable":
                return jsonify({"ok":False,"error":"2fa_unreadable"}), 401
            _fail(ip); time.sleep(0.3)
            _audit("login_failed", None, target=uname, detail="wrong two-factor code")
            return jsonify({"ok":False,"error":"2fa_invalid"}), 401
        if why == "backup":
            _audit("2fa_backup_used", user, target=user["username"],
                   detail=f"{_twofa.backup_remaining(_get_user_by_username(uname))} backup code(s) left")
    _clear_fails(ip); _acct_clear(uname)
    resp = make_response(jsonify({
        "ok":True,"role":user["role"],"username":user["username"]}))
    _set_session(resp, user["id"], d.get("client_info"))
    _audit("login", user)
    return resp

@app.route("/logout", methods=["GET", "POST"])
def logout():
    # Plain links keep working, but only from Void OS's own pages: a GET
    # from another site (an <img src=".../logout">, a link on a sibling
    # subdomain) used to sign the user out on demand.
    if request.method == "GET" and request.headers.get("Sec-Fetch-Site", "").lower() in ("cross-site", "same-site"):
        return redirect("/")
    user = _current_user()
    tok = request.cookies.get(_COOKIE)
    if tok:
        try:
            payload = _serial.loads(tok, max_age=SESSION_HRS * 3600)
            sid = payload.get("sid")
            if sid:
                with _sessions_lock:
                    _sessions_save([s for s in _sessions_load() if s.get("id") != sid])
                _end_live_for_sids([sid], "signed_out")   # this device's other tabs and shells end with it
        except (BadSignature, SignatureExpired):
            pass
    if user: _audit("logout", user)
    r = redirect("/login"); r.delete_cookie(_COOKIE); return r

@app.route("/")
@require_auth
def index(): return _render_app()

# ── Current user info ─────────────────────────────────────────────────
@app.route("/api/me")
@require_auth
def api_me():
    user = _current_user()
    safe = _safe_user(user)
    safe["settings"] = _get_user_settings(user)
    safe["settings"]["ui_mode_resolved"] = _resolve_ui_mode(safe["settings"])
    safe["settings"]["wallpaper_url"] = _wallpaper_url(user, safe["settings"]["wallpaper"])
    if user.get("role") == "guest" and GUEST_QUOTA_MB > 0:
        safe["quota"] = {"used_bytes": _quota_used_bytes(user), "cap_mb": GUEST_QUOTA_MB}
    return jsonify(safe)

@app.route("/api/me/access")
@require_auth
def api_me_access():
    """What this account may do right now. The page asks for it when its live channel (re)connects
    and every few seconds while open, so a change an admin made is never missed if a push was lost."""
    resp = jsonify(_access_payload(_current_user()))
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/me/settings", methods=["PATCH"])
@require_auth
def api_me_settings():
    user = _current_user()
    patch = request.get_json(silent=True) or {}
    # Only allow known, type-safe keys through
    clean = {}
    if "display_name" in patch:
        clean["display_name"] = str(patch["display_name"])[:64]
    if "show_blobs" in patch:
        clean["show_blobs"] = bool(patch["show_blobs"])
    if "sidebar_pinned" in patch:
        clean["sidebar_pinned"] = bool(patch["sidebar_pinned"])
    if "theme" in patch:
        t = str(patch["theme"])
        if t not in VALID_THEMES:
            return jsonify({"error": "Unknown theme"}), 400
        clean["theme"] = t
    if "blob_speed" in patch:
        s = str(patch["blob_speed"])
        if s not in VALID_BLOB_SPEEDS:
            return jsonify({"error": "Unknown blob speed"}), 400
        clean["blob_speed"] = s
    if "time_format" in patch:
        tf = str(patch["time_format"])
        if tf not in VALID_TIME_FORMATS:
            return jsonify({"error": "Time format must be 12 or 24"}), 400
        clean["time_format"] = tf
    if "ui_mode" in patch:
        m = str(patch["ui_mode"])
        if m not in VALID_UI_MODES:
            return jsonify({"error": "Unknown UI mode"}), 400
        clean["ui_mode"] = m
    if "wallpaper" in patch:
        w = str(patch["wallpaper"])
        if w and not (w.startswith("preset:") and w.split(":", 1)[1] in WALLPAPER_PRESETS) \
               and not (w.startswith("custom:") and _safe_wallpaper_filename(user, w.split(":", 1)[1])):
            return jsonify({"error": "Unknown wallpaper"}), 400
        clean["wallpaper"] = w
    if not clean:
        return jsonify({"error": "No valid settings provided"}), 400
    merged = _save_user_settings(user["id"], clean)
    if merged is None:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"ok": True, "settings": merged})

@app.route("/api/server-settings", methods=["GET"])
@require_auth
def api_server_settings_get():
    return jsonify({"ui_mode_default": _ui_mode_default()})

@app.route("/api/server-settings", methods=["PATCH"])
@require_auth
def api_server_settings_patch():
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can change the server-wide default"}), 403
    d = request.get_json(silent=True) or {}
    if "ui_mode_default" in d:
        m = str(d["ui_mode_default"])
        if m not in ("classic", "home"):
            return jsonify({"error": "ui_mode_default must be 'classic' or 'home'"}), 400
        with _server_settings_lock:
            cur = _server_settings_load()
            cur["ui_mode_default"] = m
            _server_settings_save(cur)
        _audit("server_settings_changed", user, detail=f"ui_mode_default={m}")
    _broadcast_live({"type": "server_settings_changed"})     # everyone on "auto" switches interface now
    return jsonify({"ok": True, "ui_mode_default": _ui_mode_default()})

# ── Notifications (email / Discord / Telegram) ──────────────────────────
@app.route("/api/admin/notifications", methods=["GET"])
@require_admin
def api_notify_get():
    cfg = _notify_load()
    # Never send secrets back down — same "has_X boolean, not the value
    # itself" treatment cloud storage tokens and network-share passwords
    # already get.
    safe = json.loads(json.dumps(cfg))
    safe["email"]["has_password"] = bool(safe["email"].pop("password", ""))
    safe["discord"]["has_webhook"] = bool(safe["discord"]["webhook_url"])
    if safe["discord"]["webhook_url"]:
        safe["discord"]["webhook_url"] = safe["discord"]["webhook_url"][:20] + "…"
    safe["telegram"]["has_token"] = bool(safe["telegram"].pop("bot_token", ""))
    safe["status"] = _notify_status_load()  # last delivery result per channel
    return jsonify(safe)

@app.route("/api/admin/notifications", methods=["PATCH"])
@require_admin
def api_notify_patch():
    """Super-User-only to write (matches server-wide UI-mode-default's own
    gate — this is server config, not a per-account preference), any Admin
    can read via GET above so they at least know what's configured."""
    user = _current_user()
    if user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can change notification settings"}), 403
    d = request.get_json(silent=True) or {}
    with _notify_lock:
        cfg = _notify_load()
        if "events" in d and isinstance(d["events"], dict):
            for k, v in d["events"].items():
                if k in MAJOR_EVENTS:
                    cfg["events"][k] = bool(v)
        for channel in ("email", "discord", "telegram"):
            if channel in d and isinstance(d[channel], dict):
                incoming = dict(d[channel])
                # blank password/token/webhook in the payload means "keep what's already saved" —
                # the GET response never contains the real secret to send back unmodified
                for secret_key in ("password", "webhook_url", "bot_token"):
                    if secret_key in incoming and not incoming[secret_key]:
                        incoming.pop(secret_key)
                cfg[channel].update(incoming)
        _notify_save(cfg)
    _audit("notify_settings_changed", user)
    return jsonify({"ok": True})

@app.route("/api/admin/notifications/test/<channel>", methods=["POST"])
@require_superuser
def api_notify_test(channel):
    if channel not in ("email", "discord", "telegram"):
        return jsonify({"error": "Unknown channel"}), 404
    cfg = _notify_load()
    if not cfg[channel]["enabled"]:
        return jsonify({"error": f"{channel.title()} isn't enabled"}), 400
    single = {"email": dict(_NOTIFY_DEFAULTS["email"]), "discord": dict(_NOTIFY_DEFAULTS["discord"]),
              "telegram": dict(_NOTIFY_DEFAULTS["telegram"])}
    single[channel] = cfg[channel]
    single["events"] = cfg["events"]
    result = _notify_dispatch(single, "\u2705 Void OS test notification",
        f"If you're reading this, {channel} notifications are working.")
    r = result.get(channel, {"ok": False, "message": "Not sent — fill in every field for this channel and save first."})
    if channel in result:
        _notify_record({channel: r}, "Test notification")
    if not r["ok"]:
        return jsonify({"ok": False, "error": r["message"], "message": r["message"], "code": f"{channel}_failed"}), 400
    return jsonify(r)

# ── In-app notification routes (feed lives near _maybe_notify above) ──
@app.route("/api/notifications")
@require_auth
def api_notifications():
    user = _current_user()
    _inapp_close_orphans(user["id"])
    with _inapp_lock:
        items = _inapp_load().get(user["id"], [])
    return jsonify({"items": items, "unread": sum(1 for x in items if not x.get("read"))})

def _inapp_close_orphans(uid: str) -> None:
    """A live "Installing..." item whose job is gone (Void OS restarted mid-install)
    would show a progress bar forever: turn it into a failure."""
    from modules.app_store import INSTALL_JOBS
    from modules.activity import _inapp_update
    with _inapp_lock:
        stale = [i["id"] for i in _inapp_load().get(uid, [])
                 if (i.get("data") or {}).get("ongoing")
                 and (INSTALL_JOBS.get((i.get("data") or {}).get("job_id")) or {}).get("done", True)]
    for nid in stale:
        _inapp_update(uid, nid, title="Installation was interrupted", level="error", read=False,
                      body="Void OS restarted before this finished. Check the app in the App Store and try again.",
                      data={"ongoing": False})

@app.route("/api/notifications/read", methods=["POST"])
@require_auth
def api_notifications_read():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    ids = set(d.get("ids") or [])
    with _inapp_lock:
        feed = _inapp_load()
        for x in feed.get(user["id"], []):
            if d.get("all") or x["id"] in ids:
                x["read"] = True
        _atomic_write_json(INAPP_FILE, feed)
    return jsonify({"ok": True})

@app.route("/api/notifications", methods=["DELETE"])
@require_auth
def api_notifications_clear():
    user = _current_user()
    nid = request.args.get("id")
    with _inapp_lock:
        feed = _inapp_load()
        # Clear removes everything except things still in progress; removing a single one by id always works.
        feed[user["id"]] = [x for x in feed.get(user["id"], [])
                            if ((x["id"] != nid) if nid else (x.get("data") or {}).get("ongoing"))]
        _atomic_write_json(INAPP_FILE, feed)
    return jsonify({"ok": True})

# ── Health probe (the page's "Trying to Reconnect" screen) ─────────────
@app.route("/api/health")
def api_health():
    """Answered by Void OS itself, so a reply here means the server — not
    just a proxy or tunnel in front of it — is up. Deliberately says nothing
    about the server beyond that, plus whether *this browser's* session is
    still valid (so the page can go to the login screen instead of
    reconnecting forever after a restart that changed the secret key)."""
    resp = jsonify({"ok": True, "authenticated": bool(_current_user())})
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ── Error Log (admin) + browser error reports ────────────────────────────
@app.route("/api/admin/errors")
@require_admin
def api_admin_errors():
    """Newest first. ?ref=abc123 looks one up (what a user reads off an
    error message); ?source=server|browser|job|notification filters."""
    ref = request.args.get("ref", "").strip().lower()
    source = request.args.get("source", "").strip()
    with _error_log_lock:
        items = list(_error_log)[::-1]
    if ref:
        items = [x for x in items if x.get("ref") == ref]
    if source:
        items = [x for x in items if x.get("source") == source]
    caller = _current_user()
    if caller.get("role") != "superuser":
        # Tracebacks can include paths and other users' data; Admins get the
        # summary, the Super User gets everything.
        items = [{k: v for k, v in x.items() if k != "trace"} for x in items]
    return jsonify({"items": items[:300], "total": len(items)})

@app.route("/api/admin/errors", methods=["DELETE"])
@require_superuser
def api_admin_errors_clear():
    with _error_log_lock:
        _error_log.clear()
        try: ERROR_LOG_FILE.unlink()
        except OSError: pass
    _audit("error_log_cleared", _current_user())
    return jsonify({"ok": True})

_client_err_times: dict = {}

@app.route("/api/client-error", methods=["POST"])
@require_auth
def api_client_error():
    """The browser reports its own crashes (uncaught exceptions, failed
    promise chains) so they're visible to admins instead of only in a
    devtools console nobody has open. Rate-limited per user; text-only."""
    user = _current_user()
    now = time.time()
    times = [t for t in _client_err_times.get(user["id"], []) if now - t < 300]
    if len(times) >= 20:
        return jsonify({"ok": False}), 429
    times.append(now); _client_err_times[user["id"]] = times
    d = request.get_json(silent=True) or {}
    ref = uuid.uuid4().hex[:8]
    from modules import errors as _errors
    why = _errors.explain_client_error(str(d.get("kind", "error"))[:30], str(d.get("message", ""))[:300],
                                       str(d.get("stack", ""))[:4000], str(d.get("view", ""))[:60],
                                       bool(d.get("stale")))
    recent = []
    for r in (d.get("requests") if isinstance(d.get("requests"), list) else [])[-8:]:
        if isinstance(r, dict):
            recent.append(f"{str(r.get('m', ''))[:8]} {str(r.get('u', ''))[:120]} -> {str(r.get('s', ''))[:6]} "
                          f"({str(r.get('t', ''))[:8]} ms, {str(r.get('ago', ''))[:8]} s ago)")
    _record_error({"ref": ref, "ts": now, "source": "browser", "code": why["category"], "status": 0,
                   "message": str(d.get("message", ""))[:300], "exception": str(d.get("kind", "error"))[:60],
                   "reason": why["reason"], "where": why["where"], "category": why["category"],
                   "severity": why["severity"], "recent": recent,
                   "build": str(d.get("build", ""))[:40], "ui": str(d.get("ui", ""))[:20],
                   "route": str(d.get("view", ""))[:60], "context": str(d.get("url", ""))[:200],
                   "user": user["username"], "trace": str(d.get("stack", ""))[:4000],
                   "agent": request.headers.get("User-Agent", "")[:200]})
    return jsonify({"ok": True, "ref": ref, "category": why["category"]})

# ── Wallpapers ───────────────────────────────────────────────────────────
ALLOWED_WALLPAPER_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_WALLPAPER_BYTES = 12 * 1024 * 1024
MAX_WALLPAPERS_PER_USER = 5

@app.route("/api/me/wallpaper/upload", methods=["POST"])
@require_auth
def api_wallpaper_upload():
    user = _current_user()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_WALLPAPER_EXT:
        return jsonify({"error": "Only JPG, PNG or WEBP images are supported"}), 400
    data = f.read(MAX_WALLPAPER_BYTES + 1)
    if len(data) > MAX_WALLPAPER_BYTES:
        return jsonify({"error": "Image too large (12MB max)"}), 400
    wdir = _user_wallpaper_dir(user)
    if sum(1 for x in wdir.iterdir() if x.is_file()) >= MAX_WALLPAPERS_PER_USER:
        return jsonify({"error": f"You can keep up to {MAX_WALLPAPERS_PER_USER} uploaded wallpapers — delete one first"}), 400
    fname = f"{secrets.token_hex(8)}{ext}"
    dest = wdir / fname
    dest.write_bytes(data)
    setting_val = f"custom:{fname}"
    _save_user_settings(user["id"], {"wallpaper": setting_val})
    _audit("wallpaper_uploaded", user, detail=fname)
    return jsonify({"ok": True, "wallpaper": setting_val, "url": f"/api/me/wallpaper/file/{fname}"})

@app.route("/api/me/wallpaper/file/<path:filename>")
@require_auth
def api_wallpaper_file(filename):
    user = _current_user()
    p = _safe_wallpaper_filename(user, filename)
    if not p:
        return "", 404
    resp = send_file(p)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp

@app.route("/api/me/wallpaper/<filename>", methods=["DELETE"])
@require_auth
def api_wallpaper_delete(filename):
    user = _current_user()
    p = _safe_wallpaper_filename(user, filename)
    if not p:
        return jsonify({"error": "Not found"}), 404
    p.unlink(missing_ok=True)
    settings = _get_user_settings(user)
    if settings.get("wallpaper") == f"custom:{filename}":
        _save_user_settings(user["id"], {"wallpaper": ""})
    return jsonify({"ok": True})


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global CLOUD_PROVIDER_ENV, _cleanup_all_tunnels, _cloud_provider_config
    global _sync_stop_supervisor, _sync_supervisors
    from modules.cloud_routes import (
        CLOUD_PROVIDER_ENV, _cloud_provider_config, _sync_stop_supervisor, _sync_supervisors,
    )
    from modules.tunnels import _cleanup_all_tunnels
