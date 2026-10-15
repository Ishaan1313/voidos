"""
modules/twofa_routes.py — Routes for optional two-factor sign-in: set it up, turn it
off, regenerate backup codes, and let an admin reset someone who lost their phone.
The sign-in step itself is in account_routes.auth_check; the logic is in twofactor.py.

Flow (modelled on Discord's):
  1. Settings -> Security -> "Enable two-factor": enter your password.
  2. Scan the QR code (or type the key) in an authenticator app, enter its 6-digit code.
  3. Save the 10 one-time backup codes shown once.
Turning it off needs the password plus a current code (or a backup code).
"""
from flask import g, jsonify, request
from werkzeug.security import check_password_hash

from modules import twofactor
from modules.activity import _audit
from modules.auth import _clear_fails, _fail, _ip, _is_locked
from modules.decorators import require_admin, require_auth
from modules.error_log import _current_user
from modules.users import _users_load
from modules.web import _notify_permissions_changed, app
from modules.admin_routes import _revoke_user_sessions


def _password_gate(user: dict, pw) -> tuple | None:
    """None if `pw` is this user's password; otherwise a (response, status) to
    return. Wrong passwords count toward the sign-in lockout, so a hijacked
    session can't be used to guess the password here."""
    ip = _ip()
    locked, rem = _is_locked(ip)
    if locked:
        return jsonify({"error": f"Too many attempts — try again in {rem}s"}), 429
    if not check_password_hash(user.get("password_hash", ""), str(pw or "")):
        _fail(ip)
        return jsonify({"error": "Password is incorrect"}), 403     # 401 would send the page to the login screen
    _clear_fails(ip)
    return None


def _code_error(reason: str):
    if reason == "locked":
        wait = twofactor.locked_for(_current_user()["id"])
        return jsonify({"error": f"Too many wrong codes — try again in {max(wait, 1)}s"}), 429
    if reason == "unreadable":
        return jsonify({"error": "This account's two-factor data can't be read (the server's secret key changed). "
                                 "Ask the Super User to reset your two-factor sign-in."}), 409
    return jsonify({"error": "That code isn't right. Check the code in your app (or use a backup code)."}), 403


@app.route("/api/me/2fa")
@require_auth
def api_2fa_status():
    u = _current_user()
    return jsonify({"enabled": twofactor.is_enabled(u), "backup_remaining": twofactor.backup_remaining(u)})


@app.route("/api/me/2fa/setup", methods=["POST"])
@require_auth
def api_2fa_setup():
    u = _current_user()
    if twofactor.is_enabled(u):
        return jsonify({"error": "Two-factor sign-in is already on"}), 400
    d = request.get_json(silent=True) or {}
    bad = _password_gate(u, d.get("password"))
    if bad:
        return bad
    secret = twofactor.begin_setup(u["id"])
    if not secret:
        return jsonify({"error": "User not found"}), 404
    uri = twofactor.provisioning_uri(secret, u["username"])
    return jsonify({"ok": True, "key": twofactor.spaced(secret), "uri": uri, "qr_svg": twofactor.qr_svg(uri)})


@app.route("/api/me/2fa/enable", methods=["POST"])
@require_auth
def api_2fa_enable():
    u = _current_user()
    if twofactor.is_enabled(u):
        return jsonify({"error": "Two-factor sign-in is already on"}), 400
    if not twofactor.pending_secret(u):
        return jsonify({"error": "Setup expired — start again"}), 400
    d = request.get_json(silent=True) or {}
    ip = _ip()
    locked, rem = _is_locked(ip)
    if locked:
        return jsonify({"error": f"Too many attempts — try again in {rem}s"}), 429
    codes = twofactor.activate(u["id"], d.get("code"))
    if codes is None:
        _fail(ip)
        return jsonify({"error": "That code isn't right. Check that your phone's clock is correct and try the new code."}), 403
    _clear_fails(ip)
    # Like Discord: turning it on signs out every other device, so a copied
    # session cookie stops working now that the account has a second factor.
    n = _revoke_user_sessions(u["id"], keep_sid=getattr(g, "_void_sid", None))
    _audit("2fa_enabled", u, target=u["username"])
    return jsonify({"ok": True, "backup_codes": codes, "other_sessions_signed_out": n})


@app.route("/api/me/2fa/backup-codes", methods=["POST"])
@require_auth
def api_2fa_backup_codes():
    u = _current_user()
    if not twofactor.is_enabled(u):
        return jsonify({"error": "Two-factor sign-in is off"}), 400
    d = request.get_json(silent=True) or {}
    bad = _password_gate(u, d.get("password"))
    if bad:
        return bad
    codes = twofactor.regenerate_backup(u["id"])
    _audit("2fa_backup_regenerated", u, target=u["username"])
    return jsonify({"ok": True, "backup_codes": codes})


@app.route("/api/me/2fa/disable", methods=["POST"])
@require_auth
def api_2fa_disable():
    u = _current_user()
    if not twofactor.is_enabled(u):
        # also clears a setup that was started and abandoned
        twofactor.remove(u["id"])
        return jsonify({"ok": True})
    d = request.get_json(silent=True) or {}
    bad = _password_gate(u, d.get("password"))
    if bad:
        return bad
    ok, reason = twofactor.check_code(u["id"], d.get("code"))
    if not ok:
        return _code_error(reason)
    twofactor.remove(u["id"])
    _audit("2fa_disabled", u, target=u["username"])
    return jsonify({"ok": True})


@app.route("/api/users/<uid>/2fa/reset", methods=["POST"])
@require_admin
def api_users_2fa_reset(uid):
    """For someone who lost their phone AND their backup codes. Same rank rule
    as resetting a password: an Admin can reset a Guest, only the Super User
    can reset an Admin or the Super User."""
    caller = _current_user()
    if caller["id"] == uid:
        return jsonify({"error": "Use Settings → Security to turn off your own two-factor sign-in"}), 400
    target = next((x for x in _users_load() if x.get("id") == uid), None)
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target.get("role") != "guest" and caller.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can reset an Admin's two-factor sign-in"}), 403
    if not twofactor.is_enabled(target):
        return jsonify({"error": "That account doesn't have two-factor sign-in on"}), 400
    twofactor.remove(uid)
    _audit("2fa_reset", caller, target=target["username"])
    # A reset usually means a lost device or a suspected takeover: sign them out everywhere.
    _revoke_user_sessions(uid)
    _notify_permissions_changed(uid, force_logout=True, reason="2fa_reset")
    return jsonify({"ok": True})
