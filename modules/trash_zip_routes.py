"""
modules/trash_zip_routes.py — Delete, Recycle Bin, search, downloads, zip downloads.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from datetime import datetime
from datetime import timezone
from flask import after_this_request
from flask import jsonify
from flask import request
from flask import send_file
from pathlib import Path
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from modules.config import APP_DATA_DIR, FILE_ROOT, SHARED_DIR, USERS_DIR, ZIP_TMP_DIR
from modules.file_access import (
    TRASH_DIR, TRASH_DIRNAME, _check_file_access, _dir_size, _is_within, _name_error, _protected_reason,
    _is_void_internal, _safe, _trash_load, _trash_save, _trash_visible, _walk_files,
)
from modules.activity import _audit
from modules.error_log import _current_user, _err, _has_full_file_access, _log_exception
from modules.file_helpers import _ft
from modules.file_ops import _is_internal_name, _needs_permanent_delete
from modules.web import _register_job_canceller, _trash_lock, _zip_jobs, app
from modules.limits import guarded_send
from modules import errors
from modules.decorators import require_auth


@app.route("/api/file/delete", methods=["POST"])
@require_auth
def api_delete():
    user=_current_user(); body=request.get_json(silent=True) or {}
    paths=body.get("paths",[]); permanent=bool(body.get("permanent", False))
    if not paths: return jsonify({"error":"No paths"}), 400
    results=[]
    with _trash_lock:
        trash_items = None if permanent else _trash_load()
        for raw in paths:
            try: p=_safe(raw, user)
            except PermissionError as e: results.append({"path":raw,"ok":False,"error":str(e)}); continue
            if p==FILE_ROOT: results.append({"path":raw,"ok":False,"error":"Cannot delete root"}); continue
            try: rel = p.relative_to(FILE_ROOT).as_posix()
            except ValueError: rel = raw
            if rel==TRASH_DIRNAME or rel.startswith(TRASH_DIRNAME+"/"):
                results.append({"path":raw,"ok":False,"error":"Not found"}); continue
            if not p.exists(): results.append({"path":raw,"ok":False,"error":"Not found"}); continue
            _prot = _protected_reason(p)
            if _prot:
                # Includes mounted USB drives / network shares: moving one to Trash
                # would fall back to copy-then-delete across filesystems, i.e. wipe it.
                results.append({"path":raw,"ok":False,"error":f"Can't delete {_prot}"}); continue
            try:
                if permanent:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    _audit("delete_permanent", user, target=rel)
                    results.append({"path":raw,"ok":True})
                elif _needs_permanent_delete(p):
                    results.append({"path":raw,"ok":False,"code":"permanent_required",
                        "error":"This is on an external drive, so it can't go to the Recycle Bin — delete it permanently instead."})
                else:
                    tid = uuid.uuid4().hex
                    is_dir = p.is_dir()
                    trashed_name = f"{tid}__{p.name}"
                    TRASH_DIR.mkdir(parents=True, exist_ok=True)
                    dest = TRASH_DIR / trashed_name
                    shutil.move(str(p), str(dest))
                    size = _dir_size(dest) if is_dir else dest.stat().st_size
                    trash_items.append({
                        "id": tid, "trashed_name": trashed_name,
                        "original_path": rel, "name": p.name,
                        "type": "dir" if is_dir else "file", "size": size,
                        "deleted_at": datetime.now(timezone.utc).isoformat(),
                        "deleted_by": user.get("username",""),
                    })
                    _audit("trash", user, target=rel)
                    results.append({"path":raw,"ok":True,"trashed":True})
            except Exception as e: results.append({"path":raw,"ok":False,"error":errors.describe(e)})
        if trash_items is not None: _trash_save(trash_items)
    return jsonify({"results":results})

@app.route("/api/trash")
@require_auth
def api_trash_list():
    user=_current_user()
    items = _trash_visible(_trash_load(), user)
    items = sorted(items, key=lambda x: x.get("deleted_at",""), reverse=True)
    return jsonify({"items": items})

@app.route("/api/trash/restore", methods=["POST"])
@require_auth
def api_trash_restore():
    user=_current_user(); ids=set((request.get_json(silent=True) or {}).get("ids", []))
    if not ids: return jsonify({"error":"No ids"}), 400
    with _trash_lock:
        all_items = _trash_load()
        visible_ids = {it["id"] for it in _trash_visible(all_items, user)}
        remaining=[]; results=[]
        for it in all_items:
            if it["id"] not in ids:
                remaining.append(it); continue
            if it["id"] not in visible_ids:
                remaining.append(it); results.append({"id":it["id"],"ok":False,"error":"Access denied"}); continue
            src = TRASH_DIR / it["trashed_name"]
            dest = FILE_ROOT / it["original_path"]
            try:
                if not src.exists(): raise FileNotFoundError("Trashed item is missing on disk")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    stem, suf = (dest.stem, dest.suffix) if dest.suffix and it["type"]=="file" else (dest.name, "")
                    cand, i = dest, 1
                    while cand.exists():
                        cand = dest.parent / f"{stem} (restored {i}){suf}"; i += 1
                    dest = cand
                shutil.move(str(src), str(dest))
                _audit("restore", user, target=it["original_path"])
                results.append({"id":it["id"],"ok":True,"path":dest.relative_to(FILE_ROOT).as_posix()})
            except Exception as e:
                remaining.append(it); results.append({"id":it["id"],"ok":False,"error":errors.describe(e)})
        _trash_save(remaining)
    return jsonify({"results": results})

@app.route("/api/trash/delete", methods=["POST"])
@require_auth
def api_trash_delete():
    user=_current_user(); ids=set((request.get_json(silent=True) or {}).get("ids", []))
    if not ids: return jsonify({"error":"No ids"}), 400
    with _trash_lock:
        all_items = _trash_load()
        visible_ids = {it["id"] for it in _trash_visible(all_items, user)}
        remaining=[]; results=[]
        for it in all_items:
            if it["id"] not in ids:
                remaining.append(it); continue
            if it["id"] not in visible_ids:
                remaining.append(it); results.append({"id":it["id"],"ok":False,"error":"Access denied"}); continue
            p = TRASH_DIR / it["trashed_name"]
            try:
                if p.exists(): shutil.rmtree(p) if p.is_dir() else p.unlink()
                _audit("delete_permanent", user, target=it["original_path"])
                results.append({"id":it["id"],"ok":True})
            except Exception as e:
                remaining.append(it); results.append({"id":it["id"],"ok":False,"error":errors.describe(e)})
        _trash_save(remaining)
    return jsonify({"results": results})

@app.route("/api/trash/empty", methods=["POST"])
@require_auth
def api_trash_empty():
    user=_current_user()
    with _trash_lock:
        all_items = _trash_load()
        visible = _trash_visible(all_items, user)
        visible_ids = {it["id"] for it in visible}
        remaining = [it for it in all_items if it["id"] not in visible_ids]
        emptied=0
        for it in visible:
            p = TRASH_DIR / it["trashed_name"]
            try:
                if p.exists(): shutil.rmtree(p) if p.is_dir() else p.unlink()
                emptied += 1
            except Exception:
                remaining.append(it)
        _trash_save(remaining)
    if emptied: _audit("empty_trash", user, detail=f"{emptied} item(s)")
    return jsonify({"ok": True, "emptied": emptied})

MAX_CONCURRENT_SEARCHES_PER_USER = 3
_search_inflight = {}  # user_id -> count of currently-running searches
_search_inflight_lock = threading.Lock()

@app.route("/api/files/search")
@require_auth
def api_files_search():
    user = _current_user()
    with _search_inflight_lock:
        cur = _search_inflight.get(user["id"], 0)
        if cur >= MAX_CONCURRENT_SEARCHES_PER_USER:
            return jsonify({"error": f"You already have {cur} search(es) running — "
                                      "wait for one to finish before starting another"}), 429
        _search_inflight[user["id"]] = cur + 1
    try:
        return _run_files_search(user)
    finally:
        with _search_inflight_lock:
            n = _search_inflight.get(user["id"], 1) - 1
            if n <= 0: _search_inflight.pop(user["id"], None)
            else: _search_inflight[user["id"]] = n

def _run_files_search(user):
    q = request.args.get("q","").strip()
    if len(q) < 2: return jsonify({"error":"Query must be at least 2 characters"}), 400
    scope_raw = request.args.get("path","")
    try: scope = _safe(scope_raw, user) if scope_raw else None
    except PermissionError as e: return jsonify({"error":str(e)}), 403

    if scope:
        roots = [scope]
    elif _has_full_file_access(user):
        roots = [FILE_ROOT]
    else:
        pf = user.get("personal_folder", user.get("username",""))
        roots = [SHARED_DIR, USERS_DIR/pf]

    qlow = q.lower()
    MAX_RESULTS = 150
    MAX_SCANNED = 20000   # cap total files/dirs visited even if nothing matches yet
    MAX_SECONDS = 8        # wall-clock budget for a single search request
    CONTENT_MAX_BYTES = 512*1024
    results = []
    scanned = 0
    deadline = time.time() + MAX_SECONDS
    truncated_by_budget = False
    for root in roots:
        if not root or not root.exists(): continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _is_internal_name(d)
                           and not _is_within(Path(dirpath) / d, APP_DATA_DIR)    # App Store data is off limits, as in _safe()
                           and not _is_void_internal(Path(dirpath) / d)]          # ...and so are Void OS's own files
            filenames = [f for f in filenames if not _is_internal_name(f)
                         and not _is_void_internal(Path(dirpath) / f)]
            dp = Path(dirpath)
            if not _has_full_file_access(user) and not _check_file_access(dp, user):
                dirnames[:] = []; continue
            for name in dirnames + filenames:
                if len(results) >= MAX_RESULTS: break
                scanned += 1
                if scanned >= MAX_SCANNED or time.time() >= deadline:
                    truncated_by_budget = True; dirnames[:] = []; break
                full = dp / name
                is_dir = full.is_dir()
                try: rel = full.relative_to(FILE_ROOT).as_posix()
                except ValueError: continue
                name_match = qlow in name.lower()
                content_match, snippet = False, None
                if not is_dir and not name_match and _ft(full) == "text":
                    try:
                        if full.stat().st_size <= CONTENT_MAX_BYTES:
                            text = full.read_text(encoding="utf-8", errors="ignore")
                            idx = text.lower().find(qlow)
                            if idx != -1:
                                content_match = True
                                start = max(0, idx-40)
                                snippet = text[start:idx+len(q)+40].replace("\n"," ").strip()
                    except Exception: pass
                if name_match or content_match:
                    try:
                        st = full.stat()
                        results.append({"name":name, "path":rel,
                            "type":"dir" if is_dir else _ft(full),
                            "size": st.st_size if not is_dir else None,
                            "modified": int(st.st_mtime),
                            "ext": full.suffix.lstrip(".").lower() if not is_dir else None,
                            "match": "content" if content_match else "name",
                            "snippet": snippet})
                    except Exception: pass
            if len(results) >= MAX_RESULTS or truncated_by_budget: break
        if len(results) >= MAX_RESULTS or truncated_by_budget: break
    return jsonify({"query": q, "results": results,
        "truncated": len(results) >= MAX_RESULTS or truncated_by_budget})

@app.route("/api/file/mkdir", methods=["POST"])
@require_auth
def api_mkdir():
    user=_current_user(); raw=(request.get_json(silent=True) or {}).get("path","")
    for _part in [x for x in str(raw).replace("\\","/").split("/") if x]:
        nerr=_name_error(_part)
        if nerr: return jsonify({"error":nerr}), 400
    try: p=_safe(raw, user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    try:
        p.mkdir(parents=True,exist_ok=True)
        _audit("mkdir", user, target=p.relative_to(FILE_ROOT).as_posix())
        return jsonify({"ok":True})
    except Exception as e: return _err(e)

@app.route("/api/file/download")
@require_auth
def api_file_download():
    """Raw-bytes download for a single file, for ANY file type. /api/file
    (above) is the in-app viewer — it returns JSON for text/archive/binary
    files rather than raw bytes, so it was never safe to use for downloads;
    this is the dedicated download route."""
    user = _current_user()
    raw = request.args.get("path","")
    try: p = _safe(raw, user)
    except PermissionError as e: return jsonify({"error":str(e)}), 403
    if not p.exists() or not p.is_file(): return jsonify({"error":"File not found"}), 404
    return guarded_send(lambda: send_file(p, as_attachment=True, download_name=p.name), user_id=user["id"])


def _zip_cleanup_stale(max_age=1800):
    now = time.time()
    for jid in [j for j,v in _zip_jobs.items() if now - v["created"] > max_age]:
        job = _zip_jobs.pop(jid, None)
        if job and job.get("tmp_path"):
            try: os.remove(job["tmp_path"])
            except OSError: pass

def _zip_worker(job_id, paths, user, resolver=None):
    resolver = resolver or (lambda raw: _safe(raw, user))
    job = _zip_jobs[job_id]
    try:
        files = []
        for raw in paths[:100]:
            try:
                p = resolver(raw)
                if p.is_file(): files.append((p, p.name))
                elif p.is_dir():
                    for f in _walk_files(p):
                        files.append((f, f.relative_to(p.parent)))
                else:
                    job["skipped"] += 1  # gone, or not something that can be zipped
            except Exception:
                job["skipped"] += 1  # not allowed / unreadable
        job["total"] = len(files)
        fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="voidos-dl-", dir=str(ZIP_TMP_DIR))
        os.close(fd)
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p, arcname in files:
                if job.get("cancelled"): break
                try: zf.write(p, arcname)
                except Exception: job["skipped"] += 1  # locked or unreadable file: the zip still gets built without it
                job["processed"] += 1
        if job.get("cancelled"):
            try: os.remove(tmp_path)
            except OSError: pass
            _zip_jobs.pop(job_id, None)
            return
        job["tmp_path"] = tmp_path
        job["done"] = True
    except Exception as e:
        job["error"] = f"{errors.describe(e)} (ref {_log_exception(e, source='job', context='zip')})"; job["done"] = True

MAX_ZIP_JOBS_PER_USER = 3

def _cancel_zips_for_user(user_id: str, why: str):
    for job in list(_zip_jobs.values()):
        if job.get("user_id") == user_id and not job.get("done"):
            job["cancelled"] = True

_register_job_canceller(_cancel_zips_for_user)


@app.route("/api/file/zip", methods=["POST"])
@require_auth
def api_zip_start():
    user = _current_user()
    body = request.get_json(silent=True) or {}
    paths = body.get("paths", [])
    if not paths: return jsonify({"error":"No paths"}), 400
    resolver = lambda raw: _safe(raw, user)
    _zip_cleanup_stale()
    in_flight = sum(1 for j in _zip_jobs.values() if j.get("user_id") == user["id"] and not j.get("done"))
    if in_flight >= MAX_ZIP_JOBS_PER_USER:
        return jsonify({"error": f"You already have {in_flight} zip download(s) in progress — "
                                  "wait for one to finish before starting another"}), 429
    job_id = uuid.uuid4().hex
    _zip_jobs[job_id] = {"processed":0,"total":0,"done":False,"error":None,
                          "tmp_path":None,"cancelled":False,"created":time.time(),
                          "user_id":user["id"],"skipped":0}
    threading.Thread(target=_zip_worker, args=(job_id, paths, user, resolver), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/file/zip/progress/<job_id>")
@require_auth
def api_zip_progress(job_id):
    job = _zip_jobs.get(job_id)
    if not job or job.get("user_id") != _current_user()["id"]: return jsonify({"error":"Not found"}), 404
    return jsonify({"processed":job["processed"],"total":job["total"],
                     "done":job["done"],"error":job["error"],"skipped":job.get("skipped",0)})

@app.route("/api/file/zip/download/<job_id>")
@require_auth
def api_zip_download(job_id):
    job = _zip_jobs.get(job_id)
    if not job or job.get("user_id") != _current_user()["id"]: return jsonify({"error":"Not found"}), 404
    if job.get("error"): return jsonify({"error": job["error"]}), 404
    if not job["done"] or not job["tmp_path"] or not os.path.exists(job["tmp_path"]):
        return jsonify({"error":"Not ready"}), 409
    tmp_path = job["tmp_path"]
    @after_this_request
    def _cleanup(resp):
        try: os.remove(tmp_path)
        except OSError: pass
        _zip_jobs.pop(job_id, None)
        return resp
    return guarded_send(lambda: send_file(tmp_path, mimetype="application/zip",
        as_attachment=True, download_name="void-os-files.zip"), user_id=_current_user()["id"])

@app.route("/api/file/zip/<job_id>", methods=["DELETE"])
@require_auth
def api_zip_cancel(job_id):
    job = _zip_jobs.get(job_id)
    if job and job.get("user_id") == _current_user()["id"]: job["cancelled"] = True
    return jsonify({"ok": True})
