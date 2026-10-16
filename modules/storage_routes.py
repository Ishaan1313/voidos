"""
modules/storage_routes.py — External storage: USB drives and SMB/NFS network shares.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from datetime import datetime
from datetime import timezone
from flask import jsonify
from flask import request
from modules.winplat import IS_WIN
import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
import os
from modules.config import APP_DATA_DIR, DRIVES_REL, EXTERNAL_DIR, EXTERNAL_STORAGE_FILE, FILE_ROOT, VOID_ROOT
from modules.web import _atomic_write_json, app
from modules.activity import _audit
from modules.error_log import _current_user
from modules import external_storage, secret_store
from modules.decorators import require_admin


# ══════════════════════════════════════════════════════════════════════
# EXTERNAL STORAGE — SMB/NFS network shares + USB auto-mount
# (see modules/external_storage.py)
# ══════════════════════════════════════════════════════════════════════
_ext_lock = threading.RLock()

def _ext_load() -> dict:
    """external_storage.json holds saved network-share passwords, so it is stored
    encrypted (modules/secret_store.py) and owner-only. A plain file from an older
    version is read as-is and re-saved encrypted."""
    with _ext_lock:
        try:
            d = json.loads(EXTERNAL_STORAGE_FILE.read_text(encoding="utf-8"))
            if secret_store.is_sealed(d):
                try:
                    d = secret_store.unseal(d)
                except ValueError:
                    # Secret key changed: keep the old file aside, start empty (shares must be re-added)
                    try: EXTERNAL_STORAGE_FILE.replace(EXTERNAL_STORAGE_FILE.with_name(EXTERNAL_STORAGE_FILE.name + f".unreadable-{int(time.time())}"))
                    except OSError: pass
                    app.logger.warning("Saved network shares couldn't be decrypted with this secret key; re-add them in Access Control.")
                    raise ValueError("unreadable")
            elif secret_store.available() and isinstance(d, dict) and any(s.get("password") for s in d.get("network", [])):
                _ext_save(d)  # migrate an old plaintext file
            d.setdefault("network", [])
            d.setdefault("usb_auto_mount", False)
            d.setdefault("usb_auto_repair", True)   # fix a drive left dirty by a power cut before mounting it
            d.setdefault("known_usb", {})  # device UUID -> {name, last_seen} — friendly names across reconnects
            d.setdefault("hidden_drives", [])   # names under Drives left out of the Files sidebar, for everyone
            d.setdefault("mount_dir", "")   # where USB drives are mounted; empty = <files>/Drives
            d.setdefault("mounts", {})      # device -> the folder Void OS itself mounted it at
            return d
        except (OSError, ValueError):
            return {"network": [], "usb_auto_mount": False, "usb_auto_repair": True, "known_usb": {},
                    "mount_dir": "", "mounts": {}}

def _ext_save(d: dict) -> None:
    with _ext_lock:
        _atomic_write_json(EXTERNAL_STORAGE_FILE, secret_store.seal(d), private=True)
    from modules.web import _broadcast_coalesced
    _broadcast_coalesced("storage", {"type": "storage_changed"})  # drives and shares appear / vanish for everyone

def _ext_slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    return slug or "share"

@app.route("/api/storage/capabilities")
@require_admin
def api_storage_capabilities():
    usb_ok, usb_msg = external_storage.check_usb_capability()
    smb_ok, smb_msg = external_storage.check_smb_capability()
    nfs_ok, nfs_msg = external_storage.check_nfs_capability()
    priv_ok, priv_msg = external_storage.check_bind_privilege()
    return jsonify({
        "usb": {"ok": usb_ok, "message": usb_msg},
        "smb": {"ok": smb_ok, "message": smb_msg},
        "nfs": {"ok": nfs_ok, "message": nfs_msg},
        # Whether USB mounts can ALSO be bind-mounted into FILE_ROOT/external
        # (and thus browsable in Files) — separate from whether udisksctl
        # itself works, since the bind step needs the same privilege as SMB/NFS.
        "usb_bindable": priv_ok,
        "platform": "windows" if IS_WIN else "linux",
    })

# ── USB ──────────────────────────────────────────────────────────────────
@app.route("/api/storage/drives/visibility", methods=["PATCH"])
@require_admin
def api_storage_drives_visibility():
    """Which drives the Files sidebar lists, for everyone. Hiding only tidies the list: the drive stays mounted and
    its folder is still reachable (Server files, a typed path, WebDAV)."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    hidden = d.get("hidden")
    if not isinstance(hidden, list):
        return jsonify({"error": "hidden must be a list of drive names"}), 400
    clean = list(dict.fromkeys(str(x)[:120] for x in hidden if isinstance(x, str) and x and "/" not in x and "\\" not in x))[:200]
    with _ext_lock:
        ext = _ext_load()
        old = set(ext.get("hidden_drives", []))
        ext["hidden_drives"] = clean
        _ext_save(ext)                       # also tells every open page to refresh its sidebar
    changed = sorted(old.symmetric_difference(clean))
    if changed:
        _audit("drives_visibility_changed", user, detail=", ".join(changed)[:200])
    return jsonify({"ok": True, "hidden": clean})

# ── USB: where drives go, and how they get there ─────────────────────────
_usb_errors: dict = {}          # device -> {"msg", "at"}: why the last (auto-)mount of it failed

def _mount_base() -> Path:
    """The folder drives are mounted under: the one chosen in settings, else <files>/Drives."""
    custom = (_ext_load().get("mount_dir") or "").strip()
    return Path(custom) if custom else EXTERNAL_DIR

def _protected_mounts() -> tuple:
    """Folders a drive may never be mounted inside (Void OS's own files)."""
    return (str(VOID_ROOT),)

def _holds_files(mountpoint) -> bool:
    """True when this mount point contains the Files folder, Void OS itself or app data: the drive Void OS runs from."""
    if not mountpoint:
        return False
    mp = os.path.normpath(str(mountpoint))
    if mp == "/":
        return True
    for q in (FILE_ROOT, VOID_ROOT, APP_DATA_DIR):
        q = os.path.normpath(str(q))
        if q == mp or q.startswith(mp + os.sep):
            return True
    return False

def _unique_slug(label: str, device: str) -> str:
    slug = _ext_slugify(label or device)
    taken = {s["slug"] for s in _ext_load()["network"]}
    t = _mount_base() / slug
    if slug in taken or external_storage.is_mounted(t) or (t.exists() and any(t.iterdir())):
        slug = f"{slug}-{device.rsplit('/', 1)[-1].lower()}"
    return slug

def _browsable_at(slug: str, mountpoint) -> str | None:
    """Where the drive can be browsed from Files (relative to the Files folder), or None."""
    try:
        Path(str(mountpoint)).resolve().relative_to(FILE_ROOT)
        return str(Path(str(mountpoint)).resolve().relative_to(FILE_ROOT))
    except (ValueError, OSError):
        pass
    t = EXTERNAL_DIR / slug
    if external_storage.is_mounted(t):
        return str(t.relative_to(FILE_ROOT))
    return None

def _attach_usb(device: str, info: dict) -> tuple:
    """Mount a USB drive into the mount folder and make it browsable in Files. -> (ok, message, mountpoint, browsable)

    Preferred: `sudo mount` straight into <mount folder>/<name> (needs only the mount/umount sudo rule).
    Fallback: udisksctl (it picks its own folder), then a bind mount into Drives/<name>.
    A drive some other program already mounted is just linked into Drives."""
    if IS_WIN:
        ok, msg = external_storage.mount_usb(device)
        return ok, msg, None, False
    base = _mount_base()
    label = info.get("label") or device
    slug = _unique_slug(label, device)
    real = info.get("mountpoint")
    priv_ok, _ = external_storage.check_mount_privilege()
    error = ""
    if not real and priv_ok:
        target = base / slug
        ok, out = external_storage.mount_at(device, info.get("fstype"), str(target))
        if ok:
            real = str(target)
            with _ext_lock:
                ext = _ext_load()
                ext["mounts"][device] = real
                _ext_save(ext)
        else:
            error = out
            if external_storage.looks_corrupt(out):
                return False, out, None, False          # the caller decides about a repair
    if not real:
        ok, out = external_storage.mount_usb(device)   # udisksctl fallback
        if not ok:
            return False, (error or out), None, False
        real = None
        for x in external_storage.list_usb_devices():
            if x["device"] == device:
                real = x.get("mountpoint")
        if not real:
            real = out if out.startswith("/") else None
    _usb_errors.pop(device, None)
    browsable = _browsable_at(slug, real)
    if not browsable and real:
        priv2, _ = external_storage.check_bind_privilege()
        if priv2:
            target = EXTERNAL_DIR / slug
            b_ok, b_msg = external_storage.bind_mount(real, str(target))
            if b_ok:
                browsable = str(target.relative_to(FILE_ROOT))
            else:
                _audit("usb_bind_failed", None, target=device, detail=b_msg[:200])
    return True, "mounted", real, bool(browsable)

@app.route("/api/storage/usb")
@require_admin
def api_storage_usb_list():
    devices = external_storage.list_usb_devices()
    ext = _ext_load()
    for d in devices:
        d["bound_at"] = None
        d["holds_files"] = _holds_files(d.get("mountpoint"))      # the drive Void OS itself runs from
        if d.get("mountpoint") and not d["holds_files"]:
            d["bound_at"] = _browsable_at(_ext_slugify(d.get("label") or d["device"]), d["mountpoint"])
        err = _usb_errors.get(d["device"])
        d["error"] = err["msg"] if err and not d.get("mountpoint") else None
    return jsonify({"devices": devices, "auto_mount": ext.get("usb_auto_mount", False),
                    "auto_repair": ext.get("usb_auto_repair", True),
                    "mount_dir": ext.get("mount_dir", ""), "mount_base": str(_mount_base()),
                    "default_mount_dir": str(EXTERNAL_DIR),
                    "can_mount": external_storage.check_mount_privilege()[0] if not IS_WIN else None,
                    "repairing": sorted(_repairing)})

@app.route("/api/storage/usb/mount", methods=["POST"])
@require_admin
def api_storage_usb_mount():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    device = str(d.get("device", ""))
    if not external_storage.valid_device(device):
        return jsonify({"error": "Invalid device"}), 400
    info0 = {x["device"]: x for x in external_storage.list_usb_devices()}.get(device) or {}
    if _holds_files(info0.get("mountpoint")):
        return jsonify({"error": "This drive holds the Void OS Files folder; it is already in use."}), 400
    ok, msg, real_mp0, bound0 = _attach_usb(device, info0)
    if not ok:
        _usb_errors[device] = {"msg": msg[:300], "at": time.time()}
        if info0 and _ext_load().get("usb_auto_repair", True) and external_storage.looks_corrupt(msg):
            # The drive wasn't closed cleanly (a power cut). Repair it in the background and mount it after,
            # so this request doesn't sit for minutes on a big drive.
            if _start_repair(device, info0, user):
                return jsonify({"ok": True, "repairing": True,
                                "message": "This drive wasn't removed cleanly. Void OS is repairing it now and "
                                           "will mount it when it's done; you'll get a notification."}), 202
        return jsonify({"error": msg}), 400
    if IS_WIN:                                    # Windows: the drive just got (or had) a letter; link it as before
        bind_ok = None
        devices = {x["device"]: x for x in external_storage.list_usb_devices()}
        info = devices.get(device, {})
        if not info and msg and msg[1:3] == ":\\":
            info = devices.get(msg[:2].upper(), {})
        real_mp = info.get("mountpoint")
        if real_mp:
            priv_ok, _ = external_storage.check_bind_privilege()
            if priv_ok:
                target = EXTERNAL_DIR / _ext_slugify(info.get("label") or device)
                bind_ok, bind_msg = external_storage.bind_mount(real_mp, str(target))
                if not bind_ok:
                    _audit("usb_bind_failed", user, target=device, detail=bind_msg[:200])
        _audit("usb_mounted", user, target=device, detail=msg)
        return jsonify({"ok": True, "mountpoint": real_mp, "browsable": bool(bind_ok)})
    _audit("usb_mounted", user, target=device, detail=str(real_mp0))
    return jsonify({"ok": True, "mountpoint": real_mp0, "browsable": bool(bound0)})

@app.route("/api/storage/usb/unmount", methods=["POST"])
@require_admin
def api_storage_usb_unmount():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    device = str(d.get("device", ""))
    if not external_storage.valid_device(device):
        return jsonify({"error": "Invalid device"}), 400
    devices = {x["device"]: x for x in external_storage.list_usb_devices()}
    info = devices.get(device, {})
    mp = info.get("mountpoint")
    if _holds_files(mp):
        return jsonify({"error": "This drive holds the Void OS Files folder, so it can't be unmounted from here."}), 400
    if mp:
        target = EXTERNAL_DIR / _ext_slugify(info.get("label") or device)
        if str(target) != str(mp) and external_storage.is_mounted(target):     # the copy of it shown in Files
            external_storage.unbind_mount(str(target))
            try:
                target.rmdir()
            except OSError:
                pass
    ext = _ext_load()
    ours = ext.get("mounts", {}).get(device)
    if not IS_WIN and mp and (ours == mp or os.path.normpath(mp).startswith(str(_mount_base()) + os.sep)):
        ok, msg = external_storage.umount_at(mp)               # we mounted it ourselves: undo it the same way
        if not ok:
            ok, msg = external_storage.unmount_usb(device)
    else:
        ok, msg = external_storage.unmount_usb(device)
    if not ok:
        return jsonify({"error": msg}), 400
    with _ext_lock:
        ext = _ext_load()
        if ext.get("mounts", {}).pop(device, None) is not None:
            _ext_save(ext)
    _audit("usb_unmounted", user, target=device)
    return jsonify({"ok": True})

@app.route("/api/storage/usb/auto-mount", methods=["PATCH"])
@require_admin
def api_storage_usb_automount():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    new_dir = None
    if "mount_dir" in d:
        if IS_WIN:
            return jsonify({"error": "Windows gives drives their own letters, so there is no mount folder to choose."}), 400
        ok_dir, why, norm = external_storage.validate_mount_dir(str(d.get("mount_dir") or ""), _protected_mounts())
        if not ok_dir:
            return jsonify({"error": why}), 400
        new_dir = norm
    with _ext_lock:
        ext = _ext_load()
        if "enabled" in d:
            ext["usb_auto_mount"] = bool(d.get("enabled"))
        if "auto_repair" in d:
            ext["usb_auto_repair"] = bool(d.get("auto_repair"))
        if new_dir is not None:
            ext["mount_dir"] = "" if new_dir == str(EXTERNAL_DIR) else new_dir
        _ext_save(ext)
    if new_dir is not None:
        _usb_errors.clear()
        _audit("usb_mount_dir_changed", user, detail=ext["mount_dir"] or "(default)")
    if "enabled" in d:
        _audit("usb_automount_changed", user, detail=str(ext["usb_auto_mount"]))
    if "auto_repair" in d:
        _audit("usb_autorepair_changed", user, detail=str(ext["usb_auto_repair"]))
    return jsonify({"ok": True, "auto_mount": ext["usb_auto_mount"], "auto_repair": ext["usb_auto_repair"],
                    "mount_dir": ext.get("mount_dir", ""), "mount_base": str(_mount_base())})

# Drives being repaired right now (device paths), and when a drive was last tried by the watcher, so a
# drive that can't be fixed isn't hammered every five seconds.
_repairing: set = set()
_repair_lock = threading.Lock()

def _start_repair(device: str, info: dict, actor) -> bool:
    """Repair `device` (a removable partition from list_usb_devices) in a background thread, then mount
    it. Returns False when one is already running for it."""
    with _repair_lock:
        if device in _repairing:
            return False
        _repairing.add(device)
    def job():
        try:
            fs = info.get("fstype")
            _audit("usb_repair_started", actor, target=device, detail=str(fs or "unknown file system"))
            fixed, summary = external_storage.repair_usb(device, fs)
            if not fixed:
                _audit("usb_repair_failed", actor, target=device, detail=summary[:300])
                return
            ok, why, real_mp, _b = _attach_usb(device, info)
            if not ok:
                _audit("usb_repair_failed", actor, target=device,
                       detail=f"Repaired, but it still won't mount: {str(why)[:200]}")
                return
            _audit("usb_repaired", actor, target=device, detail=f"{summary} Mounted at {real_mp}")
        except Exception as e:
            _audit("usb_repair_failed", actor, target=device, detail=f"Unexpected error: {e}"[:300])
        finally:
            with _repair_lock:
                _repairing.discard(device)
    threading.Thread(target=job, daemon=True, name=f"usb-repair-{device}").start()
    return True

def _usb_watcher_loop():
    """Polls for newly-inserted USB storage and auto-mounts it when the
    setting above is on — the same bounded-polling shape as this app's
    other background watchers (zip janitor, upload janitor) rather than a
    kernel-event/udev-listener integration, which would need a real pip
    dependency (pyudev) for what a 5-second poll already does reliably
    enough for a home-server USB drive, not a hot-swap-heavy workload."""
    seen = set()
    tried: dict = {}      # device -> time of the last failed attempt; cleared when the drive is unplugged
    attempts: dict = {}   # device -> how many times in a row it has failed
    from modules import rootguard
    while True:
        try:
            if not rootguard.ok():            # mounting would create Drives/<name> on the system disk instead
                time.sleep(5)
                continue
            ext = _ext_load()
            present = {d["device"] for d in external_storage.list_usb_devices()}
            for gone in [k for k in tried if k not in present]:
                tried.pop(gone, None)
                attempts.pop(gone, None)
                _usb_errors.pop(gone, None)
            if ext.get("usb_auto_mount"):
                for dev in external_storage.list_usb_devices():
                    device = dev["device"]
                    if device in seen or device in _repairing or _holds_files(dev.get("mountpoint")):
                        continue            # (never the drive Void OS itself runs from)
                    if dev.get("mountpoint") and _browsable_at(_ext_slugify(dev.get("label") or device), dev["mountpoint"]):
                        continue            # already mounted and visible in Files
                    wait = (30, 120, 600)[min(attempts.get(device, 0), 2)]     # 30 s, 2 min, then every 10 min
                    if time.time() - tried.get(device, 0) < wait:
                        continue
                    ok, msg, real_mp, _b = _attach_usb(device, dev)
                    if not ok:
                        tried[device] = time.time()
                        attempts[device] = attempts.get(device, 0) + 1
                        _usb_errors[device] = {"msg": str(msg)[:300], "at": time.time()}
                        if attempts[device] == 1:        # say so once per insertion, in plain words
                            from modules.activity import _inapp_notify_admins
                            _audit("usb_auto_mount_failed", None, target=device, detail=str(msg)[:200])
                            _inapp_notify_admins("A USB drive couldn't be mounted",
                                                 f"{dev.get('label') or device}: {str(msg)[:240]}", level="error",
                                                 kind="system", link="access")
                        if ext.get("usb_auto_repair", True) and external_storage.looks_corrupt(msg):
                            _start_repair(device, dev, None)
                        continue
                    attempts.pop(device, None)
                    tried.pop(device, None)
                    _audit("usb_auto_mounted", None, target=device, detail=str(real_mp))
            seen = {d["device"] for d in external_storage.list_usb_devices() if d.get("mountpoint")}
        except Exception as e:
            app.logger.warning(f"USB watcher error: {e}")
        time.sleep(5)

# ── SMB / NFS ─────────────────────────────────────────────────────────────
@app.route("/api/storage/network")
@require_admin
def api_storage_network_list():
    ext = _ext_load()
    out = []
    for s in ext["network"]:
        safe = {k: v for k, v in s.items() if k != "password"}
        safe["has_password"] = bool(s.get("password"))
        target = EXTERNAL_DIR / s["slug"]
        safe["mounted"] = external_storage.is_mounted(target)
        out.append(safe)
    return jsonify(out)

@app.route("/api/storage/network", methods=["POST"])
@require_admin
def api_storage_network_create():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    kind = str(d.get("type", "")).lower()
    if kind not in ("smb", "nfs"):
        return jsonify({"error": "type must be 'smb' or 'nfs'"}), 400
    server = str(d.get("server", "")).strip()
    share = str(d.get("share", "")).strip().lstrip("/")
    name = str(d.get("name", "")).strip() or share or server
    if not server or not share:
        return jsonify({"error": "server and share/export path are required"}), 400
    if not external_storage.valid_host(server):
        return jsonify({"error": "That isn't a valid server name or address (letters, digits, dots and hyphens)."}), 400
    if any(c in str(d.get(k, "")) for k in ("username", "password", "domain") for c in "\r\n\x00"):
        return jsonify({"error": "The username, password and domain can't contain line breaks."}), 400
    cap_ok, cap_msg = (external_storage.check_smb_capability() if kind == "smb"
                       else external_storage.check_nfs_capability())
    if not cap_ok:
        return jsonify({"error": cap_msg}), 400
    slug = _ext_slugify(name)
    ext = _ext_load()
    if any(s["slug"] == slug for s in ext["network"]):
        slug = f"{slug}-{secrets.token_hex(3)}"
    target = EXTERNAL_DIR / slug
    if kind == "smb":
        ok, msg = external_storage.mount_smb(server, share, str(target),
            username=str(d.get("username", "")), password=str(d.get("password", "")),
            domain=str(d.get("domain", "")))
    else:
        ok, msg = external_storage.mount_nfs(server, "/" + share, str(target))
    if not ok:
        return jsonify({"error": msg}), 400
    entry = {
        "id": str(uuid.uuid4()), "slug": slug, "type": kind, "name": name,
        "server": server, "share": share,
        "username": str(d.get("username", "")), "password": str(d.get("password", "")),
        "domain": str(d.get("domain", "")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _ext_lock:
        ext = _ext_load()
        ext["network"].append(entry)
        _ext_save(ext)
    _audit("network_share_mounted", user, target=f"{server}/{share}", detail=kind)
    return jsonify({"ok": True, "id": entry["id"], "path": f"{DRIVES_REL}/{slug}"})

@app.route("/api/storage/network/<share_id>", methods=["DELETE"])
@require_admin
def api_storage_network_delete(share_id):
    user = _current_user()
    with _ext_lock:
        ext = _ext_load()
        entry = next((s for s in ext["network"] if s["id"] == share_id), None)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        ext["network"] = [s for s in ext["network"] if s["id"] != share_id]
        _ext_save(ext)
    target = EXTERNAL_DIR / entry["slug"]
    if external_storage.is_mounted(target):
        ok, msg = (external_storage.unmount_smb(str(target)) if entry["type"] == "smb"
                   else external_storage.unmount_nfs(str(target)))
        if not ok:
            # Put it back so state doesn't silently drift from what's actually still mounted
            with _ext_lock:
                ext = _ext_load()
                ext["network"].append(entry)
                _ext_save(ext)
            return jsonify({"error": f"Unmount failed, share kept: {msg}"}), 400
    try:
        target.rmdir()
    except OSError:
        pass
    _audit("network_share_unmounted", user, target=f"{entry['server']}/{entry['share']}", detail=entry["type"])
    return jsonify({"ok": True})

def _remount_network_shares_on_boot() -> None:
    """Network shares don't survive a restart (they're kernel mounts, not
    a supervised process) — remounts every configured one, same idea as
    restoring sync pairs/tunnels on boot elsewhere in this file."""
    ext = _ext_load()
    for entry in ext.get("network", []):
        target = EXTERNAL_DIR / entry["slug"]
        if external_storage.is_mounted(target):
            continue
        try:
            if entry["type"] == "smb":
                ok, msg = external_storage.mount_smb(entry["server"], entry["share"], str(target),
                    username=entry.get("username", ""), password=entry.get("password", ""),
                    domain=entry.get("domain", ""))
            else:
                ok, msg = external_storage.mount_nfs(entry["server"], "/" + entry["share"], str(target))
            if not ok:
                app.logger.warning(f"Failed to remount {entry['type']} share {entry['name']}: {msg}")
        except Exception as e:
            app.logger.warning(f"Failed to remount share {entry.get('name')}: {e}")
