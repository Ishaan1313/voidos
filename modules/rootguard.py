"""
modules/rootguard.py — knows whether the storage folder (VOID_FILE_ROOT) is really there.

The Files folder usually lives on an external drive (for example /media/STORAGE). If that drive drops off for a
moment, its mount point is left behind as an ordinary empty folder on the system disk, and anything that keeps
writing "into the root" quietly fills that folder instead — a cloud sync would download the whole account onto the
system disk, uploads would land there, and when the drive comes back everything is hidden underneath it.

So Void OS recognises its own storage: a small marker file (.void-root) is kept in the folder, and its random id is
remembered next to the app. The folder only counts as present while that marker is readable and matches. While it
isn't:
  * the file, trash, share, WebDAV, drive-mounting and cloud-sync routes answer "the storage drive isn't present"
    (HTTP 503) instead of touching the disk;
  * cloud syncs are paused (and nothing is created or deleted on either side);
  * an error notification goes to the admins and a banner shows in the page;
and when the marker is back, everything resumes by itself, with no restart.

Bookkeeping (accounts, settings, logs) lives in the app folder, not in the root, so Void OS itself keeps running.
"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, request

from modules import consolelog
from modules.config import FILE_ROOT, _void_path
from modules.web import _atomic_write_json, app

MARK = ".void-root"
_ID_FILE = _void_path("VOID_ROOT_ID_FILE", "file_root_id.json")
_REMOVABLE_PREFIXES = ("/media/", "/mnt/", "/run/media/")
_state = {"ok": True, "reason": "", "since": 0.0, "checked": 0.0}
_lock = threading.Lock()
_started = False


class RootMissing(PermissionError):
    """Raised instead of touching the disk while the storage folder is not present."""


def _record():
    try:
        d = json.loads(_ID_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("id") else None
    except (OSError, ValueError):
        return None


def _under_removable(p: Path) -> bool:
    return sys.platform != "win32" and str(p).startswith(_REMOVABLE_PREFIXES)


def _has_mount_ancestor(p: Path) -> bool:
    """True if this folder, or one of its parents inside /media, /mnt or /run/media, is a mount point."""
    for a in [p, *p.parents]:
        if str(a) in ("/", "/media", "/mnt", "/run/media", "/run"):
            return False
        try:
            if os.path.ismount(a):
                return True
        except OSError:
            pass
    return False


def compute() -> tuple:
    """(ok, reason) right now, without any caching."""
    root = FILE_ROOT
    rec = _record()
    if rec is not None and rec.get("path") != str(root):
        rec = None                               # the root was changed on purpose: treat it as a new one
    try:
        exists = root.is_dir()
    except OSError:
        exists = False
    if rec is None:
        if not exists:
            return False, f"The storage folder {root} doesn't exist."
        if _under_removable(root) and not _has_mount_ancestor(root):
            return False, (f"{root} looks like an empty mount point: the drive that should be mounted there "
                           "isn't connected or isn't mounted.")
        try:                                      # first time we see a usable root: remember it
            rid = uuid.uuid4().hex
            (root / MARK).write_text(rid, encoding="utf-8")
            _atomic_write_json(_ID_FILE, {"id": rid, "path": str(root)})
        except OSError:
            pass                                  # can't write a marker here: don't block anything over it
        return True, ""
    if not exists:
        return False, f"The storage folder {root} isn't there. Its drive is probably disconnected."
    try:
        mark = (root / MARK).read_text(encoding="utf-8").strip()
    except OSError:
        return False, (f"{root} is there but isn't the drive Void OS stores its files on (it may be an empty mount "
                       "point). Reconnect or mount the drive.")
    if mark != rec["id"]:
        return False, f"A different drive is mounted at {root} than the one Void OS stores its files on."
    return True, ""


def status(max_age: float = 2.0) -> dict:
    with _lock:
        now = time.time()
        if now - _state["checked"] > max_age:
            ok, reason = compute()
            _apply(ok, reason, notify=False)
            _state["checked"] = now
        return {"ok": _state["ok"], "reason": _state["reason"], "path": str(FILE_ROOT), "since": _state["since"]}


def ok() -> bool:
    return status()["ok"]


def require():
    """For code that is about to write under the root: raises RootMissing while it is not present."""
    s = status()
    if not s["ok"]:
        raise RootMissing(_message(s))


def _message(s: dict) -> str:
    return ("The storage drive isn't present, so files, uploads, drives and cloud sync are paused until it is back. "
            + (s.get("reason") or ""))


def _apply(new_ok: bool, reason: str, notify: bool) -> bool:
    """Record a new state; returns True when it changed."""
    changed = new_ok != _state["ok"]
    _state["ok"], _state["reason"] = new_ok, reason
    if changed:
        _state["since"] = time.time()
    return changed


def _on_change(now_ok: bool, reason: str):
    """Everything that follows from the drive going away or coming back."""
    try:
        from modules.activity import _audit, _inapp_notify_admins
    except Exception:
        _audit = _inapp_notify_admins = None
    if not now_ok:
        consolelog.warn(f"The storage folder {FILE_ROOT} isn't available. File access, uploads, drive mounting and "
                        f"cloud sync are paused until it is back. {reason}", "files")
        if _audit:
            _audit("storage_root_missing", None, target=str(FILE_ROOT), detail=reason[:200])
        if _inapp_notify_admins:
            _inapp_notify_admins("The storage drive isn't present", _message({"reason": reason}), level="error",
                                 kind="system", link="settings")
        try:
            from modules import cloud_routes
            cloud_routes._pause_sync_pairs()
        except Exception as e:
            consolelog.warn(f"Couldn't pause cloud sync: {e}", "files")
    else:
        consolelog.say(f"The storage folder {FILE_ROOT} is back. Resuming.", "files")
        if _audit:
            _audit("storage_root_restored", None, target=str(FILE_ROOT), detail="")
        if _inapp_notify_admins:
            _inapp_notify_admins("The storage drive is back", f"{FILE_ROOT} is available again. Files and cloud sync "
                                 "have resumed.", level="success", kind="system")
        try:
            from modules import security
            security._init_filesystem()           # recreate Shared / Users / Drives if the drive was replaced
        except Exception:
            pass
        try:
            from modules import cloud_routes
            cloud_routes._resume_sync_pairs()
        except Exception as e:
            consolelog.warn(f"Couldn't resume cloud sync: {e}", "files")
    try:
        from modules.web import _broadcast_coalesced
        _broadcast_coalesced("storage", {"type": "storage_changed"})
    except Exception:
        pass


def _watch():
    while True:
        try:
            ok_now, reason = compute()
            with _lock:
                changed = _apply(ok_now, reason, notify=True)
                _state["checked"] = time.time()
            if changed:
                _on_change(ok_now, reason)
        except Exception:
            pass
        time.sleep(4)


def start():
    global _started
    if _started:
        return
    _started = True
    ok_now, reason = compute()
    with _lock:
        _apply(ok_now, reason, notify=False)
        _state["since"] = time.time()
        _state["checked"] = time.time()
    if not ok_now:
        consolelog.warn(f"The storage folder {FILE_ROOT} isn't available: {reason} File access, uploads, drive "
                        "mounting and cloud sync stay paused until it is.", "files")
    threading.Thread(target=_watch, daemon=True, name="root-guard").start()


# ── the request gate ──────────────────────────────────────────────────────
_ALL_METHODS = ("/api/file", "/api/files", "/api/trash", "/s/", "/webdav")
_WRITES_ONLY = ("/api/storage", "/api/shares", "/api/cloud/", "/api/appstore/install", "/api/app-store/install")


@app.before_request
def _gate():
    if _state["ok"] and time.time() - _state["checked"] < 6:
        return None
    s = status(max_age=1.0)
    if s["ok"]:
        return None
    p = request.path
    block = any(p == x or p.startswith(x if x.endswith("/") else x + "/") or p == x.rstrip("/") for x in _ALL_METHODS) \
        or (request.method not in ("GET", "HEAD", "OPTIONS") and any(p.startswith(x) for x in _WRITES_ONLY))
    if not block:
        return None
    resp = jsonify({"error": _message(s), "code": "storage_missing", "path": s["path"]})
    resp.status_code = 503
    resp.headers["Retry-After"] = "10"
    return resp


@app.route("/api/storage/root")
def api_storage_root():
    """Is the storage folder present? (Anyone signed in may ask; the page uses it for its banner.)"""
    from modules.error_log import _current_user
    if not _current_user():
        return jsonify({"error": "unauthorized"}), 401
    s = status(max_age=1.0)
    return jsonify({"ok": s["ok"], "reason": s["reason"], "path": s["path"]})


start()
