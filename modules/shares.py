"""
modules/shares.py — Public share links: create, manage, landing page, downloads.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import Response
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
import tempfile
import threading
import time
import uuid
import zipfile
from modules.config import COOKIE_SEC, FILE_ROOT, PIN_LEN, SHARED_PARTS, SHARES_FILE, ZIP_TMP_DIR
from modules.auth import (
    MAX_SHARE_ZIP_DOWNLOADS_PER_IP, _ip, _share_clear_fails, _share_dl_rate_check, _share_fail,
    _share_is_locked, _share_link_fail, _share_link_locked, _share_zip_inflight_by_ip, _share_zip_inflight_lock,
)
from modules.web import _atomic_write_json, _share_serial, _shares_lock, app
from modules.limits import guarded_send
from modules.activity import _audit
from modules.error_log import _current_user
from modules.users import _get_user_by_id, _users_load
from modules.file_access import _safe, _walk_files
from modules import errors
from modules.decorators import require_auth


# ══════════════════════════════════════════════════════════════════════
# SHARE LINKS — a signed, optionally time-limited and password-protected
# public URL to one file or folder inside home/shared. Deliberately scoped
# to Shared only, never personal folders: Shared is already readable/
# writable by every account on the system, so turning one of its items
# into an unauthenticated public link is a difference of degree (who else
# can reach it), not of kind (who can already see it) — personal folders
# don't have that property, so they're excluded entirely, checked
# server-side in _require_shared_scope regardless of what a client sends.
#
# The token itself (URLSafeTimedSerializer, its own salt so it can never be
# confused with or replayed as a session token) carries nothing but an
# opaque share id — no path, expiry, or permissions are embedded in it.
# The actual source of truth is the matching record in shares.json, so
# revoking a link is instant (delete the record) rather than relying on
# the token's own signature outliving the record, and shortening an
# expiry always takes effect immediately.
# ══════════════════════════════════════════════════════════════════════
SHARE_MAX_ZIP_FILES = int(os.environ.get("VOID_SHARE_MAX_ZIP_FILES", "20000"))
# Cap on how many files a single folder-share zip will include — a folder share
# is a public, unauthenticated, repeatedly-hittable endpoint, so this needs the
# same "don't let a single request do unbounded work" treatment as the
# search-scan cap elsewhere in this codebase, not just the multi-select zip's
# per-request path count.
SHARE_MAX_ZIP_BYTES = int(os.environ.get("VOID_SHARE_MAX_ZIP_BYTES", str(10 * 1024**3)))  # default 10 GiB
# File-count alone doesn't bound total transfer size — a handful of huge
# files (raw video, VM images, ...) could still make one request unboundedly
# large/long, especially now that streaming is uncompressed. This truncates
# the same way SHARE_MAX_ZIP_FILES already does: once adding the next file
# would cross the byte ceiling, stop there rather than reject outright — a
# partial-but-honest zip (declared size matches what's actually included)
# beats an all-or-nothing failure for a folder that's merely somewhat over.
SHARE_STREAM_STALL_TIMEOUT_S = int(os.environ.get("VOID_SHARE_STREAM_STALL_TIMEOUT_S", "45"))
# If a download makes no forward progress (client not reading, or the
# writer thread stuck) for this long, the generator aborts and the
# semaphore slot releases — see the stall-detection comment in generate()
# below for exactly what this can and can't guarantee at the Flask/
# Werkzeug application layer (a full Slowloris defense needs raw-socket-
# level timeouts this layer doesn't have reliable access to).
MAX_CONCURRENT_SHARE_ZIPS = 3
_share_zip_semaphore = threading.Semaphore(MAX_CONCURRENT_SHARE_ZIPS)
SHARE_UNLOCK_TTL = 600  # seconds a correct share password is "remembered" in the browser via a signed cookie

def _sh_load() -> list:
    with _shares_lock:
        if SHARES_FILE.exists():
            try:
                data = json.loads(SHARES_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list): return data
            except Exception:
                pass
        return []

def _sh_save(d):
    with _shares_lock:
        _atomic_write_json(SHARES_FILE, d)
    try:
        from modules.web import _broadcast_coalesced
        _broadcast_coalesced("shares", {"type": "shares_changed"})     # every open Share Links page redraws
    except Exception:
        pass

def _require_shared_scope(p: Path):
    """Raise ValueError unless p is the Shared folder itself or something under it."""
    try:
        parts = p.relative_to(FILE_ROOT).parts
    except ValueError:
        raise ValueError("Path is outside the file root")
    if tuple(parts[:len(SHARED_PARTS)]) != SHARED_PARTS:
        raise ValueError("Only files or folders inside Shared can be turned into a public link")

def _share_rec_path(rec: dict) -> Path:
    return (FILE_ROOT / rec["path"]).resolve()

def _share_cookie_name(sid: str) -> str:
    return f"void_share_{sid}"

def _share_pw_version(rec: dict) -> str:
    """Fingerprint of the link's current PIN/password. Stored in the unlock cookie, so changing or
    removing the PIN ends every visitor's unlocked access at once instead of when the cookie expires."""
    import hashlib
    return hashlib.sha256((rec.get("password_hash") or "").encode("utf-8")).hexdigest()[:16]


def _share_is_unlocked(rec: dict) -> bool:
    if not rec.get("password_hash"):
        return True
    tok = request.cookies.get(_share_cookie_name(rec["id"]))
    if not tok:
        return False
    try:
        data = _share_serial.loads(tok, max_age=SHARE_UNLOCK_TTL)
    except (BadSignature, SignatureExpired):
        return False
    return (data.get("sid") == rec["id"] and data.get("unlocked") is True
            and data.get("pv") == _share_pw_version(rec))

def _resolve_share(token: str):
    """Returns (rec, error_message). error_message is a user-safe string
    to show on the public page; rec is None whenever error_message is set."""
    try:
        # No max_age here deliberately — token age isn't what governs expiry,
        # the record's own expires_at is (see module docstring above). A
        # "never expires" share shouldn't start failing just because the
        # token itself has existed for a long time.
        data = _share_serial.loads(token)
    except SignatureExpired:
        return None, "This link is invalid."
    except BadSignature:
        return None, "This link is invalid."
    sid = data.get("sid") if isinstance(data, dict) else None
    if not sid:
        return None, "This link is invalid."
    rec = next((s for s in _sh_load() if s["id"] == sid), None)
    if rec is None:
        return None, "This link has been revoked or no longer exists."
    if rec.get("expires_at") and time.time() > rec["expires_at"]:
        return None, "This link has expired."
    p = _share_rec_path(rec)
    try:
        _require_shared_scope(p)  # defense in depth: the item may have been moved out of Shared since
    except ValueError:
        return None, "This item is no longer shared."
    if not p.exists():
        return None, "This item no longer exists."
    return rec, None

def _can_manage_share(actor: dict, owner_role: str, is_owner: bool) -> bool:
    """Who can view/revoke/edit a given share, beyond its own owner.
    Mirrors _can_view_session_details's rule, just for shares instead of
    sessions: Guests never see anyone else's; Admins see their own plus any
    Guest's, never another Admin's; the Super User sees everyone's."""
    if is_owner:
        return True
    if actor.get("role") == "superuser":
        return True
    if actor.get("role") == "admin":
        return owner_role == "guest"
    return False

@app.route("/api/shares", methods=["POST"])
@require_auth
def api_shares_create():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    raw = str(d.get("path", "")).strip()
    try:
        p = _safe(raw, user)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    try:
        _require_shared_scope(p)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    expires_at = None
    hours = d.get("expires_in_hours")
    if hours:
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid expiry"}), 400
        if not (hours == hours) or hours > 24 * 365 * 10:   # NaN or absurdly far away
            return jsonify({"error": "Invalid expiry"}), 400
        if hours > 0:
            expires_at = time.time() + hours * 3600
    pw = str(d.get("password") or "")
    if len(pw) > 200:
        return jsonify({"error": "Password too long"}), 400
    # The share dialog lets the user explicitly pick PIN or Password mode
    # (sent as "mode"). If omitted (e.g. an older client, or a direct API
    # call), fall back to auto-detecting from the secret itself — a short,
    # all-digit value reads as a PIN. Either way this only changes which
    # unlock UI the share page shows; the secret is still checked the same
    # way (generate_password_hash / check_password_hash) regardless of mode.
    mode = d.get("mode")
    if mode == "pin":
        if not (pw.isdigit() and len(pw) == PIN_LEN):
            return jsonify({"error": f"A PIN must be exactly {PIN_LEN} digits"}), 400
        is_pin = True
    elif mode == "password":
        is_pin = False
    else:
        is_pin = bool(pw) and pw.isdigit() and len(pw) == PIN_LEN
    sid = uuid.uuid4().hex
    rec = {
        "id": sid, "owner_id": user["id"], "owner_username": user["username"],
        "path": p.relative_to(FILE_ROOT).as_posix(), "name": p.name, "is_dir": p.is_dir(),
        "created_at": time.time(), "expires_at": expires_at,
        "password_hash": generate_password_hash(pw) if pw else None,
        "is_pin": is_pin, "pin_length": len(pw) if is_pin else None,
        "hits": 0,
    }
    with _shares_lock:
        shares = _sh_load(); shares.append(rec); _sh_save(shares)
    token = _share_serial.dumps({"sid": sid})
    _audit("share_create", user, target=rec["path"],
           detail=f"{'folder' if rec['is_dir'] else 'file'}"
                  + (", never expires" if not expires_at else f", expires in {hours:g}h")
                  + (", password-protected" if rec["password_hash"] else ""))
    return jsonify({"id": sid, "url": f"/s/{token}", "expires_at": expires_at})

@app.route("/api/shares", methods=["GET"])
@require_auth
def api_shares_list():
    user = _current_user()
    shares = _sh_load()
    # Look up each owner's CURRENT role fresh (not whatever it was at
    # share-creation time) — a share's owner can be promoted/demoted after
    # the share was created, and visibility should reflect who they are
    # *now*, same reasoning api_sessions_list already uses for its own
    # roles map.
    roles = {u["id"]: u.get("role", "") for u in _users_load()}
    visible = [s for s in shares
               if _can_manage_share(user, roles.get(s.get("owner_id"), ""), s.get("owner_id") == user["id"])]
    now = time.time()
    out = [{
        "id": s["id"], "path": s["path"], "name": s["name"], "is_dir": s["is_dir"],
        "owner_username": s.get("owner_username", ""), "owner_role": roles.get(s.get("owner_id"), ""),
        "is_mine": s.get("owner_id") == user["id"], "created_at": s["created_at"],
        "expires_at": s.get("expires_at"), "has_password": bool(s.get("password_hash")),
        "hits": s.get("hits", 0),
        "expired": bool(s.get("expires_at") and now > s["expires_at"]),
    } for s in visible]
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(out)

@app.route("/api/shares/<sid>/link")
@require_auth
def api_shares_link(sid):
    """Returns the actual share URL (bearer token included) for the Share
    Links tab's Copy Link button. Deliberately a separate, on-demand
    endpoint rather than a field on the list response above — the token is
    the real bearer credential for the link, so it's only ever handed back
    to someone who's *already* allowed to manage this specific share,
    exactly when they click Copy, not bundled into every list fetch."""
    user = _current_user()
    shares = _sh_load()
    rec = next((s for s in shares if s["id"] == sid), None)
    if rec is None:
        return jsonify({"error": "Not found"}), 404
    owner = _get_user_by_id(rec.get("owner_id"))
    owner_role = owner.get("role", "") if owner else ""
    if not _can_manage_share(user, owner_role, rec["owner_id"] == user["id"]):
        return jsonify({"error": "Not found"}), 404
    token = _share_serial.dumps({"sid": sid})
    return jsonify({"url": f"/s/{token}"})

@app.route("/api/shares/<sid>", methods=["PATCH"])
@require_auth
def api_shares_edit(sid):
    """Edit a share's expiry. Deliberately does NOT allow changing the
    password/PIN in this first pass — that's a materially more sensitive
    operation (the old secret silently stops working for whoever it was
    given to) and can be a follow-up; this endpoint scopes to duration
    only, matching exactly what was asked for."""
    user = _current_user()
    with _shares_lock:
        shares = _sh_load()
        rec = next((s for s in shares if s["id"] == sid), None)
        if rec is None:
            return jsonify({"error": "Not found"}), 404
        owner = _get_user_by_id(rec.get("owner_id"))
        owner_role = owner.get("role", "") if owner else ""
        if not _can_manage_share(user, owner_role, rec["owner_id"] == user["id"]):
            return jsonify({"error": "Not found"}), 404
        d = request.get_json(silent=True) or {}
        hours = d.get("expires_in_hours")
        expires_at = None
        if hours:
            try:
                hours = float(hours)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid expiry"}), 400
            if not (hours == hours) or hours > 24 * 365 * 10:
                return jsonify({"error": "Invalid expiry"}), 400
            if hours > 0:
                expires_at = time.time() + hours * 3600
        rec["expires_at"] = expires_at
        _sh_save(shares)
    _audit("share_edit", user, target=rec["path"],
           detail="never expires" if not expires_at else f"expires in {hours:g}h")
    return jsonify({"ok": True, "expires_at": expires_at})

@app.route("/api/shares/<sid>", methods=["DELETE"])
@require_auth
def api_shares_del(sid):
    user = _current_user()
    with _shares_lock:
        shares = _sh_load()
        rec = next((s for s in shares if s["id"] == sid), None)
        if rec is None:
            return jsonify({"error": "Not found"}), 404
        owner = _get_user_by_id(rec.get("owner_id"))
        owner_role = owner.get("role", "") if owner else ""
        if not _can_manage_share(user, owner_role, rec["owner_id"] == user["id"]):
            # Same 404-not-403 pattern used for session details: don't confirm
            # to a non-owner, non-admin caller that a given share id exists.
            return jsonify({"error": "Not found"}), 404
        _sh_save([s for s in shares if s["id"] != sid])
    _audit("share_revoke", user, target=rec["path"])
    return jsonify({"ok": True})

@app.route("/s/<token>", methods=["GET"])
def share_landing(token):
    rec, err = _resolve_share(token)
    if err:
        return render_template("share.html", error=err), 404
    size = None
    if not rec["is_dir"]:
        try: size = _share_rec_path(rec).stat().st_size
        except OSError: pass
    return render_template("share.html", rec=rec, token=token,
                            unlocked=_share_is_unlocked(rec), size=size)

@app.route("/s/<token>", methods=["POST"])
def share_unlock(token):
    rec, err = _resolve_share(token)
    if err:
        return render_template("share.html", error=err), 404
    if not rec.get("password_hash"):
        return redirect(f"/s/{token}")
    ip = _ip()
    locked, wait = _share_is_locked(ip)
    if not locked:
        locked, wait = _share_link_locked(rec["id"])
    if locked:
        resp = make_response(render_template("share.html", rec=rec, token=token, unlocked=False,
                              pw_error=f"Too many attempts — try again in {wait}s"), 429)
        resp.headers["X-Retry-After"] = str(wait)
        return resp
    pw = request.form.get("password", "")
    if not check_password_hash(rec["password_hash"], pw):
        _share_fail(ip)
        _share_link_fail(rec["id"])
        return render_template("share.html", rec=rec, token=token, unlocked=False,
                                pw_error="Incorrect PIN" if rec.get("is_pin") else "Incorrect password"), 403
    _share_clear_fails(ip)
    resp = make_response(redirect(f"/s/{token}"))
    resp.set_cookie(_share_cookie_name(rec["id"]),
                     _share_serial.dumps({"sid": rec["id"], "unlocked": True, "pv": _share_pw_version(rec)}),
                     max_age=SHARE_UNLOCK_TTL, httponly=True, secure=COOKIE_SEC,
                     samesite="Lax", path="/s/")
    return resp

def _share_zip_precompute_size(files) -> int | None:
    """Exact size of the ZIP_STORED archive zipfile.ZipFile will produce —
    not an estimate. Writing to a real seekable file (which is what the
    streaming route below still does, just tailed while it grows) means
    zipfile never needs a trailing data-descriptor: it seeks back and
    patches the real CRC/size into each local file header once known, so
    the classic fixed-size header layout applies exactly — 30-byte local
    header + 46-byte central directory header per entry (each also
    carrying the filename once), plus one 22-byte End-Of-Central-Directory
    record. Verified byte-for-byte against real zipfile output before
    relying on it for a Content-Length header (getting this wrong would
    either truncate every download or hang every client waiting for bytes
    that will never arrive).

    Returns None if the archive would need Zip64 (a single file >=4GiB,
    more than 65535 entries, or total size >=4GiB) — rare for a shared
    folder, and precomputing Zip64's extra-field layout isn't worth the
    added complexity for that edge case; the caller falls back to the
    original non-streamed build-then-send path when this returns None.
    """
    ZIP64_LIMIT = 0xFFFFFFFF
    if len(files) > 0xFFFF:
        return None
    total = 22  # End-Of-Central-Directory record
    for f, arcname in files:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        if size >= ZIP64_LIMIT:
            return None
        name_len = len(str(arcname).replace(os.sep, "/").encode("utf-8"))
        total += size + 30 + name_len + 46 + name_len
    if total >= ZIP64_LIMIT:
        return None
    return total

@app.route("/s/<token>/download")
def share_download(token):
    rec, err = _resolve_share(token)
    if err:
        return render_template("share.html", error=err), 404
    if not _share_is_unlocked(rec):
        return render_template("share.html", rec=rec, token=token, unlocked=False,
                                pw_error="Password required"), 403

    ip = _ip()
    # Independent of password state deliberately — most shares have none
    # (or are already unlocked), so this is the only thing standing between
    # a visitor and hitting this endpoint in a tight loop. Applies to both
    # branches below (single-file and folder), unlike the per-IP zip-slot
    # cap further down, which only makes sense for the semaphore-guarded
    # folder path.
    allowed, retry_after = _share_dl_rate_check(ip)
    if not allowed:
        return jsonify({"error": "Too many downloads from this address — "
                                  f"try again in {retry_after}s"}), 429, {"X-Retry-After": str(retry_after)}

    p = _share_rec_path(rec)
    with _shares_lock:
        shares = _sh_load()
        for s in shares:
            if s["id"] == rec["id"]:
                s["hits"] = s.get("hits", 0) + 1
                break
        _sh_save(shares)

    if not rec["is_dir"]:
        _audit("share_download", None, target=rec["path"], detail=f"share {rec['id'][:8]} from {ip}", ip=ip)
        return guarded_send(lambda: send_file(p, as_attachment=True, download_name=p.name), ip=ip)

    # One of the 3 global zip-build/stream slots per visitor, max — this is
    # the direct fix for a single visitor parking all 3 and taking every
    # other share link on the server down with it (see
    # SECURITY_ISSUE_share_download_dos_risk.md). Checked before the global
    # semaphore so an IP already at its own cap gets a clear 429 rather than
    # queuing behind the semaphore and maybe still taking the last slot.
    with _share_zip_inflight_lock:
        if _share_zip_inflight_by_ip.get(ip, 0) >= MAX_SHARE_ZIP_DOWNLOADS_PER_IP:
            return jsonify({"error": "You already have a folder download in progress — "
                                      "wait for it to finish before starting another"}), 429
        _share_zip_inflight_by_ip[ip] = _share_zip_inflight_by_ip.get(ip, 0) + 1
    ip_slot_released = threading.Event()
    def _release_ip_slot():
        if ip_slot_released.is_set(): return
        ip_slot_released.set()
        with _share_zip_inflight_lock:
            n = _share_zip_inflight_by_ip.get(ip, 1) - 1
            if n <= 0: _share_zip_inflight_by_ip.pop(ip, None)
            else: _share_zip_inflight_by_ip[ip] = n

    if not _share_zip_semaphore.acquire(blocking=False):
        _release_ip_slot()
        return jsonify({"error": "Server is busy building another shared download right now — try again shortly"}), 429

    files, truncated, total_bytes = [], False, 0
    for f in _walk_files(p):
        try: fsize = f.stat().st_size
        except OSError: fsize = 0
        if len(files) >= SHARE_MAX_ZIP_FILES or total_bytes + fsize > SHARE_MAX_ZIP_BYTES:
            truncated = True
            break
        files.append((f, f.relative_to(p.parent)))
        total_bytes += fsize

    content_length = _share_zip_precompute_size(files)
    share_path = rec["path"]
    audit_detail = f"share {rec['id'][:8]} from {ip}" + (f", truncated at {SHARE_MAX_ZIP_FILES} files/{SHARE_MAX_ZIP_BYTES} bytes" if truncated else "")

    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="voidos-share-", dir=str(ZIP_TMP_DIR))
    os.close(fd)
    result = {"ok": False}
    # The generator (below) tails tmp_path from the *request-handling
    # thread* while this writer thread fills it — real streaming, first
    # bytes go out well before the last file is even opened, instead of
    # the old build-the-whole-zip-then-send_file gap. reader_done is how
    # the writer knows it's safe to delete the temp file: not the instant
    # it finishes writing, but only once nothing is reading from it anymore
    # (normal completion, the visitor disconnecting, or a stall timeout).
    reader_done = threading.Event()
    writer_done = threading.Event()
    # Chunk-level write progress, so a stuck writer (stuck network mount
    # under FILE_ROOT, a failing disk, antivirus holding a file handle) can
    # be detected the same way a stuck *reader* already is — measuring time
    # since the last forward progress, not total elapsed time, so a large
    # legitimate file that's continuously flowing never trips this even if
    # it takes a while overall. Plain mutable containers instead of a lock:
    # each holds a single value written by one thread and read by another,
    # which is atomic enough under the GIL for this use (same pattern as
    # the `alive=[True]` flags used elsewhere in this file, e.g. the
    # terminal session loop).
    last_write_progress = [time.time()]
    current_file = [None]
    semaphore_released = threading.Event()
    def _release_semaphore():
        # Idempotent because two different paths can now trigger this: the
        # writer's own `finally` in the normal case, or the reader-side
        # watchdog directly if the writer never returns control at all (a
        # truly hung write) — Python can't preempt a blocked C-level
        # read/write from another thread, so if that happens the writer
        # thread just never reaches its own cleanup, possibly ever.
        if semaphore_released.is_set(): return
        semaphore_released.set()
        _share_zip_semaphore.release()

    def _writer():
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED, allowZip64=(content_length is None)) as zf:
                for f, arcname in files:
                    current_file[0] = str(arcname)
                    try:
                        zi = zipfile.ZipInfo.from_file(f, arcname)
                        zi.compress_type = zipfile.ZIP_STORED
                        with zf.open(zi, "w") as dest, open(f, "rb") as src:
                            while True:
                                chunk = src.read(262144)
                                if not chunk: break
                                dest.write(chunk)
                                last_write_progress[0] = time.time()
                    except Exception: pass  # unreadable file mid-share — skip it, matches prior behavior
            result["ok"] = True
        except Exception as e:
            result["error"] = errors.describe(e)
        finally:
            writer_done.set()
            reader_done.wait(timeout=300)  # don't outlive the reader by more than this, either way
            _release_semaphore()
            _release_ip_slot()
            try: os.remove(tmp_path)
            except OSError: pass  # already removed by the reader-side stall path, or (Windows) still open — see below

    threading.Thread(target=_writer, daemon=True).start()

    def generate():
        sent = 0
        stalled = False
        stall_reason = None
        # Forward-progress watchdog: checks elapsed time immediately upon
        # resuming from each yield — i.e. exactly how long that chunk's
        # write took, however long the client (or a stuck writer thread)
        # made it wait — comparing against the timestamp from *before* that
        # yield, before it gets overwritten. A plain Python generator can't
        # preempt a single blocked write from inside its own body (there's
        # no portable way to reach the raw socket's SO_SNDTIMEO through
        # Gunicorn's WSGI environ), but the moment control returns, this
        # catches it before issuing another read/yield — so the slot can't
        # be held indefinitely by a client that stops reading after the
        # very first chunk.
        last_progress = time.time()
        try:
            with open(tmp_path, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if chunk:
                        sent += len(chunk)
                        yield chunk
                        now = time.time()
                        if now - last_progress > SHARE_STREAM_STALL_TIMEOUT_S:
                            stalled, stall_reason = True, "reader"
                            break
                        last_progress = now
                        continue
                    # Caught up to what's been written so far. If the writer
                    # thread has already finished, do one last read in case
                    # a final flush landed between that check and this one,
                    # then stop; otherwise wait for more bytes to appear.
                    if writer_done.is_set():
                        chunk = fh.read(262144)
                        if chunk:
                            sent += len(chunk)
                            yield chunk
                            now = time.time()
                            if now - last_progress > SHARE_STREAM_STALL_TIMEOUT_S:
                                stalled, stall_reason = True, "reader"
                                break
                            last_progress = now
                            continue
                        break
                    # Writer isn't done and there's nothing new to read yet —
                    # by construction this can only ever be the *writer's*
                    # problem (there's nothing for the client to be slow
                    # about reading), so this checks last_write_progress
                    # only, not last_progress — that check belongs solely at
                    # the two yield sites above/below, where it measures
                    # exactly how long a real client read took. Getting this
                    # wrong here would misattribute a stuck writer as a
                    # stalled reader, which matters because only the
                    # "writer" classification releases the semaphore slot
                    # immediately instead of waiting on a thread that may
                    # never return control.
                    if time.time() - last_write_progress[0] > SHARE_STREAM_STALL_TIMEOUT_S:
                        stalled, stall_reason = True, "writer"
                        break
                    time.sleep(0.05)
        finally:
            # Explicitly NOT calling _audit() with implicit request-context
            # access here — this generator keeps running after the view
            # function has already returned, so `ip` and `share_path` were
            # captured above, and _audit's ip= param is passed explicitly to
            # skip its own request-context lookup entirely.
            reader_done.set()
            _release_ip_slot()
            if stall_reason == "writer":
                # Don't wait on the writer thread for any of this — it may
                # be the very thing that's stuck. Release what we can here
                # and now; the writer's own `finally` (if it ever runs) is
                # written to no-op safely on top of this.
                _release_semaphore()
                try: os.remove(tmp_path)
                except OSError: pass  # (Windows) likely still open by the stuck writer — leaks until it returns or the process restarts, same accepted trade-off as the orphaned thread itself
                _audit("share_download_writer_stalled", None, target=share_path,
                       detail=audit_detail + f" (stuck writing {current_file[0]!r}, "
                                              f"no write progress for {SHARE_STREAM_STALL_TIMEOUT_S}s, {sent} bytes sent to client)", ip=ip)
            elif stalled:
                _audit("share_download_incomplete", None, target=share_path,
                       detail=audit_detail + f" (stalled after {sent} bytes, no progress for {SHARE_STREAM_STALL_TIMEOUT_S}s)", ip=ip)
            elif result.get("ok") and (content_length is None or sent >= content_length):
                _audit("share_download", None, target=share_path, detail=audit_detail, ip=ip)
            else:
                _audit("share_download_incomplete", None, target=share_path,
                       detail=audit_detail + f" ({sent} bytes sent)", ip=ip)

    headers = {"Content-Disposition": f'attachment; filename="{rec["name"]}.zip"'}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    return Response(generate(), mimetype="application/zip", headers=headers)
