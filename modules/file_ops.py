"""
modules/file_ops.py — Background copy / move / delete / import jobs with progress.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify
from flask import request
from modules.winplat import IS_WIN
from pathlib import Path
import json
import os
import psutil
import shutil
import threading
import time
import uuid
from modules.config import FILE_ROOT, GUEST_QUOTA_MB, MAX_FILE_JOBS_PER_USER, MIN_FREE_MB, SHARED_DIR, _void_path
from modules.file_access import (
    TRASH_DIR, TRASH_DIRNAME, _in_personal_folder, _is_within, _name_error, _protected_reason,
    _quota_error, _quota_remaining_bytes, _safe, _trash_load, _trash_save, _trash_visible,
    _is_void_internal, _walk_files, _shared_quota_error, _shared_remaining_bytes,
)
from modules.error_log import _api_error, _current_user, _log_exception
from modules.web import _atomic_write_json, _push_to_live_sessions, _register_job_canceller, _trash_lock, app
from modules.activity import _audit, _inapp_notify
from modules.cloud_routes import _cloud_authed_token
from modules import cloud_storage, errors, winplat
from modules.decorators import require_auth


# ══════════════════════════════════════════════════════════════════════
# FILE OPERATION JOBS (copy / move / trash / delete / empty trash)
# ══════════════════════════════════════════════════════════════════════
# Why this exists: /api/file/move did the whole copy inside one HTTP
# request, writing straight to the final name. So (1) the destination
# showed up in listings at once while it was still being written, (2) a
# long copy outlived proxy timeouts (cloudflared cuts requests at ~100s)
# — the browser reported failure while the server kept copying — and (3)
# there was no way to show progress or cancel.
#
# Now the request only VALIDATES (permissions, protected folders, name
# clashes, quota — so every "can't do that" error is immediate and
# specific) and returns a job id. A worker thread does the work:
#   - same-drive move  → a single rename: instant and atomic
#   - anything else    → copied in 1 MB chunks into a hidden staging entry
#                        (".voidpart-<job>-<name>") next to the destination,
#                        renamed into place only once complete
# Staging entries are hidden from listings, search and WebDAV, so an item
# appears in the folder exactly when it's finished — never half-written.
# Progress is pushed live over /ws/control (and pollable), each item's
# failure is reported individually, and Cancel removes the partial copy.
STAGING_PREFIX = ".voidpart-"
STAGING_REGISTRY = _void_path("VOID_STAGING_REGISTRY", "staging_registry.json")
_staging_lock = threading.Lock()

def _staging_path(dest_dir: Path, tag: str, name: str) -> Path:
    """Hidden in-progress entry next to the final destination. Same folder =
    same drive, so finishing is always an atomic rename."""
    return dest_dir / f"{STAGING_PREFIX}{tag}-{name[:150]}"

def _staging_track(p: Path, add: bool = True) -> None:
    with _staging_lock:
        try:
            reg = set(json.loads(STAGING_REGISTRY.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            reg = set()
        (reg.add if add else reg.discard)(str(p))
        try:
            _atomic_write_json(STAGING_REGISTRY, sorted(reg))
        except OSError:
            pass

def _staging_sweep_on_start() -> None:
    """No transfer or upload survives a restart, so any staging entry still
    registered is debris from a crash/kill — remove it."""
    with _staging_lock:
        try:
            reg = json.loads(STAGING_REGISTRY.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in reg:
            p = Path(raw)
            if STAGING_PREFIX not in p.name:
                continue  # never delete anything that isn't unmistakably ours
            try:
                if p.is_dir() and not p.is_symlink(): shutil.rmtree(p)
                elif p.exists(): p.unlink()
            except OSError:
                pass
        try: STAGING_REGISTRY.unlink()
        except OSError: pass
_FILE_OPS: dict = {}
_file_ops_lock = threading.Lock()
_file_ops_sem = threading.BoundedSemaphore(int(os.environ.get("VOID_MAX_PARALLEL_TRANSFERS", "2")))
_FILE_OP_KEEP_S = 1800  # finished jobs stay queryable this long

def _is_internal_name(name: str) -> bool:
    return name == TRASH_DIRNAME or name.startswith(STAGING_PREFIX) or ".voidput-" in name

class _OpCancelled(Exception):
    pass

class _OpReject(Exception):
    """A per-item validation failure with a user-facing message."""
    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message); self.message = message; self.code = code

def _rel(p: Path) -> str:
    try:
        return Path(os.path.normpath(p)).relative_to(FILE_ROOT).as_posix()
    except ValueError:
        return p.name

def _tree_size(p: Path) -> tuple[int, int]:
    """(bytes, files) under p, links not followed — matches what's copied."""
    if p.is_file() and not p.is_symlink():
        try: return p.stat().st_size, 1
        except OSError: return 0, 1
    total = files = 0
    for f in _walk_files(p):
        try: total += f.stat().st_size
        except OSError: pass
        files += 1
    return total, files

def _job_public(job: dict) -> dict:
    keys = ("id", "op", "state", "label", "created", "started", "finished", "total_bytes", "done_bytes",
            "total_items", "done_items", "total_files", "done_files", "current", "errors", "dest_dir",
            "src_dirs", "message", "speed", "eta", "ref")
    return {k: job.get(k) for k in keys}

def _job_push(job: dict, force: bool = False) -> None:
    now = time.time()
    if not force and now - job.get("_last_push", 0) < 0.3:
        return
    job["_last_push"] = now
    if job.get("started") and job.get("done_bytes") and job["state"] == "running":
        elapsed = max(0.5, now - job["started"])
        job["speed"] = int(job["done_bytes"] / elapsed)
        remaining = max(0, (job.get("total_bytes") or 0) - job["done_bytes"])
        job["eta"] = int(remaining / job["speed"]) if job["speed"] else None
    try:
        _push_to_live_sessions(job["user_id"], {"type": "file_op", "job": _job_public(job)})
    except Exception:
        pass

def _check_cancel(job: dict) -> None:
    if job.get("cancel"):
        raise _OpCancelled()

def _copy_file_progress(src: Path, dst: Path, job: dict) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            _check_cancel(job)
            buf = fi.read(1024 * 1024)
            if not buf:
                break
            fo.write(buf)
            job["done_bytes"] += len(buf)
            _job_push(job)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass  # timestamps/permission bits are best-effort (FAT/exFAT/network shares)
    job["done_files"] += 1

def _copy_tree_progress(src: Path, dst: Path, job: dict) -> int:
    """Copies src (file or folder) to dst. Returns how many links were
    skipped (never followed — same rule as zips and share links)."""
    if src.is_file():
        _copy_file_progress(src, dst, job)
        return 0
    skipped = 0
    dst.mkdir()
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel = Path(dirpath).relative_to(src)
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full) or winplat.is_link_dir(full) or _is_internal_name(d) or _is_void_internal(full):
                skipped += 1
                continue
            keep.append(d)
            (dst / rel / d).mkdir(exist_ok=True)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if full.is_symlink() or not full.is_file() or _is_void_internal(full):
                skipped += 1
                continue
            job["current"] = (rel / fn).as_posix() if str(rel) != "." else fn
            _copy_file_progress(full, dst / rel / fn, job)
    return skipped

def _remove_tree(p: Path, job: dict | None = None) -> None:
    if winplat.is_link_dir(p) or p.is_symlink():
        winplat.remove_link_dir(p) or p.unlink()
        return
    if p.is_dir():
        for dirpath, dirnames, filenames in os.walk(p, topdown=False):
            for fn in filenames:
                if job: _check_cancel(job)
                os.unlink(os.path.join(dirpath, fn))
                if job:
                    job["done_files"] += 1; _job_push(job)
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if os.path.islink(full) or winplat.is_link_dir(full):
                    winplat.remove_link_dir(full) or os.unlink(full)
                else:
                    os.rmdir(full)
        os.rmdir(p)
    else:
        p.unlink()
        if job:
            job["done_files"] += 1; _job_push(job)

def _same_device(a: Path, b_dir: Path) -> bool:
    """True when a rename from a into b_dir can't cross filesystems. On
    Windows, compare the drive the paths really live on: an external/ entry
    is a junction or symlink to another drive or a network share."""
    try:
        if IS_WIN:
            da = os.path.splitdrive(os.path.realpath(a))[0].lower()
            db = os.path.splitdrive(os.path.realpath(b_dir))[0].lower()
            return da == db
        return os.stat(a, follow_symlinks=False).st_dev == os.stat(b_dir).st_dev
    except OSError:
        return False

def _run_copy_move(job: dict, item: dict, user: dict) -> None:
    src, dst, move = item["src_p"], item["dst_p"], job["op"] == "move"
    job["current"] = src.name
    if dst.exists():
        if not item.get("overwrite"):
            raise _OpReject(f"\u201c{dst.name}\u201d already exists in the destination.", "exists")
        _webdav_trash(dst, user)  # replaced items go to the Recycle Bin, never silently destroyed
    if move and _same_device(src, dst.parent):
        os.replace(src, dst)  # same drive: instant and atomic
        job["done_bytes"] += item["bytes"]; job["done_files"] += item["files"]
        return
    staging = _staging_path(dst.parent, job['id'][:8], dst.name)
    item["staging"] = staging
    _staging_track(staging)
    try:
        skipped = _copy_tree_progress(src, staging, job)
        _check_cancel(job)
        if dst.exists():
            raise _OpReject(f"\u201c{dst.name}\u201d appeared in the destination while copying — nothing was overwritten.", "exists")
        os.rename(staging, dst)
        item.pop("staging", None)
        if skipped:
            job["errors"].append({"name": src.name, "code": "links_skipped",
                                  "error": f"{skipped} shortcut/link item(s) inside were skipped (links aren't copied)."})
    finally:
        if item.get("staging") and item["staging"].exists():
            try: _remove_tree(item["staging"])
            except OSError: pass
        _staging_track(staging, add=False)
    if move:
        _remove_tree(src)  # only after the copy is safely in place

def _needs_permanent_delete(p: Path) -> bool:
    """Items on a different drive than the Recycle Bin (USB sticks, network
    shares, any other mount) are deleted permanently rather than copied
    into the main drive's Recycle Bin — copying them there was slow and
    could fill the main disk. The user is asked to confirm this first."""
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return not _same_device(p, TRASH_DIR)

def _run_trash(job: dict, item: dict, user: dict) -> None:
    src = item["src_p"]
    job["current"] = src.name
    if _needs_permanent_delete(src):
        _remove_tree(src, job)
        item["permanent"] = True
        _audit("delete_permanent", user, target=_rel(src), detail="external drive — Recycle Bin bypassed")
        return
    _webdav_trash(src, user)
    job["done_bytes"] += item["bytes"]; job["done_files"] += item["files"]

def _unique_dest(dst: Path) -> Path:
    """name.ext → "name (2).ext", "name (3).ext"... first one that's free."""
    if not dst.exists():
        return dst
    stem, suf = dst.stem, dst.suffix
    for i in range(2, 1000):
        cand = dst.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
    raise _OpReject("Too many files with that name already exist there.", "exists")

class _CountingWriter:
    def __init__(self, fp, job):
        self.fp, self.job = fp, job
    def write(self, b):
        _check_cancel(self.job)
        n = self.fp.write(b)
        self.job["done_bytes"] += len(b)
        _job_push(self.job)
        return n
    def __getattr__(self, k):
        return getattr(self.fp, k)

def _run_cloud_import(job: dict, item: dict, user: dict) -> None:
    dst = item["dst_p"]
    staging = _staging_path(dst.parent, job["id"][:8], dst.name)
    _staging_track(staging)
    job["current"] = item["name"]
    try:
        tok = _cloud_authed_token(user, item["provider"])
        with open(staging, "wb") as fp:
            cloud_storage.download_to(item["provider"], tok, item["remote_id"], _CountingWriter(fp, job))
        final = _unique_dest(dst)  # never overwrite a local file with an import
        os.rename(staging, final)
        job["done_files"] += 1
        if final != dst:
            job["errors"].append({"name": item["name"], "code": "renamed",
                                  "error": f"A file with that name already existed, so it was saved as \u201c{final.name}\u201d."})
        _audit("cloud_import", user, target=_rel(final), detail=item["provider"])
    except cloud_storage.CloudError as e:
        raise _OpReject(str(e), "cloud_error")
    finally:
        if staging.exists():
            try: staging.unlink()
            except OSError: pass
        _staging_track(staging, add=False)

def _file_op_worker(job_id: str, user: dict) -> None:
    job = _FILE_OPS[job_id]
    with _file_ops_sem:
        if job.get("cancel"):
            job.update(state="cancelled", finished=time.time()); _job_push(job, True); return
        job.update(state="running", started=time.time()); _job_push(job, True)
        for item in job["_items"]:
            if job.get("cancel"):
                break
            try:
                if job["op"] in ("copy", "move"):
                    _run_copy_move(job, item, user)
                    _audit(job["op"], user, target=f"{_rel(item['src_p'])} -> {_rel(item['dst_p'])}")
                elif job["op"] == "trash":
                    _run_trash(job, item, user)
                    if not item.get("permanent"):
                        _audit("trash", user, target=_rel(item["src_p"]))
                elif job["op"] == "delete":
                    _remove_tree(item["src_p"], job)
                    _audit("delete_permanent", user, target=_rel(item["src_p"]))
                elif job["op"] == "cloud_import":
                    _run_cloud_import(job, item, user)
                elif job["op"] == "empty_trash":
                    p = TRASH_DIR / item["trashed_name"]
                    if p.exists() or p.is_symlink():
                        _remove_tree(p, job)
                    with _trash_lock:
                        _trash_save([t for t in _trash_load() if t["id"] != item["trash_id"]])
                job["done_items"] += 1
            except _OpCancelled:
                break
            except _OpReject as r:
                job["errors"].append({"name": item["name"], "error": r.message, "code": r.code})
            except Exception as e:
                status, code, msg, hint = errors.friendly(e)
                ref = _log_exception(e, source="job", context=f"{job['op']} {item['name']}") if status >= 500 else None
                job["errors"].append({"name": item["name"], "error": f"{msg} {hint}".strip(), "code": code, "ref": ref})
            _job_push(job)
        if job["op"] == "empty_trash" and job["done_items"]:
            _audit("empty_trash", user, detail=f"{job['done_items']} item(s)")
        hard_errors = [e for e in job["errors"] if e.get("code") not in ("links_skipped", "renamed")]
        if job.get("cancel"):
            job["state"] = "cancelled"
            job["message"] = "Cancelled — nothing partial was left behind."
        elif hard_errors and job["done_items"] == 0:
            job["state"] = "error"
        elif hard_errors:
            job["state"] = "partial"
        else:
            job["state"] = "done"
        job["finished"] = time.time(); job["current"] = None
        _job_push(job, True)
        # The bell gets an entry for slow jobs (the user may have navigated
        # away) and for every failure; quick successes only get a toast.
        took = job["finished"] - (job.get("started") or job["finished"])
        if job["state"] in ("error", "partial"):
            first = hard_errors[0]
            _inapp_notify(user["id"], f"{job['label']} — " + ("failed" if job["state"] == "error"
                          else f"{len(hard_errors)} of {job['total_items']} item(s) failed"),
                          f"{first['name']}: {first['error']}", level="error", kind="files", link="files",
                          ref=first.get("ref"), data={"job": job_id})
        elif job["state"] == "done" and took > 8:
            _inapp_notify(user["id"], f"{job['label']} — finished", f"Took {int(took)}s.",
                          level="success", kind="files", link="files", data={"job": job_id, "dest": job.get("dest_dir")})

def _file_ops_prune():
    now = time.time()
    with _file_ops_lock:
        for jid in [j for j, v in _FILE_OPS.items() if v.get("finished") and now - v["finished"] > _FILE_OP_KEEP_S]:
            _FILE_OPS.pop(jid, None)

def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")

def _file_job_limit_error(user: dict):
    """None, or a 429 response when this account already has VOID_MAX_FILE_JOBS_PER_USER
    transfers queued or running. Every job is a thread, and a queue with no limit
    would let one account pile up thousands of them."""
    with _file_ops_lock:
        active = sum(1 for j in _FILE_OPS.values()
                     if j.get("user_id") == user["id"] and j.get("state") in ("queued", "running"))
    if active >= MAX_FILE_JOBS_PER_USER:
        return _api_error(f"You already have {active} file operations waiting or running.", 429, "too_many_jobs",
                          "Wait for some to finish (or cancel them) before starting more.")
    return None

@app.route("/api/file/ops", methods=["POST"])
@require_auth
def api_file_ops_start():
    """Body: {op: copy|move|trash|delete|empty_trash, items: [{src, dst}] (copy/move),
    paths: [...] (trash/delete), overwrite: bool}. Validates every item up
    front; returns {job_id, rejected: [{path, error}]}, or 4xx if nothing
    was valid (with the first item's specific reason as the message)."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    op = d.get("op")
    if op not in ("copy", "move", "trash", "delete", "empty_trash"):
        return _api_error("Unknown file operation.", 400, "bad_op")
    overwrite = bool(d.get("overwrite"))
    busy = _file_job_limit_error(user)
    if busy:
        return busy
    items, rejected = [], []
    incoming_personal = 0
    incoming_shared = 0

    if op == "empty_trash":
        with _trash_lock:
            visible = _trash_visible(_trash_load(), user)
        if not visible:
            return _api_error("The Recycle Bin is already empty.", 400, "empty")
        for t in visible:
            items.append({"name": t.get("name", "?"), "trash_id": t["id"], "trashed_name": t["trashed_name"],
                          "bytes": int(t.get("size") or 0), "files": 1})
    else:
        raw_list = d.get("items") if op in ("copy", "move") else [{"src": p} for p in (d.get("paths") or [])]
        if not raw_list or not isinstance(raw_list, list):
            return _api_error("Nothing was selected.", 400, "empty")
        if len(raw_list) > 1000:
            return _api_error("That's too many items for one operation (max 1000) — do it in smaller batches.", 400, "too_many")
        seen_dst = set()
        for it in raw_list:
            src_raw = str((it or {}).get("src", ""))
            name = src_raw.rstrip("/").split("/")[-1] or src_raw
            try:
                try:
                    src = _safe(src_raw, user)
                except PermissionError as e:
                    raise _OpReject(str(e) or "You don't have access to that item.", "forbidden")
                if not src.exists() and not src.is_symlink():
                    raise _OpReject("It no longer exists — it may have been moved or deleted.", "not_found")
                if _is_internal_name(src.name) or (src.parent == FILE_ROOT and src.name == TRASH_DIRNAME):
                    raise _OpReject("That item can't be changed.", "forbidden")
                prot = _protected_reason(src)
                if prot and op != "copy":
                    raise _OpReject(f"That's {prot} — it can't be {'moved' if op == 'move' else 'deleted'}.", "protected")
                rec = {"name": src.name, "src_p": src, "is_dir": src.is_dir()}
                if op in ("copy", "move"):
                    dst_raw = str(it.get("dst", ""))
                    dname = dst_raw.rstrip("/").split("/")[-1]
                    nerr = _name_error(dname)
                    if nerr:
                        raise _OpReject(nerr, "invalid_name")
                    try:
                        dst = _safe(dst_raw, user)
                    except PermissionError as e:
                        raise _OpReject(str(e) or "You can't write to that folder.", "forbidden")
                    if Path(os.path.normpath(dst)) == Path(os.path.normpath(src)):
                        raise _OpReject("It's already there.", "same")
                    if _is_within(dst, src):
                        raise _OpReject("A folder can't be copied or moved into itself.", "into_itself")
                    if not dst.parent.is_dir():
                        raise _OpReject("The destination folder no longer exists.", "not_found")
                    if (dst.exists() and not overwrite) or str(dst).lower() in seen_dst:
                        raise _OpReject(f"\u201c{dst.name}\u201d already exists there.", "exists")
                    if dst.exists() and _protected_reason(dst):
                        raise _OpReject("The item it would replace is protected.", "protected")
                    seen_dst.add(str(dst).lower())
                    rec.update(dst_p=dst, overwrite=overwrite)
                elif op == "delete" and _rel(src).split("/")[0] == TRASH_DIRNAME:
                    raise _OpReject("Use the Recycle Bin view to delete items in it.", "forbidden")
                rec["bytes"], rec["files"] = _tree_size(src)
                if op in ("copy", "move") and _in_personal_folder(user, rec["dst_p"]) and not _in_personal_folder(user, src):
                    incoming_personal += rec["bytes"]
                if op in ("copy", "move") and _shared_remaining_bytes(user, rec["dst_p"]) is not None \
                        and not _is_within(src, SHARED_DIR):
                    incoming_shared += rec["bytes"]
                items.append(rec)
            except _OpReject as r:
                rejected.append({"path": src_raw, "name": name, "error": r.message, "code": r.code})
        if op == "trash" and not d.get("confirm_permanent"):
            perm = [i for i in items if _needs_permanent_delete(i["src_p"])]
            if perm:
                names = [i["name"] for i in perm]
                return jsonify({"ok": False, "code": "permanent_required", "items": names[:50],
                                "count": len(names), "total": len(items),
                                "error": (f"\u201c{names[0]}\u201d is" if len(names) == 1 else f"{len(names)} of these items are")
                                         + " on an external drive, so it can't go to the Recycle Bin — it will be deleted permanently."}), 409
        remaining = _quota_remaining_bytes(user) if incoming_personal else None
        if remaining is not None and incoming_personal > remaining:
            return _api_error(_quota_error(GUEST_QUOTA_MB), 507, "quota_exceeded",
                              f"This needs {_fmt_bytes(incoming_personal)}; {_fmt_bytes(remaining)} is left.")
        if incoming_shared:
            left = _shared_remaining_bytes(user, next(i["dst_p"] for i in items if "dst_p" in i))
            if left is not None and incoming_shared > left:
                return _api_error(_shared_quota_error(), 507, "quota_exceeded")
        if not items:
            first = rejected[0] if rejected else {"error": "Nothing to do.", "code": "empty"}
            msg = first["error"] if len(rejected) <= 1 else f"None of the {len(rejected)} items could be processed: {first['error']}"
            return jsonify({"error": msg, "code": first.get("code"), "rejected": rejected}), 409 if first.get("code") == "exists" else 400
        if op in ("copy", "move"):
            free_check = items[0]["dst_p"].parent
            need = sum(i["bytes"] for i in items if not (op == "move" and _same_device(i["src_p"], i["dst_p"].parent)))
            try:
                free = psutil.disk_usage(str(free_check)).free
                reserve = MIN_FREE_MB * 1024 * 1024
                if need + reserve > free:
                    return _api_error("There isn't enough free space on the destination drive.", 507, "disk_full",
                                      f"This needs {_fmt_bytes(need)}; only {_fmt_bytes(free)} is free there"
                                      + (f", and Void OS keeps {_fmt_bytes(reserve)} free." if reserve else "."))
            except OSError:
                pass

    job = _launch_file_job(user, op, items)
    return jsonify({"job_id": job["id"], "job": _job_public(job), "rejected": rejected})

def _launch_file_job(user: dict, op: str, items: list) -> dict:
    total_bytes = sum(i["bytes"] for i in items)
    n = len(items)
    noun = items[0]["name"] if n == 1 else _plural(n, "item")
    label = {"copy": f"Copying {noun}", "move": f"Moving {noun}", "trash": f"Moving {noun} to the Recycle Bin",
             "delete": f"Deleting {noun}", "empty_trash": "Emptying the Recycle Bin",
             "cloud_import": f"Importing {noun}"}[op]
    job_id = uuid.uuid4().hex
    dest_dir = _rel(items[0]["dst_p"].parent) if op in ("copy", "move", "cloud_import") else None
    src_dirs = sorted({_rel(i["src_p"].parent) for i in items if "src_p" in i})
    job = {"id": job_id, "user_id": user["id"], "op": op, "state": "queued", "label": label,
           "created": time.time(), "started": None, "finished": None, "total_bytes": total_bytes, "done_bytes": 0,
           "total_items": n, "done_items": 0, "total_files": sum(i["files"] for i in items), "done_files": 0,
           "current": None, "errors": [], "dest_dir": dest_dir, "src_dirs": src_dirs, "message": None,
           "speed": None, "eta": None, "_items": items}
    _file_ops_prune()
    with _file_ops_lock:
        _FILE_OPS[job_id] = job
    threading.Thread(target=_file_op_worker, args=(job_id, user), daemon=True).start()
    return job

@app.route("/api/file/ops")
@require_auth
def api_file_ops_list():
    """The caller's running jobs plus ones finished in the last 30 min, so
    a reloaded tab picks up the progress tray where it left off."""
    user = _current_user()
    _file_ops_prune()
    with _file_ops_lock:
        jobs = [_job_public(j) for j in _FILE_OPS.values() if j["user_id"] == user["id"]]
    return jsonify({"jobs": sorted(jobs, key=lambda j: j["created"])})

@app.route("/api/file/ops/<job_id>")
@require_auth
def api_file_ops_get(job_id):
    job = _FILE_OPS.get(job_id)
    if not job or job["user_id"] != _current_user()["id"]:
        return _api_error("That transfer isn't running any more.", 404, "not_found")
    return jsonify(_job_public(job))

def _cancel_ops_for_user(user_id: str, why: str):
    for job in list(_FILE_OPS.values()):
        if job.get("user_id") != user_id or job.get("state") in ("done", "error", "partial", "cancelled"):
            continue
        job["cancel"] = True
        if job.get("state") == "queued":
            job.update(state="cancelled", finished=time.time(), message=why)
            _job_push(job, True)
        else:
            job["message"] = why

_register_job_canceller(_cancel_ops_for_user)


@app.route("/api/file/ops/<job_id>/cancel", methods=["POST"])
@require_auth
def api_file_ops_cancel(job_id):
    job = _FILE_OPS.get(job_id)
    if not job or job["user_id"] != _current_user()["id"]:
        return _api_error("That transfer isn't running any more.", 404, "not_found")
    if job["state"] in ("done", "error", "partial", "cancelled"):
        return _api_error("That transfer has already finished.", 409, "finished")
    job["cancel"] = True
    if job["state"] == "queued":
        job.update(state="cancelled", finished=time.time(), message="Cancelled before it started.")
        _job_push(job, True)
    return jsonify({"ok": True})


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _fmt_bytes, _webdav_trash
    from modules.disk_monitor import _fmt_bytes
    from modules.webdav import _webdav_trash
