"""
modules/webdav.py — WebDAV network-drive server.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from datetime import datetime
from datetime import timezone
from email.utils import formatdate
from flask import Response
from flask import g
from flask import jsonify
from flask import request
from flask import send_file
from modules.winplat import IS_WIN
from pathlib import Path
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape
import mimetypes
import shutil
import time
import uuid
from modules.config import APP_DATA_DIR, FILE_ROOT, GUEST_QUOTA_MB, OS_NAME, SHARED_DIR, WEBDAV_EN
from modules.file_access import (
    _is_void_internal, TRASH_DIR, TRASH_DIRNAME, _dir_size, _external_link_path, _in_personal_folder, _is_within,
    _name_error, _personal_root, _protected_reason, _quota_error, _quota_remaining_bytes,
    _trash_load, _trash_save,
)
from modules.activity import _audit
from modules.auth import _acct_clear, _acct_fail, _acct_locked, _clear_fails, _fail, _ip, _is_locked, _password_ok
from modules.error_log import _err, _has_full_file_access
from modules.users import _get_user_by_username
from modules.file_ops import _is_internal_name, _needs_permanent_delete, _remove_tree
from modules.web import _trash_lock, app
from modules import twofactor as _twofa, winplat
from modules.limits import guarded_send


# ══════════════════════════════════════════════════════════════════════
# WEBDAV — mount Void OS as a network drive (Finder / Explorer / rclone /
# Cyberduck / most file-manager apps). Runs on the SAME port as everything
# else, so the existing Cloudflare Tunnel or reverse-proxy setup exposes it
# automatically with no extra config.
#
# WebDAV clients authenticate per-request with HTTP Basic Auth rather than
# a session cookie, so this is only safe over HTTPS (already true for both
# documented remote-access paths in the README). Failed attempts share the
# same IP lockout counter as the normal login form.
#
# Every path goes through the exact same _check_file_access rules as the
# browser Files app: a restricted user (guest, or a guest_file_view-off
# admin) sees only two top-level folders at the WebDAV root — "Shared" and
# "My Files" — mirroring the browser's guest virtual root, so the real
# home/personal/<other users> tree is never exposed, not even by name.
#
# Deliberately minimal:
#  - LOCK/UNLOCK are accepted but not actually enforced against concurrent
#    writers (Windows' built-in client refuses to mount writable shares
#    without a LOCK response; real conflict resolution is out of scope for
#    a single-user home server).
#  - Depth: infinity PROPFIND is rejected (same call Apache's mod_dav makes
#    by default) since a full recursive FILE_ROOT listing could be huge.
#    Every mainstream client falls back to per-directory PROPFIND anyway.
#  - Deletes go through the same Recycle Bin as the browser, not a
#    permanent delete.
# ══════════════════════════════════════════════════════════════════════
WEBDAV_PREFIX = "/webdav"

def _webdav_mounts(user: dict) -> dict:
    """Named top-level folders a restricted user sees at the WebDAV root."""
    return {"Shared": SHARED_DIR, "My Files": _personal_root(user)}

def _webdav_resolve(subpath: str, user: dict):
    """Map a WebDAV URL subpath to a real Path, enforcing the same rules
    as the browser Files app. Returns (kind, path):
      ("virtual_root", None) — restricted user at the WebDAV root
      ("real", Path)         — a concrete, permitted filesystem path
      (None, None)           — doesn't exist / not permitted / escapes root
    """
    parts = [p for p in subpath.split("/") if p and p not in (".", "..")]
    if IS_WIN and any(_name_error(p) for p in parts):
        # Same Windows naming rules as the Files app (drive letters, ":"
        # streams, CON/NUL...) -- a real Windows WebDAV client never sends
        # these for an existing file, so this only ever rejects bad writes.
        return None, None
    if _has_full_file_access(user):
        candidate = FILE_ROOT.joinpath(*parts).resolve()
        try:
            rel = candidate.relative_to(FILE_ROOT)
        except ValueError:
            linked = _external_link_path("/".join(parts))
            if linked is None:
                return None, None
            candidate = linked
            rel = candidate.relative_to(FILE_ROOT)
        if TRASH_DIRNAME in rel.parts:
            return None, None
        if _is_within(candidate, APP_DATA_DIR) or _is_void_internal(candidate):
            return None, None  # same rule as _safe(): App Store data and Void OS's own files aren't reachable
        return "real", candidate
    if not parts:
        return "virtual_root", None
    mounts = _webdav_mounts(user)
    if parts[0] not in mounts:
        return None, None
    base = mounts[parts[0]].resolve()
    candidate = base.joinpath(*parts[1:]).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None, None
    return "real", candidate

def _webdav_is_mount_root(path: Path, user: dict) -> bool:
    """True if `path` IS one of a restricted user's top-level mounts itself
    ('Shared' or 'My Files') — writable into, but never delete/move/rename-able."""
    if _has_full_file_access(user):
        return False
    return path in (m.resolve() for m in _webdav_mounts(user).values())

def _webdav_children(kind, path, user):
    """List (name, Path, is_dir) tuples for a collection, one level deep."""
    if kind == "virtual_root":
        out = []
        for name, p in _webdav_mounts(user).items():
            p.mkdir(parents=True, exist_ok=True)
            out.append((name, p, True))
        return out
    out = []
    try:
        for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if _is_internal_name(item.name): continue
            if _is_within(item, APP_DATA_DIR) or _is_void_internal(item): continue
            out.append((item.name, item, item.is_dir()))
    except (PermissionError, FileNotFoundError): pass
    return out

def _webdav_url_parts(path: Path, user: dict) -> list:
    """Reconstruct the WebDAV URL segments for a real Path, in whichever
    namespace (full tree, or a Shared/My Files mount) it belongs to."""
    if _has_full_file_access(user):
        return list(path.relative_to(FILE_ROOT).parts)
    for name, mount in _webdav_mounts(user).items():
        try: return [name, *path.relative_to(mount.resolve()).parts]
        except ValueError: continue
    return [path.name]

def _webdav_href(*parts) -> str:
    return WEBDAV_PREFIX + "/" + "/".join(quote(p) for p in parts if p)

def _webdav_audit_target(path: Path) -> str:
    try: return path.relative_to(FILE_ROOT).as_posix()
    except ValueError: return str(path)

def _webdav_trash(path: Path, user: dict):
    """Soft-delete, mirroring api_delete's non-permanent branch so a WebDAV
    DELETE lands in the same Recycle Bin as everything else. Items on
    another drive are deleted permanently instead (see
    _needs_permanent_delete) — WebDAV clients can't show a confirmation,
    and Windows Explorer / macOS Finder already treat deletes on network
    drives as permanent, so that's what their users expect anyway."""
    rel = _webdav_audit_target(path)
    if _needs_permanent_delete(path):
        _remove_tree(path)
        _audit("delete_permanent", user, target=rel, detail="external drive — Recycle Bin bypassed")
        return
    tid = uuid.uuid4().hex
    is_dir = path.is_dir()
    trashed_name = f"{tid}__{path.name}"
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRASH_DIR / trashed_name
    shutil.move(str(path), str(dest))
    size = _dir_size(dest) if is_dir else dest.stat().st_size
    with _trash_lock:
        items = _trash_load()
        items.append({
            "id": tid, "trashed_name": trashed_name,
            "original_path": rel, "name": path.name,
            "type": "dir" if is_dir else "file", "size": size,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "deleted_by": user.get("username", ""),
        })
        _trash_save(items)
    _audit("trash", user, target=rel, detail="via WebDAV")

def _webdav_dest_subpath():
    """Parse the Destination header (MOVE/COPY) down to a WEBDAV_PREFIX-relative subpath."""
    dest = request.headers.get("Destination", "")
    if not dest: return None
    path = unquote(urlparse(dest).path)
    if path == WEBDAV_PREFIX: return ""
    prefix = WEBDAV_PREFIX + "/"
    if not path.startswith(prefix): return None
    return path[len(prefix):].strip("/")

def _webdav_auth() -> dict | None:
    """HTTP Basic Auth against the same user store as the browser login,
    sharing the same per-IP brute-force lockout."""
    ip = _ip()
    locked, _rem = _is_locked(ip)
    if locked: return None
    auth = request.authorization
    if not auth or not auth.username or not auth.password: return None
    if _acct_locked(auth.username)[0]: return None
    user = _get_user_by_username(auth.username.strip().lower())
    if not _password_ok(user, auth.password):
        _fail(ip); _acct_fail(auth.username); _audit("login_failed", None, target=auth.username, detail="via WebDAV")
        return None
    if _twofa.is_enabled(user):
        # WebDAV clients can only send a password, never a one-time code, so letting
        # the password alone in would bypass the second factor.
        g._dav_2fa_blocked = True
        return None
    _clear_fails(ip); _acct_clear(auth.username)
    return user

class _QuotaExceeded(Exception):
    pass

def _webdav_unauthorized():
    if getattr(g, "_dav_2fa_blocked", False):
        return Response("WebDAV is turned off for accounts that use two-factor sign-in.", 403)
    r = Response("Unauthorized", 401)
    r.headers["WWW-Authenticate"] = f'Basic realm="{OS_NAME}"'
    return r

_DAV_NS = 'xmlns:D="DAV:"'

def _dav_entry(href, name, is_dir, size, mtime) -> str:
    rtype = "<D:collection/>" if is_dir else ""
    extra = "" if is_dir else (
        f"<D:getcontentlength>{size}</D:getcontentlength>"
        f"<D:getcontenttype>{xml_escape(mimetypes.guess_type(name)[0] or 'application/octet-stream')}</D:getcontenttype>")
    return (f"<D:response><D:href>{xml_escape(href)}</D:href><D:propstat><D:prop>"
            f"<D:displayname>{xml_escape(name)}</D:displayname>"
            f"<D:resourcetype>{rtype}</D:resourcetype>"
            f"<D:getlastmodified>{formatdate(mtime, usegmt=True)}</D:getlastmodified>"
            f"{extra}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")

_WEBDAV_METHODS = ["OPTIONS","PROPFIND","GET","HEAD","PUT","DELETE","MKCOL","MOVE","COPY","LOCK","UNLOCK"]

@app.route(WEBDAV_PREFIX, methods=_WEBDAV_METHODS)
@app.route(f"{WEBDAV_PREFIX}/", methods=_WEBDAV_METHODS)
@app.route(f"{WEBDAV_PREFIX}/<path:subpath>", methods=_WEBDAV_METHODS)
def webdav_dispatch(subpath=""):
    if not WEBDAV_EN:
        return jsonify({"error": "WebDAV is disabled"}), 404

    method = request.method
    if method == "OPTIONS":  # capability probe — no auth required, reveals nothing
        r = Response("")
        r.headers["DAV"] = "1,2"
        r.headers["Allow"] = ",".join(_WEBDAV_METHODS)
        r.headers["MS-Author-Via"] = "DAV"
        return r

    user = _webdav_auth()
    if not user:
        return _webdav_unauthorized()

    kind, path = _webdav_resolve(subpath, user)

    if method == "PROPFIND":
        if kind is None: return jsonify({"error": "Not found"}), 404
        depth = request.headers.get("Depth", "1")
        # Spec default for a missing header is "infinity"; we treat it as "1"
        # instead, since infinity is rejected below anyway and this avoids
        # unnecessarily rejecting clients that simply omit the header.
        if depth == "infinity":
            return Response("Depth: infinity is not supported — request one level at a time",
                             403, mimetype="text/plain")
        if kind == "virtual_root":
            self_entry = _dav_entry(_webdav_href(), OS_NAME, True, None, time.time())
            children = _webdav_children(kind, path, user) if depth == "1" else []
        else:
            try: st = path.stat()
            except FileNotFoundError: return jsonify({"error": "Not found"}), 404
            is_dir = path.is_dir()
            self_parts = _webdav_url_parts(path, user)
            self_entry = _dav_entry(_webdav_href(*self_parts), path.name or OS_NAME,
                                     is_dir, None if is_dir else st.st_size, st.st_mtime)
            children = _webdav_children(kind, path, user) if (is_dir and depth == "1") else []
        body = [self_entry]
        for name, cpath, is_dir in children:
            try: cst = cpath.stat()
            except (FileNotFoundError, PermissionError): continue
            href_parts = [name] if kind == "virtual_root" else _webdav_url_parts(cpath, user)
            body.append(_dav_entry(_webdav_href(*href_parts), name, is_dir,
                                    None if is_dir else cst.st_size, cst.st_mtime))
        xml = (f'<?xml version="1.0" encoding="utf-8"?><D:multistatus {_DAV_NS}>{"".join(body)}</D:multistatus>')
        return Response(xml, 207, mimetype="application/xml; charset=utf-8")

    if method in ("GET", "HEAD"):
        if kind != "real" or not path.is_file(): return jsonify({"error": "Not found"}), 404
        return guarded_send(lambda: send_file(path, conditional=True), user_id=user["id"])

    if method == "MKCOL":
        if request.get_data(): return Response("", 415)  # body not supported
        if kind is None: return jsonify({"error": "Access denied"}), 403
        if kind == "virtual_root": return jsonify({"error": "Cannot create a collection at the WebDAV root"}), 403
        if path.exists(): return Response("", 405)
        if not path.parent.exists(): return Response("", 409)
        try:
            path.mkdir()
            _audit("mkdir", user, target=_webdav_audit_target(path), detail="via WebDAV")
            return Response("", 201)
        except Exception as e: return _err(e)

    if method == "PUT":
        if kind is None: return jsonify({"error": "Access denied"}), 403
        if kind == "virtual_root": return jsonify({"error": "Cannot write to the WebDAV root"}), 403
        if not path.parent.exists(): return Response("", 409)
        if path.is_dir(): return jsonify({"error": "Is a directory"}), 409
        # Streamed to a temp file in the same folder, then atomically renamed:
        # request.get_data() used to pull the whole body (up to
        # VOID_MAX_UPLOAD_MB) into RAM per request, *before* the quota check
        # -- a few parallel PUTs could exhaust a Pi's memory -- and a dropped
        # connection left a truncated file in place of the original.
        remaining = _quota_remaining_bytes(user) if _in_personal_folder(user, path) else None
        if remaining is not None and (request.content_length or 0) > remaining:
            return jsonify({"error": _quota_error(GUEST_QUOTA_MB)}), 507
        is_new = not path.exists()
        tmp = path.parent / f".{path.name}.voidput-{uuid.uuid4().hex[:8]}"
        written = 0
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = request.stream.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if remaining is not None and written > remaining:
                        raise _QuotaExceeded()
                    out.write(chunk)
            winplat.replace_with_retry(tmp, path)
            _audit("upload", user, target=_webdav_audit_target(path), detail=f"{written} bytes via WebDAV")
            return Response("", 201 if is_new else 204)
        except _QuotaExceeded:
            return jsonify({"error": _quota_error(GUEST_QUOTA_MB)}), 507
        except Exception as e:
            return _err(e)
        finally:
            try: tmp.unlink()
            except OSError: pass

    if method == "DELETE":
        if kind != "real" or not path.exists(): return jsonify({"error": "Not found"}), 404
        if path == FILE_ROOT or _webdav_is_mount_root(path, user) or _protected_reason(path):
            return jsonify({"error": "Cannot delete this folder"}), 403
        try:
            _webdav_trash(path, user)
            return Response("", 204)
        except Exception as e: return _err(e)

    if method in ("MOVE", "COPY"):
        if kind != "real" or not path.exists(): return jsonify({"error": "Not found"}), 404
        if method == "MOVE" and (path == FILE_ROOT or _webdav_is_mount_root(path, user) or _protected_reason(path)):
            return jsonify({"error": "Access denied"}), 403
        dest_sub = _webdav_dest_subpath()
        if dest_sub is None: return jsonify({"error": "Missing or invalid Destination header"}), 400
        dkind, dst = _webdav_resolve(dest_sub, user)
        if dkind != "real": return jsonify({"error": "Invalid destination"}), 403
        # The destination gets REPLACED (rmtree) when it exists and Overwrite
        # isn't "F", so it needs the same protection as a DELETE: before this,
        # "MOVE x  Destination: /webdav/" from any full-access account wiped
        # the entire file root, and a guest could do the same to Shared.
        if dst == FILE_ROOT or _webdav_is_mount_root(dst, user) or _protected_reason(dst):
            return jsonify({"error": "Invalid destination"}), 403
        if _is_within(dst, path):
            return jsonify({"error": "Cannot move or copy a collection into itself"}), 409
        overwrite = request.headers.get("Overwrite", "T").upper() != "F"
        existed = dst.exists()
        if existed and not overwrite: return jsonify({"error": "Destination exists"}), 412
        if _in_personal_folder(user, dst):
            remaining = _quota_remaining_bytes(user)
            if remaining is not None:
                incoming = _dir_size(path) if path.is_dir() else path.stat().st_size
                if incoming > remaining: return jsonify({"error": _quota_error(GUEST_QUOTA_MB)}), 507
        try:
            # Overwritten content goes to the Recycle Bin rather than being
            # destroyed outright -- same as every other delete path in the app.
            if existed: _webdav_trash(dst, user)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if method == "COPY":
                if path.is_dir(): shutil.copytree(path, dst, ignore=lambda d, names: [n for n in names if _is_void_internal(Path(d) / n)])
                else: shutil.copy2(path, dst)
            else: shutil.move(str(path), str(dst))
            _audit("copy" if method == "COPY" else "move", user,
                target=f"{_webdav_audit_target(path)} -> {_webdav_audit_target(dst)}", detail="via WebDAV")
            return Response("", 204 if existed else 201)
        except Exception as e: return _err(e)

    if method == "LOCK":
        # Best-effort only: always grants the lock, never actually enforces
        # exclusivity against a second writer. Present because Windows'
        # built-in WebDAV client refuses to treat a share as writable
        # without a LOCK response — real conflict resolution isn't a
        # meaningful concern for a single-user home server.
        if kind is None: return jsonify({"error": "Access denied"}), 403
        token = f"opaquelocktoken:{uuid.uuid4()}"
        xml = (f'<?xml version="1.0" encoding="utf-8"?><D:prop {_DAV_NS}><D:lockdiscovery>'
               f'<D:activelock><D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope>'
               f'<D:depth>0</D:depth><D:timeout>Second-600</D:timeout>'
               f'<D:locktoken><D:href>{token}</D:href></D:locktoken></D:activelock>'
               f'</D:lockdiscovery></D:prop>')
        r = Response(xml, 200, mimetype="application/xml; charset=utf-8")
        r.headers["Lock-Token"] = f"<{token}>"
        return r

    if method == "UNLOCK":
        return Response("", 204)

    return jsonify({"error": "Method not allowed"}), 405
