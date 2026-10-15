"""
modules/admin_routes.py — Passwords, sessions, user management, backup & restore, activity-log routes.

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
from flask import request
from flask import send_file
from pathlib import Path
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash
import io
import json
import re
import shutil
import uuid
import zipfile
from modules.config import (
    APPS_FILE, AUDIT_FILE, AUDIT_MAX_ENTRIES, SESSIONS_FILE, SHARES_FILE, STACKS_FILE, USERS_DIR,
    USERS_FILE,
)
from modules.web import (
    _apps_lock, _atomic_write_json, _audit_lock, _end_live_for_sids, _notify_permissions_changed, _resync_all_live_users,
    _sessions_lock,
    _shares_lock, _stacks_lock, _users_lock, app,
)
from modules.activity import _audit
from modules.error_log import (
    _audit_read, _can_use_intercom, _current_user, _drain_gate_audit, _is_admin_level,
)
from modules.auth import (
    _clear_fails, _fail, _ip, _is_locked, _parse_user_agent, _sessions_load, _sessions_prune,
    _sessions_save,
)
from modules.users import _safe_user, _users_load, _users_save
from modules.account_routes import manifest
from modules.decorators import require_admin, require_auth, require_superuser
from modules import winplat


def _revoke_user_sessions(uid: str, keep_sid: str | None = None) -> int:
    """Drop every stored session for `uid` (optionally keeping one). A
    password change/reset has to do this: before, a stolen session cookie
    kept working after the victim -- or an admin -- changed the password."""
    with _sessions_lock:
        sessions = _sessions_load()
        keep = [x for x in sessions if x.get("uid") != uid or (keep_sid and x.get("id") == keep_sid)]
        dropped = [x.get("id") for x in sessions if x not in keep]
        if len(keep) != len(sessions):
            _sessions_save(keep)
    _end_live_for_sids(dropped, "session_revoked")
    return len(dropped)

@app.route("/api/me/password", methods=["PATCH"])
@require_auth
def api_me_password():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    old_pw = str(d.get("old_password",""))
    new_pw = str(d.get("new_password",""))
    ip = _ip(); locked, rem = _is_locked(ip)
    if locked: return jsonify({"error":f"Too many attempts — try again in {rem}s"}), 429
    if not check_password_hash(user.get("password_hash",""), old_pw):
        # Counted toward the login lockout: otherwise anyone holding a
        # hijacked session could brute-force the real password here freely.
        _fail(ip)
        return jsonify({"error":"Current password is incorrect"}), 403  # 401 would send the page to the login screen
    _clear_fails(ip)
    if len(new_pw) < 8:
        return jsonify({"error":"Password must be at least 8 characters"}), 400
    with _users_lock:
        users = _users_load()
        for i, u in enumerate(users):
            if u["id"] == user["id"]:
                users[i]["password_hash"] = generate_password_hash(new_pw)
                _users_save(users)
                _audit("password_changed", user, target=user["username"])
                n = _revoke_user_sessions(user["id"], keep_sid=getattr(g, "_void_sid", None))
                return jsonify({"ok":True, "other_sessions_signed_out": n})
    return jsonify({"error":"User not found"}), 404

# ══════════════════════════════════════════════════════════════════════
# ROUTES — SESSIONS (self-service view/revoke; admins see every user's)
# ══════════════════════════════════════════════════════════════════════
def _can_view_session_details(viewer: dict, owner: dict) -> bool:
    """Who can open the full session-details view for a given session's
    owner. Guests never can, not even for their own session (they keep the
    existing inline-only view). Admins can see their own session plus any
    Guest's. The Super User can see everyone's."""
    if not _is_admin_level(viewer):
        return False
    if viewer["id"] == owner["id"]:
        return True
    if viewer.get("role") == "superuser":
        return True
    if viewer.get("role") == "admin":
        return owner.get("role") == "guest"
    return False

@app.route("/api/sessions")
@require_auth
def api_sessions_list():
    user = _current_user()
    is_admin = _is_admin_level(user)
    cur_sid = getattr(g, "_void_sid", None)
    sessions = _sessions_prune(_sessions_load())
    all_users = _users_load()
    usernames = {u["id"]: u.get("username", "?") for u in all_users}
    roles     = {u["id"]: u.get("role", "") for u in all_users}
    # Same visibility as /details and DELETE below: the Super User sees all, an
    # Admin sees their own plus Guests' (never another Admin's or the Super
    # User's IP / location), a Guest sees only their own.
    if user.get("role") == "superuser":
        relevant = sessions
    elif is_admin:
        relevant = [s for s in sessions if s.get("uid") == user["id"] or roles.get(s.get("uid")) == "guest"]
    else:
        relevant = [s for s in sessions if s.get("uid") == user["id"]]
    relevant.sort(key=lambda s: s.get("last_seen_ts", 0), reverse=True)
    out = [{
        "id": s["id"],
        "username": usernames.get(s.get("uid"), "?"),
        "owner_role": roles.get(s.get("uid"), ""),
        "label": s.get("label", "Unknown device"),
        "ip": s.get("ip", ""),
        "country": s.get("country", ""),
        "country_code": s.get("country_code", ""),
        "created_at": s.get("created_at", ""),
        "last_seen_ts": s.get("last_seen_ts", 0),
        "is_current": s["id"] == cur_sid,
        "is_mine": s.get("uid") == user["id"],
    } for s in relevant]
    return jsonify({"sessions": out, "is_admin": is_admin})

@app.route("/api/sessions/<sid>/details")
@require_auth
def api_session_details(sid):
    viewer = _current_user()
    session = next((s for s in _sessions_load() if s.get("id") == sid), None)
    if not session:
        return jsonify({"error": "Not found"}), 404
    owner = next((u for u in _users_load() if u["id"] == session.get("uid")), None)
    if not owner or not _can_view_session_details(viewer, owner):
        # 404 rather than 403 for a disallowed target — doesn't confirm to an
        # unauthorized viewer (e.g. an Admin probing another Admin's session
        # id) that the session even exists.
        return jsonify({"error": "Not found"}), 404
    ua = session.get("user_agent", "")
    parsed = _parse_user_agent(ua)
    return jsonify({
        "id": session["id"],
        "username": owner.get("username", "?"),
        "role": owner.get("role", ""),
        "ip": session.get("ip", ""),
        "country": session.get("country", ""),
        "country_code": session.get("country_code", ""),
        "user_agent": ua,
        "browser": parsed["browser"], "browser_version": parsed["browser_version"],
        "os": parsed["os"], "os_version": parsed["os_version"],
        "engine": parsed["engine"], "device_type": parsed["device_type"],
        "client_info": session.get("client_info", {}),
        "created_at": session.get("created_at", ""),
        "last_seen_ts": session.get("last_seen_ts", 0),
    })

@app.route("/api/sessions/<sid>", methods=["DELETE"])
@require_auth
def api_sessions_revoke(sid):
    user = _current_user()
    with _sessions_lock:
        sessions = _sessions_load()
        target = next((s for s in sessions if s.get("id") == sid), None)
        if not target:
            return jsonify({"error": "Not found"}), 404
        if target.get("uid") != user["id"]:
            if user.get("role") == "superuser":
                pass  # Super User can revoke anyone
            elif user.get("role") == "admin":
                owner = next((u for u in _users_load() if u["id"] == target.get("uid")), None)
                if not owner or owner.get("role") != "guest":
                    return jsonify({"error": "Not found"}), 404
            else:
                return jsonify({"error": "Not found"}), 404
        _sessions_save([s for s in sessions if s.get("id") != sid])
    _end_live_for_sids([sid], "session_revoked")      # the device's terminal / call / page, right now
    usernames = {u["id"]: u.get("username", "?") for u in _users_load()}
    _audit("session_revoked", user, target=usernames.get(target.get("uid"), "?"))
    return jsonify({"ok": True})

@app.route("/api/sessions/revoke-others", methods=["POST"])
@require_auth
def api_sessions_revoke_others():
    user = _current_user()
    cur_sid = getattr(g, "_void_sid", None)
    with _sessions_lock:
        sessions = _sessions_load()
        kept = [s for s in sessions if not (s.get("uid") == user["id"] and s.get("id") != cur_sid)]
        revoked = len(sessions) - len(kept)
        dropped = [s.get("id") for s in sessions if s not in kept]
        _sessions_save(kept)
    _end_live_for_sids(dropped, "session_revoked")
    if revoked: _audit("sessions_revoked_others", user, target=user["username"], detail=f"{revoked} session(s)")
    return jsonify({"ok": True, "revoked": revoked})


@app.route("/api/users", methods=["GET"])
@require_admin
def api_users_list():
    return jsonify([_safe_user(u) for u in _users_load()])

@app.route("/api/users", methods=["POST"])
@require_admin
def api_users_create():
    caller = _current_user()
    d = request.get_json(silent=True) or {}
    uname = str(d.get("username","")).strip().lower()
    pw    = str(d.get("password",""))
    role  = str(d.get("role","guest"))
    if role == "superuser":
        return jsonify({"error":"A Super User account can only be created via setup or the transfer flow"}), 400
    if role == "admin" and caller.get("role") != "superuser":
        return jsonify({"error":"Only the Super User can create Admin accounts"}), 403
    if not uname or not pw:
        return jsonify({"error":"username and password are required"}), 400
    if len(pw) < 8:
        return jsonify({"error":"Password must be at least 8 characters"}), 400
    if role not in ("admin","guest"):
        return jsonify({"error":"role must be admin or guest"}), 400
    if not re.fullmatch(r"[a-z_][a-z0-9_]{2,31}", uname):
        return jsonify({"error":"Username must be 3-32 characters: lowercase letters, digits and underscores, not starting with a digit"}), 400
    if winplat.win_reserved(uname):
        # Also blocked off Windows: a users.json moved to a Windows host later
        # would otherwise end up with a personal folder named after a device.
        return jsonify({"error":"That username is reserved — pick another"}), 400
    with _users_lock:
        if any(u["username"] == uname for u in _users_load()):
            return jsonify({"error":"Username already exists"}), 409
        uid = str(uuid.uuid4())
        user: dict = {
            "id": uid, "username": uname,
            "password_hash": generate_password_hash(pw),
            "role": role,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if role == "guest":
            user["permissions"] = {"terminal": False, "intercom": False}
            user["personal_folder"] = uname
            (USERS_DIR / uname).mkdir(parents=True, exist_ok=True)
        elif role == "admin":
            user["permissions"] = {"terminal": False, "docker": False, "guest_file_view": True, "intercom": False}
        users = _users_load(); users.append(user); _users_save(users)
    _audit("user_created", caller, target=uname, detail=f"role={role}")
    return jsonify(_safe_user(user)), 201

def _purge_user_leftovers(uid: str) -> None:
    """A deleted account must not leave anything alive that acts in its name:
    its public share links (still downloadable by anyone with the URL), its
    continuous cloud-sync pairs (rclone kept syncing with the saved token),
    and its stored sign-ins."""
    with _shares_lock:
        try:
            shares = json.loads(SHARES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            shares = None
        if isinstance(shares, list):
            keep = [s for s in shares if s.get("owner_id") != uid]
            if len(keep) != len(shares):
                _atomic_write_json(SHARES_FILE, keep)
    from modules.cloud_routes import _sync_lock, _sync_pairs_load, _sync_pairs_save, _sync_stop_supervisor
    with _sync_lock:
        pairs = _sync_pairs_load()
        mine = [p for p in pairs if p.get("user_id") == uid]
        if mine:
            _sync_pairs_save([p for p in pairs if p.get("user_id") != uid])
    for p in mine:
        _sync_stop_supervisor(p["id"])
    _revoke_user_sessions(uid)

@app.route("/api/users/<uid>", methods=["DELETE"])
@require_admin
def api_users_delete(uid):
    me = _current_user()
    if me["id"] == uid:
        return jsonify({"error":"You cannot delete your own account"}), 400
    with _users_lock:
        users = _users_load()
        target = next((u for u in users if u["id"] == uid), None)
        if not target:
            return jsonify({"error":"User not found"}), 404
        if target.get("role") == "superuser":
            return jsonify({"error":"The Super User cannot be deleted — transfer the title to an Admin first"}), 400
        if target.get("role") == "admin" and me.get("role") != "superuser":
            return jsonify({"error":"Only the Super User can delete Admin accounts"}), 403
        _users_save([u for u in users if u["id"] != uid])
    _purge_user_leftovers(uid)
    _audit("user_deleted", me, target=target["username"])
    _notify_permissions_changed(uid, force_logout=True, reason="account_deleted")
    return jsonify({"ok":True})

@app.route("/api/users/<uid>/permissions", methods=["PATCH"])
@require_admin
def api_users_permissions(uid):
    caller = _current_user()
    d = request.get_json(silent=True) or {}
    with _users_lock:
        users = _users_load()
        for i, u in enumerate(users):
            if u["id"] == uid:
                if u["role"] == "superuser":
                    return jsonify({"error":"The Super User is never restricted by permission flags"}), 400
                if u["role"] == "admin":
                    if caller.get("role") != "superuser":
                        return jsonify({"error":"Only the Super User can change an Admin's permissions"}), 403
                    perms = u.setdefault("permissions", {})
                    for key in ("terminal","docker","guest_file_view","intercom"):
                        if key in d: perms[key] = bool(d[key])
                elif u["role"] == "guest":
                    perms = u.setdefault("permissions", {})
                    if any(k in d for k in ("docker","guest_file_view")):
                        return jsonify({"error":"docker/guest_file_view only apply to Admin accounts"}), 400
                    if "terminal" in d:
                        want_terminal = bool(d["terminal"])
                        if want_terminal and caller.get("role") != "superuser":
                            # A terminal is a shell as the account Void OS runs as, which can read its own
                            # configuration: handing one out is the Super User's decision alone.
                            return jsonify({"error":"Only the Super User can grant terminal access"}), 403
                        perms["terminal"] = want_terminal
                    if "intercom" in d:
                        want_intercom = bool(d["intercom"])
                        if want_intercom and caller.get("role") == "admin" and not _can_use_intercom(caller):
                            return jsonify({"error":"You can't grant intercom access to a guest without having it yourself"}), 403
                        perms["intercom"] = want_intercom
                else:
                    return jsonify({"error":"Unknown role"}), 400
                users[i] = u; _users_save(users)
                _audit("permission_changed", caller, target=u["username"], detail=json.dumps(perms))
                _notify_permissions_changed(uid, u)
                return jsonify({"ok":True,"permissions":perms})
    return jsonify({"error":"User not found"}), 404

@app.route("/api/users/<uid>/password", methods=["PATCH"])
@require_admin
def api_users_set_password(uid):
    caller = _current_user()
    d = request.get_json(silent=True) or {}
    new_pw = str(d.get("password",""))
    if len(new_pw) < 8:
        return jsonify({"error":"Password must be at least 8 characters"}), 400
    with _users_lock:
        users = _users_load()
        u = next((x for x in users if x["id"] == uid), None)
        if not u:
            return jsonify({"error":"User not found"}), 404
        if u["role"] != "guest" and caller.get("role") != "superuser":
            return jsonify({"error":"Only the Super User can reset an Admin's password"}), 403
        u["password_hash"] = generate_password_hash(new_pw)
        _users_save(users)
    _audit("password_reset", caller, target=u["username"])
    # A reset is usually *because* an account may be compromised: sign the
    # account out everywhere and close its live terminal/intercom sockets.
    if uid != caller["id"]:
        _revoke_user_sessions(uid)
        _notify_permissions_changed(uid, force_logout=True, reason="password_reset")
    else:
        _revoke_user_sessions(uid, keep_sid=getattr(g, "_void_sid", None))
    return jsonify({"ok":True})

def _prune_out_of_scope_sync(user: dict) -> int:
    """After a demotion: stop and remove this user's continuous-sync pairs whose
    local folder their NEW rank can no longer open. Pairs still inside their
    scope are kept."""
    from modules.cloud_routes import _sync_lock, _sync_pairs_load, _sync_pairs_save, _sync_stop_supervisor
    from modules.file_access import _safe
    gone = []
    with _sync_lock:
        pairs = _sync_pairs_load()
        for p in pairs:
            if p.get("user_id") != user["id"]:
                continue
            try:
                _safe(p.get("local_path", ""), user)
            except Exception:
                gone.append(p)
        if gone:
            ids = {p["id"] for p in gone}
            _sync_pairs_save([p for p in pairs if p["id"] not in ids])
    for p in gone:
        _sync_stop_supervisor(p["id"])
    return len(gone)

@app.route("/api/users/<uid>/promote", methods=["POST"])
@require_superuser
def api_users_promote(uid):
    """Super-User-only: move a user between admin and guest in either direction."""
    caller = _current_user()
    d = request.get_json(silent=True) or {}
    new_role = str(d.get("role",""))
    if new_role not in ("admin","guest"):
        return jsonify({"error":"role must be admin or guest"}), 400
    with _users_lock:
        users = _users_load()
        for i, u in enumerate(users):
            if u["id"] == uid:
                if u["role"] == "superuser":
                    return jsonify({"error":"Use the transfer flow to change the Super User's rank"}), 400
                if u["role"] == new_role:
                    return jsonify({"error": f"User is already {new_role}"}), 400
                old_role = u["role"]
                if new_role == "admin":
                    u["permissions"] = {"terminal": False, "docker": False, "guest_file_view": True, "intercom": False}
                    u.pop("personal_folder", None)  # admins don't use the guest personal-folder scoping
                else:  # -> guest
                    u["permissions"] = {"terminal": False, "intercom": False}
                    if not u.get("personal_folder"):
                        u["personal_folder"] = u["username"]
                    (USERS_DIR / u["personal_folder"]).mkdir(parents=True, exist_ok=True)
                u["role"] = new_role
                users[i] = u; _users_save(users)
                _audit("role_changed", caller, target=u["username"], detail=f"{old_role} -> {new_role}")
                if new_role == "guest":
                    n = _prune_out_of_scope_sync(u)
                    if n:
                        _audit("cloud_sync_disabled", caller, target=u["username"],
                               detail=f"{n} sync pair(s) removed: outside Guest access after demotion")
                _notify_permissions_changed(uid, u)
                return jsonify(_safe_user(u))
    return jsonify({"error":"User not found"}), 404

@app.route("/api/users/<uid>/transfer-superuser", methods=["POST"])
@require_superuser
def api_users_transfer_superuser(uid):
    """Atomically hand the Super User title to an existing Admin; the
    current Super User becomes an Admin in the same write. Never leaves
    the system with zero or two Super Users, even transiently."""
    caller = _current_user()
    with _users_lock:
        users = _users_load()
        target = next((u for u in users if u["id"] == uid), None)
        if not target:
            return jsonify({"error":"User not found"}), 404
        if target["id"] == caller["id"]:
            return jsonify({"error":"You are already the Super User"}), 400
        if target.get("role") != "admin":
            return jsonify({"error":"The target must currently be an Admin — promote them first"}), 400
        for i, u in enumerate(users):
            if u["id"] == target["id"]:
                u["role"] = "superuser"
                u.pop("permissions", None)  # superuser is never restricted
                users[i] = u; new_target = u
            elif u["id"] == caller["id"]:
                u["role"] = "admin"
                u["permissions"] = {"terminal": False, "docker": False, "guest_file_view": True, "intercom": False}
                users[i] = u; new_caller = u
        _users_save(users)
    _audit("superuser_transferred", caller, target=target["username"],
        detail=f"previous Super User was {caller['username']}")
    _notify_permissions_changed(target["id"], new_target)
    # The outgoing Super User's own terminal access is reset to False by this
    # transfer too — if they happen to have a live terminal open right now
    # (unusual, but possible), it needs to be killed just as instantly as if
    # someone else had done it to them.
    _notify_permissions_changed(caller["id"], new_caller)
    return jsonify({"ok": True})

# ══════════════════════════════════════════════════════════════════════
# ROUTES — BACKUP / RESTORE (superuser-only)
# ══════════════════════════════════════════════════════════════════════
# Every JSON-store file this app owns, by the name it's given inside the
# backup zip. audit.log is handled separately below (append-only on
# import, rather than overwritten — see api_backup_import).
_BACKUP_STORE_FILES = {
    "users.json":       (USERS_FILE,    _users_lock),
    "apps.json":        (APPS_FILE,     _apps_lock),
    "sessions.json":    (SESSIONS_FILE, _sessions_lock),
    "shares.json":      (SHARES_FILE,   _shares_lock),
    "app_stacks.json":  (STACKS_FILE,   _stacks_lock),
}

def _validate_backup_store(name: str, data) -> str | None:
    """Light shape validation for one store's parsed JSON — enough to
    reject an unrelated/foreign JSON file or a corrupted export, without
    trying to fully re-validate every field an ordinary write would.
    Returns an error string, or None if the shape looks right."""
    if name == "users.json":
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            return "users.json: expected an object with a 'users' list"
        for u in data["users"]:
            if not isinstance(u, dict) or "id" not in u or "username" not in u:
                return "users.json: contains a malformed user entry"
        if not any(u.get("role") == "superuser" for u in data["users"]):
            return ("users.json: contains no Super User account — refusing to import a user set "
                    "that would leave nobody able to manage the system")
    else:
        if not isinstance(data, list):
            return f"{name}: expected a JSON list"
    return None

def _pre_import_snapshot(path: Path, ts: str) -> str | None:
    """Copies the current file to <name>.pre-import-<timestamp>.bak next to
    itself before it gets overwritten, so a bad/wrong import can be undone
    even if the operator didn't think to export a backup first."""
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.pre-import-{ts}.bak")
    shutil.copy2(path, dest)
    return str(dest)

@app.route("/api/backup/export")
@require_superuser
def api_backup_export():
    """Exports every JSON store (users, tiles, sessions, share links,
    installed-app records) plus the audit log into one zip — a manual
    backup, or a way to migrate accounts/settings to new hardware.
    Superuser-only: this is a full copy of every account's password hash
    and session state, not something any Admin should be able to pull.
    Deliberately does NOT include FILE_ROOT/home or APP_DATA_DIR — actual
    file/app data can be gigabytes; this backs up Void OS's own
    configuration and history, not the files it serves. Copy those
    separately (rsync, your NAS's own backup tool, etc.) for a full
    hardware migration."""
    user = _current_user()
    buf = io.BytesIO()
    included = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, (path, lock) in _BACKUP_STORE_FILES.items():
            with lock:
                if path.exists():
                    zf.writestr(name, path.read_text(encoding="utf-8"))
                    included.append(name)
        with _audit_lock:
            if AUDIT_FILE.exists():
                zf.writestr("audit.log", AUDIT_FILE.read_text(encoding="utf-8"))
                included.append("audit.log")
        manifest = {
            "void_os_export": True, "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exported_by": user["username"], "files": included,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    buf.seek(0)
    _audit("backup_exported", user, detail=f"included: {', '.join(included)}")
    fname = f"voidos-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=fname)

@app.route("/api/backup/import", methods=["POST"])
@require_superuser
def api_backup_import():
    """Restores from a zip produced by api_backup_export. Cross-machine
    migration is explicitly supported — a backup made on one install can be
    restored onto a different one — so there is deliberately no
    machine/key fingerprint check blocking that here.
    Every store about to be overwritten is snapshotted next to itself first
    (<name>.pre-import-<timestamp>.bak), and the whole import either fully
    succeeds or writes nothing: every included file is parsed and shape-
    checked up front, before any file on disk is touched.
    audit.log is the one exception to "overwrite": its lines are appended,
    never replacing what's already there — an import should never erase
    this machine's own history, only add the imported machine's history
    alongside it.
    Refuses the whole import if users.json is included but contains zero
    Super User accounts (a true lockout, fixable only by editing files on
    disk) — but does NOT require the *currently signed-in* Super User
    specifically to survive the import. Restoring a full account set from
    a different install, under entirely different usernames, is a
    legitimate migration; the caller making this request may well end up
    signed out afterward, which is expected, not a bug."""
    user = _current_user()
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        zf = zipfile.ZipFile(f.stream)
    except zipfile.BadZipFile:
        return jsonify({"error": "Not a valid zip file"}), 400
    names = set(zf.namelist())
    if "manifest.json" not in names:
        return jsonify({"error": "Not a Void OS backup (missing manifest.json)"}), 400
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except Exception:
        return jsonify({"error": "manifest.json is not valid JSON"}), 400
    if not manifest.get("void_os_export"):
        return jsonify({"error": "Not a Void OS backup"}), 400

    parsed = {}
    for name in _BACKUP_STORE_FILES:
        if name not in names:
            continue
        try:
            data = json.loads(zf.read(name).decode("utf-8"))
        except Exception:
            return jsonify({"error": f"{name} is not valid JSON"}), 400
        err = _validate_backup_store(name, data)
        if err:
            return jsonify({"error": err}), 400
        parsed[name] = data

    audit_text = None
    if "audit.log" in names:
        try:
            audit_text = zf.read("audit.log").decode("utf-8")
        except Exception:
            return jsonify({"error": "audit.log could not be read"}), 400

    if not parsed and audit_text is None:
        return jsonify({"error": "Archive contained no recognized Void OS store files"}), 400

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    locks = {_BACKUP_STORE_FILES[name][1] for name in parsed}
    if audit_text is not None:
        locks.add(_audit_lock)
    # Sorted, stable acquisition order across requests — avoids a theoretical deadlock if an
    # export and an import (or two imports) happen to overlap. Imports are rare/manual, but this
    # costs nothing to get right.
    ordered_locks = sorted(locks, key=id)
    for lock in ordered_locks:
        lock.acquire()
    snapshots = []
    try:
        for name, data in parsed.items():
            path, _ = _BACKUP_STORE_FILES[name]
            snap = _pre_import_snapshot(path, ts)
            if snap:
                snapshots.append(snap)
            _atomic_write_json(path, data)
        if audit_text is not None:
            with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
                fh.write(audit_text if audit_text.endswith("\n") or not audit_text else audit_text + "\n")
    finally:
        for lock in ordered_locks:
            lock.release()

    restored = list(parsed.keys()) + (["audit.log"] if audit_text is not None else [])
    detail = f"restored: {', '.join(restored)}"
    if snapshots:
        detail += f"; pre-import snapshots: {', '.join(Path(s).name for s in snapshots)}"
    if "app_stacks.json" in parsed:
        detail += ("; note: app_stacks.json only restores installed-app metadata — the actual "
                   "Docker containers and app-data folders must be migrated separately")
    _audit("backup_imported", user, detail=detail)
    _resync_all_live_users()     # the account list was replaced: every open page gets its current access now
    return jsonify({"ok": True, "restored": restored, "snapshots": snapshots})

# ══════════════════════════════════════════════════════════════════════
# ROUTES — AUDIT LOG (admin-only)
# ══════════════════════════════════════════════════════════════════════
@app.route("/api/audit")
@require_admin
def api_audit():
    _drain_gate_audit()
    limit = min(int(request.args.get("limit", 200) or 200), 1000)
    action = request.args.get("action", "").strip()
    username = request.args.get("username", "").strip()
    entries = _audit_read(limit=limit, action=action, username=username)
    actions = sorted({e.get("action","") for e in _audit_read(limit=AUDIT_MAX_ENTRIES) if e.get("action")})
    return jsonify({"entries": entries, "actions": actions})

@app.route("/api/audit/clear", methods=["POST"])
@require_superuser
def api_audit_clear():
    """Clear audit log entries — the single most destructive action this
    log supports, so it's deliberately gated tighter than viewing it
    (superuser-only, not any admin) and deliberately never leaves the log
    silent about its own clearing: the very next entry written is always a
    record of who cleared it, when, how many entries were removed, and
    what cutoff (if any) was used. Two modes, both audited identically:
      - before_ts omitted/null → clear everything.
      - before_ts set (an ISO timestamp) → keep entries at or after that
        point, remove everything strictly older — a retention window
        rather than a full wipe.
    confirm_text is required and checked server-side as defense in depth
    beyond whatever confirmation dialog the frontend shows — a stray or
    replayed API call can't silently wipe the log just because it hit the
    right URL."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    if str(d.get("confirm_text", "")).strip() != "CLEAR AUDIT LOG":
        return jsonify({"error": "Confirmation text did not match"}), 400
    before_ts = d.get("before_ts")
    with _audit_lock:
        try:
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        total_before = len([l for l in lines if l.strip()])
        if before_ts:
            try:
                cutoff = datetime.fromisoformat(str(before_ts))
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid before_ts"}), 400
            kept, removed = [], 0
            for line in lines:
                s = line.strip()
                if not s:
                    continue
                try:
                    entry_ts = datetime.fromisoformat(json.loads(s).get("ts", ""))
                except Exception:
                    kept.append(line)  # unparseable line — keep it rather than silently drop unknown data
                    continue
                if entry_ts < cutoff:
                    removed += 1
                else:
                    kept.append(line)
            with open(AUDIT_FILE, "w", encoding="utf-8") as f:
                f.writelines(kept)
            detail = f"removed {removed} of {total_before} entries older than {cutoff.date().isoformat()}"
        else:
            with open(AUDIT_FILE, "w", encoding="utf-8") as f:
                pass
            removed = total_before
            detail = f"removed all {total_before} entries"
    _audit("audit_log_cleared", user, detail=detail)
    return jsonify({"ok": True, "removed": removed})
