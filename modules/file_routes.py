"""
modules/file_routes.py — Browsing, viewing, editing, renaming and uploading files.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify
from flask import request
from flask import send_file
from pathlib import Path
import errno
import mimetypes
import os
import shutil
import time
import uuid
import zipfile
from modules.config import (
    APP_DATA_DIR, DRIVES_REL, EXTERNAL_DIR, FILE_ROOT, GUEST_QUOTA_MB, SHARED_REL, UPLOAD_SESSION_TTL_S, USERS_REL,
)
from modules.limits import guarded_send
from modules.error_log import _api_error, _current_user, _err, _has_full_file_access, _log_exception
from modules.activity import _audit
from modules.file_access import (
    _dir_size, _guest_breadcrumb, _guest_virtual_root, _in_personal_folder, _is_void_internal, _is_within,
    _name_error, _protected_reason, _quota_error, _quota_remaining_bytes, _safe,
    _ensure_personal_folder, _free_space_problem, _shared_quota_error, _shared_remaining_bytes,
)
from modules.file_helpers import _ft
from modules.web import _upload_sessions, _upload_sessions_lock, app
from modules import errors, winplat
from modules.decorators import require_auth


# ══════════════════════════════════════════════════════════════════════
# ROUTES — FILES (permission-enforced on every endpoint)
# ══════════════════════════════════════════════════════════════════════
@app.route("/api/files/places")
@require_auth
def api_files_places():
    """What the Files sidebar offers this account: its own folder (every rank has one)
    with the folders inside it, Shared and, for accounts that can see everything, Users,
    each connected drive, and the whole server folder."""
    user = _current_user()
    me = _ensure_personal_folder(user)
    full = _has_full_file_access(user)
    out = {"full": full,
           "me": {"name": "My Files", "path": me.relative_to(FILE_ROOT).as_posix(), "folders": []},
           "shared": {"name": "Shared", "path": SHARED_REL}}
    try:
        for d in sorted(me.iterdir(), key=lambda x: x.name.lower()):
            if d.is_dir() and not _is_internal_name(d.name) and not d.name.startswith("."):
                out["me"]["folders"].append({"name": d.name, "path": d.relative_to(FILE_ROOT).as_posix()})
    except OSError:
        pass
    out["me"]["folders"] = out["me"]["folders"][:12]
    if full:
        out["users"] = {"name": "Users", "path": USERS_REL}
        out["root"] = {"name": "Server files", "path": ""}
        if user.get("role") == "superuser":
            try:      # only reachable through the Files app when it sits inside the files folder
                out["app_data"] = {"name": "App data", "path": APP_DATA_DIR.relative_to(FILE_ROOT).as_posix()}                     if APP_DATA_DIR.is_dir() else None
            except ValueError:
                out["app_data"] = None
        drives = []
        try:
            for d in sorted(EXTERNAL_DIR.iterdir(), key=lambda x: x.name.lower()):
                if d.is_dir() and not _is_internal_name(d.name):
                    drives.append({"name": d.name, "path": f"{DRIVES_REL}/{d.name}"})
        except OSError:
            pass
        out["drives"] = {"name": "Drives", "path": DRIVES_REL, "items": drives}
    return jsonify(out)

@app.route("/api/files")
@require_auth
def api_files():
    user = _current_user()
    raw = request.args.get("path","")
    if not _has_full_file_access(user) and raw == "":
        return _guest_virtual_root(user)
    try: p = _safe(raw, user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not p.exists(): return jsonify({"error":"Not found"}), 404
    if not p.is_dir(): return jsonify({"error":"Not a directory"}), 400
    entries = []
    try:
        for item in sorted(p.iterdir(), key=lambda x:(not x.is_dir(),x.name.lower())):
            if _is_internal_name(item.name) or _is_void_internal(item):
                continue  # Recycle Bin, in-progress transfers (shown in the progress tray), Void OS's own files
            try:
                st = item.stat()
                entry = {"name":item.name,"type":"dir" if item.is_dir() else _ft(item),
                    "size":st.st_size if item.is_file() else None,"modified":int(st.st_mtime),
                    "ext":item.suffix.lstrip(".").lower() if item.is_file() else None,
                    "path":item.relative_to(FILE_ROOT).as_posix()}
                if _protected_reason(item): entry["protected"] = True  # structural: the page hides rename / cut / delete
                entries.append(entry)
            except: pass
    except PermissionError: return jsonify({"error":"Permission denied"}), 403
    rel = p.relative_to(FILE_ROOT).as_posix() if p != FILE_ROOT else ""
    rel_parts = Path(rel).parts if rel else ()
    if not _has_full_file_access(user):
        bc = _guest_breadcrumb(rel_parts, user)
    else:
        bc = []
        if rel and rel != ".":
            parts = Path(rel).parts
            for i, part in enumerate(parts):
                bc.append({"name":part,"path":"/".join(parts[:i+1])})
    return jsonify({"path":rel,"breadcrumb":bc,"entries":entries})

@app.route("/api/file")
@require_auth
def api_file():
    user = _current_user()
    raw = request.args.get("path","")
    try: p = _safe(raw, user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not p.exists() or not p.is_file(): return jsonify({"error":"File not found"}), 404
    ft = _ft(p)
    # conditional=True is already Flask's own default — made explicit here because the frontend
    # now actually depends on it: viewFile() sets video/audio elements' src= directly rather than
    # blob-fetching the whole file first, so Range request support (which conditional=True is what
    # enables) is what makes seeking/scrubbing in a large video work at all, not just a nice-to-have.
    # This route also needs to answer HEAD correctly (Flask handles that automatically for any GET
    # route) — viewFile() does a HEAD probe first to read Content-Type without transferring the file
    # body, since the actual transfer is the browser's own subsequent src=-triggered GET.
    if ft in ("image","video","audio","pdf"): return guarded_send(lambda: send_file(p, conditional=True), user_id=user["id"])
    if ft == "text":
        MAX=1024*1024; sz=p.stat().st_size
        with open(p,"rb") as fh: content=fh.read(MAX).decode("utf-8",errors="replace")
        return jsonify({"type":ft,"ext":p.suffix.lstrip(".").lower(),
            "content":content,"truncated":sz>MAX,"size":sz})
    if ft == "archive":
        info={"type":ft,"name":p.name,"size":p.stat().st_size,"contents":[]}
        if p.suffix.lower()==".zip" and info["size"] > 2*1024**3:
            info["error"]="This archive is too large to preview \u2014 download it to open it."
        elif p.suffix.lower()==".zip":
            try:
                with zipfile.ZipFile(p) as zf: info["contents"]=zf.namelist()[:300]
            except Exception as e: info["error"]="This archive couldn't be read — it may be damaged, incomplete, or not really a zip file."
        return jsonify(info)
    with open(p,"rb") as fh: rb=fh.read(256)
    lines=[]
    for i in range(0,len(rb),16):
        chunk=rb[i:i+16]
        lines.append(f"{i:08x}  {' '.join(f'{b:02x}' for b in chunk):<47}  "
                     f"{''.join(chr(b) if 32<=b<127 else '.' for b in chunk)}")
    return jsonify({"type":"binary","name":p.name,"size":p.stat().st_size,
        "mime":mimetypes.guess_type(str(p))[0] or "application/octet-stream",
        "hex":"\n".join(lines)})

@app.route("/api/file/edit", methods=["PUT"])
@require_auth
def api_edit():
    user=_current_user(); d=request.get_json(silent=True) or {}
    try: p=_safe(d.get("path",""), user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not p.exists() or not p.is_file(): return jsonify({"error":"Not found"}), 404
    if _ft(p)!="text": return jsonify({"error":"Only text files can be edited"}), 400
    if _in_personal_folder(user, p):
        remaining = _quota_remaining_bytes(user)
        grow = len(str(d.get("content","")).encode("utf-8")) - p.stat().st_size
        if remaining is not None and grow > remaining:
            return jsonify({"error": _quota_error(GUEST_QUOTA_MB)}), 400
    try:
        # Written beside the file and swapped in atomically: a full disk or a
        # crash mid-save used to leave the original truncated or empty.
        staging = _staging_path(p.parent, uuid.uuid4().hex[:8], p.name)
        try:
            staging.write_text(d.get("content",""), encoding="utf-8")
            try: shutil.copymode(p, staging)
            except OSError: pass
            winplat.replace_with_retry(staging, p)
        finally:
            if staging.exists():
                try: staging.unlink()
                except OSError: pass
        return jsonify({"ok":True,"size":p.stat().st_size})
    except Exception as e: return _err(e)

@app.route("/api/file/rename", methods=["POST"])
@require_auth
def api_rename():
    user=_current_user(); d=request.get_json(silent=True) or {}
    name=d.get("name","").strip()
    nerr=_name_error(name)
    if nerr:
        return jsonify({"error":nerr}), 400
    try: old=_safe(d.get("path",""), user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not old.exists(): return jsonify({"error":"Not found"}), 404
    _prot=_protected_reason(old)
    if _prot: return jsonify({"error":f"Can't rename {_prot}"}), 403
    new=old.parent/name
    if new.exists(): return jsonify({"error":"Name already exists"}), 409
    try:
        old.rename(new)
        _audit("rename", user, target=f"{old.relative_to(FILE_ROOT).as_posix()} -> {new.relative_to(FILE_ROOT).as_posix()}")
        return jsonify({"ok":True,"path":new.relative_to(FILE_ROOT).as_posix(),"name":name})
    except Exception as e: return _err(e)

@app.route("/api/file/move", methods=["POST"])
@require_auth
def api_move():
    user=_current_user(); d=request.get_json(silent=True) or {}; copy=bool(d.get("copy",False))
    try: src=_safe(d.get("src",""),user); dst=_safe(d.get("dst",""),user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not src.exists(): return jsonify({"error":"Source not found"}), 404
    _prot=_protected_reason(src)
    if _prot and not copy: return jsonify({"error":f"Can't move {_prot}"}), 403
    if _is_within(dst, src):
        # copytree into its own subtree recurses until the disk fills up
        return jsonify({"error":"Can't move or copy a folder into itself"}), 400
    if dst.exists() and not d.get("overwrite",False):
        return jsonify({"error":"Destination already exists"}), 409
    if _in_personal_folder(user, dst):
        remaining = _quota_remaining_bytes(user)
        if remaining is not None:
            incoming = _dir_size(src) if src.is_dir() else src.stat().st_size
            if incoming > remaining:
                return jsonify({"error": _quota_error(GUEST_QUOTA_MB)}), 400
    try:
        if copy:
            if src.is_dir(): shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if _is_void_internal(Path(d) / n)])
            else: shutil.copy2(src, dst)
        else: shutil.move(str(src),str(dst))
        _audit("copy" if copy else "move", user,
            target=f"{src.relative_to(FILE_ROOT).as_posix()} -> {dst.relative_to(FILE_ROOT).as_posix()}")
        return jsonify({"ok":True})
    except Exception as e: return _err(e)

@app.route("/api/file/upload", methods=["POST"])
@require_auth
def api_upload():
    user=_current_user(); raw=request.form.get("path","")
    try: dest_dir=_safe(raw, user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not dest_dir.is_dir(): return jsonify({"error":"Target must be a directory"}), 400
    saved,failed=[],[]
    remaining = _quota_remaining_bytes(user) if _in_personal_folder(user, dest_dir) else None
    shared_left = _shared_remaining_bytes(user, dest_dir)
    for f in request.files.getlist("files"):
        if not f.filename: continue
        name=winplat.sanitize_win_name(Path(f.filename.replace("\\","/")).name)
        if not name or _name_error(name): continue
        dest=dest_dir/name
        try:
            f.stream.seek(0, 2); fsize = f.stream.tell(); f.stream.seek(0)
        except Exception:
            fsize = 0
        if remaining is not None and fsize > remaining:
            failed.append({"name":name,"error":_quota_error(GUEST_QUOTA_MB)}); continue
        if shared_left is not None and fsize > shared_left:
            failed.append({"name":name,"error":_shared_quota_error()}); continue
        disk_msg = _free_space_problem(dest_dir, fsize)
        if disk_msg:
            failed.append({"name":name,"error":disk_msg,"code":"disk_full"}); continue
        try:
            # Written under a hidden staging name and renamed into place, so a
            # half-received file never shows up — and an interrupted upload
            # can't leave a truncated copy where the old file used to be.
            staging = _staging_path(dest_dir, uuid.uuid4().hex[:8], name)
            try:
                f.save(staging)
                if dest.is_dir():
                    raise FileExistsError(errno.EEXIST, "is a directory")
                os.replace(staging, dest)
            finally:
                if staging.exists():
                    try: staging.unlink()
                    except OSError: pass
            st=dest.stat()
            saved.append({"name":name,"path":dest.relative_to(FILE_ROOT).as_posix(),
                "type":_ft(dest),"size":st.st_size,"modified":int(st.st_mtime)})
            _audit("upload", user, target=dest.relative_to(FILE_ROOT).as_posix(), detail=f"{st.st_size} bytes")
            if remaining is not None: remaining -= st.st_size
            if shared_left is not None: shared_left -= st.st_size
        except Exception as e:
            st_, code_, msg_, hint_ = errors.friendly(e)
            failed.append({"name":name,"error":f"{msg_} {hint_}".strip(),"code":code_,
                           "ref":_log_exception(e) if st_>=500 else None})
    return jsonify({"ok":True,"saved":saved,"failed":failed})

# ══════════════════════════════════════════════════════════════════════
# CHUNKED UPLOADS — resumable, arbitrary-size uploads through Cloudflare's
# 100MB-per-request proxy cap. Alongside /api/file/upload above, not a
# replacement for it: small files still go through the single-request path.
# Sessions live in-memory (_upload_sessions), same single-process/workers=1
# assumption as APP_TUNNELS/INSTALL_JOBS/_zip_jobs — see README.md.
# ══════════════════════════════════════════════════════════════════════
def _upload_session_public(sess: dict) -> dict:
    return {"upload_id": sess["upload_id"], "received_chunks": sess["received_chunks"],
            "total_chunks": sess["total_chunks"], "bytes_received": sess["bytes_received"],
            "declared_size": sess["declared_size"]}

def _upload_sessions_cleanup_stale():
    now = time.time()
    with _upload_sessions_lock:
        stale = [uid for uid, s in _upload_sessions.items() if now - s["created"] > UPLOAD_SESSION_TTL_S]
        for uid in stale:
            sess = _upload_sessions.pop(uid, None)
            if sess and sess.get("tmp_path"):
                try: os.remove(sess["tmp_path"])
                except OSError: pass
                _staging_track(Path(sess["tmp_path"]), add=False)

def _upload_janitor():
    """Same shape as _zip_janitor — periodic sweep for abandoned .part files
    (closed tab, crashed browser, a resume that never came back) so
    UPLOAD_TMP_DIR doesn't grow unbounded."""
    while True:
        time.sleep(300)
        try: _upload_sessions_cleanup_stale()
        except Exception: pass

MAX_UPLOAD_SESSIONS_PER_USER = 6

@app.route("/api/file/upload/start", methods=["POST"])
@require_auth
def api_upload_start():
    user = _current_user()
    body = request.get_json(silent=True) or {}
    raw = body.get("path", "")
    filename = winplat.sanitize_win_name(Path(str(body.get("filename", "")).replace("\\", "/")).name)
    if filename and _name_error(filename):
        filename = ""
    size = body.get("size")
    total_chunks = body.get("total_chunks")
    chunk_size = body.get("chunk_size")
    if not filename or not isinstance(size, int) or size < 0 or not isinstance(total_chunks, int) or total_chunks < 1:
        return jsonify({"error": "Invalid upload request"}), 400
    try: dest_dir = _safe(raw, user)
    except PermissionError as e: return jsonify({"error": str(e)}), 403
    if not dest_dir.is_dir(): return jsonify({"error": "Target must be a directory"}), 400

    _upload_sessions_cleanup_stale()
    with _upload_sessions_lock:
        in_flight = sum(1 for s in _upload_sessions.values()
                         if s["user_id"] == user["id"] and s["received_chunks"] < s["total_chunks"])
        if in_flight >= MAX_UPLOAD_SESSIONS_PER_USER:
            return jsonify({"error": f"You already have {in_flight} upload(s) in progress — "
                                      "wait for one to finish before starting another"}), 429

    # Quota checked once, up front, against the *declared* total size — same
    # fail-fast principle as Panic Tier 2's permission check running before
    # Tier 1 executes anything. Nothing is written to disk if this fails.
    remaining = _quota_remaining_bytes(user) if _in_personal_folder(user, dest_dir) else None
    if remaining is not None and size > remaining:
        return jsonify({"error": _quota_error(GUEST_QUOTA_MB)}), 400
    shared_left = _shared_remaining_bytes(user, dest_dir)
    if shared_left is not None and size > shared_left:
        return _api_error(_shared_quota_error(), 507, "quota_exceeded")
    # The size is only a promise from the browser; check the drive really has room for it.
    disk_msg = _free_space_problem(dest_dir, size)
    if disk_msg:
        return _api_error(disk_msg, 507, "disk_full")

    upload_id = uuid.uuid4().hex
    # The partial file lives next to its final name (hidden from listings)
    # instead of in a temp dir that may be on another drive: finishing is
    # then an instant rename, never a long copy at 100% — and a folder you
    # can't write to fails here, at the start, not after the whole upload.
    tmp_path = str(_staging_path(dest_dir, f"up{upload_id[:8]}", filename))
    try:
        with open(tmp_path, "wb"):
            pass
    except OSError as e:
        return _err(e)
    _staging_track(Path(tmp_path))
    with _upload_sessions_lock:
        _upload_sessions[upload_id] = {
            "upload_id": upload_id, "user_id": user["id"], "dest_dir": dest_dir,
            "filename": filename, "declared_size": size,
            "chunk_size": chunk_size, "total_chunks": total_chunks,
            "received_chunks": 0, "bytes_received": 0,
            "tmp_path": tmp_path, "created": time.time(),
        }
    return jsonify({"upload_id": upload_id})

@app.route("/api/file/upload/chunk", methods=["POST"])
@require_auth
def api_upload_chunk():
    user = _current_user()
    upload_id = request.form.get("upload_id", "")
    try: chunk_index = int(request.form.get("chunk_index", ""))
    except ValueError: return jsonify({"error": "Invalid chunk_index"}), 400
    f = request.files.get("chunk")
    if not f: return jsonify({"error": "Missing chunk"}), 400

    with _upload_sessions_lock:
        sess = _upload_sessions.get(upload_id)
        if not sess or sess["user_id"] != user["id"]:
            return jsonify({"error": "Upload session not found or expired"}), 404
        # Sequential-only, per design — an out-of-order chunk means the
        # client's and server's view of progress have diverged (e.g. a
        # resume racing a still-in-flight retry of an earlier chunk), so
        # reject rather than silently accept out of order.
        if chunk_index != sess["received_chunks"]:
            return jsonify({"error": "Out of order chunk", "expected_index": sess["received_chunks"],
                             "received_chunks": sess["received_chunks"], "total_chunks": sess["total_chunks"]}), 409
        data = f.read()
        if sess["bytes_received"] + len(data) > sess["declared_size"]:
            return jsonify({"error": "That upload is larger than the size it was started with — cancel it and try again."}), 400
        disk_msg = _free_space_problem(Path(sess["tmp_path"]).parent, len(data))
        if disk_msg:
            return _api_error(disk_msg, 507, "disk_full")
        try:
            with open(sess["tmp_path"], "ab") as out:
                out.write(data)
        except Exception as e:
            return _err(e)
        sess["bytes_received"] += len(data)
        sess["received_chunks"] += 1
        return jsonify({"received_chunks": sess["received_chunks"], "total_chunks": sess["total_chunks"]})

@app.route("/api/file/upload/status/<upload_id>")
@require_auth
def api_upload_status(upload_id):
    user = _current_user()
    with _upload_sessions_lock:
        sess = _upload_sessions.get(upload_id)
        if not sess or sess["user_id"] != user["id"]:
            return jsonify({"error": "Upload session not found or expired"}), 404
        return jsonify(_upload_session_public(sess))

@app.route("/api/file/upload/finish", methods=["POST"])
@require_auth
def api_upload_finish():
    user = _current_user()
    body = request.get_json(silent=True) or {}
    upload_id = body.get("upload_id", "")
    with _upload_sessions_lock:
        sess = _upload_sessions.get(upload_id)
        if not sess or sess["user_id"] != user["id"]:
            return jsonify({"error": "Upload session not found or expired"}), 404
        if sess["received_chunks"] < sess["total_chunks"]:
            return jsonify({"error": "Not all chunks received yet",
                             "received_chunks": sess["received_chunks"], "total_chunks": sess["total_chunks"]}), 409
        tmp_path = Path(sess["tmp_path"])
        try:
            actual_size = tmp_path.stat().st_size
        except OSError:
            _upload_sessions.pop(upload_id, None)
            return jsonify({"error": "Upload temp file is missing"}), 500
        if actual_size != sess["declared_size"]:
            # Do not finalize or move anything — client can re-check /status
            # and retry finishing (or resume sending the remainder) once the
            # discrepancy is understood, per the spec's error-handling table.
            return jsonify({"error": f"Received {actual_size} bytes, expected {sess['declared_size']}"}), 400

        dest_dir = sess["dest_dir"]
        name = sess["filename"]
        dest = dest_dir / name
        try:
            # Re-validate the destination is still inside the user's access
            # scope at finish time too, not just at /start — cheap, and closes
            # any window where permissions changed mid-upload.
            _safe(str(dest.relative_to(FILE_ROOT)), user)
        except (PermissionError, ValueError) as e:
            return jsonify({"error": str(e)}), 403
        if dest.is_dir():
            return _api_error(f"A folder named \u201c{name}\u201d already exists there.", 409, "exists")
        try:
            os.replace(str(tmp_path), str(dest))  # same folder: atomic
            _staging_track(tmp_path, add=False)
            st = dest.stat()
        except Exception as e:
            return _err(e)

        _upload_sessions.pop(upload_id, None)

    _audit("upload", user, target=dest.relative_to(FILE_ROOT).as_posix(),
           detail=f"{st.st_size} bytes (chunked)")
    return jsonify({"ok": True, "name": name, "path": dest.relative_to(FILE_ROOT).as_posix(),
                     "type": _ft(dest), "size": st.st_size, "modified": int(st.st_mtime)})

@app.route("/api/file/upload/cancel", methods=["POST"])
@require_auth
def api_upload_cancel():
    user = _current_user()
    body = request.get_json(silent=True) or {}
    upload_id = body.get("upload_id", "")
    with _upload_sessions_lock:
        sess = _upload_sessions.get(upload_id)
        if not sess or sess["user_id"] != user["id"]:
            return jsonify({"ok": True})  # never let one account cancel another's upload
        _upload_sessions.pop(upload_id, None)
        if sess.get("tmp_path"):
            try: os.remove(sess["tmp_path"])
            except OSError: pass
            _staging_track(Path(sess["tmp_path"]), add=False)
    return jsonify({"ok": True})


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _is_internal_name, _staging_path, _staging_track
    from modules.file_ops import _is_internal_name, _staging_path, _staging_track
