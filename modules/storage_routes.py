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
from modules.config import DRIVES_REL, EXTERNAL_DIR, EXTERNAL_STORAGE_FILE, FILE_ROOT
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
            return d
        except (OSError, ValueError):
            return {"network": [], "usb_auto_mount": False, "usb_auto_repair": True, "known_usb": {}}

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
@app.route("/api/storage/usb")
@require_admin
def api_storage_usb_list():
    devices = external_storage.list_usb_devices()
    ext = _ext_load()
    for d in devices:
        d["bound_at"] = None
        if d.get("mountpoint"):
            target = EXTERNAL_DIR / _ext_slugify(d.get("label") or d["device"])
            if external_storage.is_mounted(target):
                d["bound_at"] = str(target.relative_to(FILE_ROOT))
    return jsonify({"devices": devices, "auto_mount": ext.get("usb_auto_mount", False),
                    "auto_repair": ext.get("usb_auto_repair", True),
                    "repairing": sorted(_repairing)})

@app.route("/api/storage/usb/mount", methods=["POST"])
@require_admin
def api_storage_usb_mount():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    device = str(d.get("device", ""))
    if not external_storage.valid_device(device):
        return jsonify({"error": "Invalid device"}), 400
    ok, msg = external_storage.mount_usb(device)
    if not ok:
        info0 = {x["device"]: x for x in external_storage.list_usb_devices()}.get(device)
        if info0 and _ext_load().get("usb_auto_repair", True) and external_storage.looks_corrupt(msg):
            # The drive wasn't closed cleanly (a power cut). Repair it in the background and mount it after,
            # so this request doesn't sit for minutes on a big drive.
            if _start_repair(device, info0, user):
                return jsonify({"ok": True, "repairing": True,
                                "message": "This drive wasn't removed cleanly. Void OS is repairing it now and "
                                           "will mount it when it's done; you'll get a notification."}), 202
        return jsonify({"error": msg}), 400
    bind_ok = None
    devices = {x["device"]: x for x in external_storage.list_usb_devices()}
    info = devices.get(device, {})
    if not info and IS_WIN and msg and msg[1:3] == ":\\":
        # Windows: a letter-less partition ("disk2part1") just got a drive
        # letter, so it's now listed under that letter instead.
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
    if info.get("mountpoint"):
        target = EXTERNAL_DIR / _ext_slugify(info.get("label") or device)
        if external_storage.is_mounted(target):
            external_storage.unbind_mount(str(target))
            try:
                target.rmdir()
            except OSError:
                pass
    ok, msg = external_storage.unmount_usb(device)
    if not ok:
        return jsonify({"error": msg}), 400
    _audit("usb_unmounted", user, target=device)
    return jsonify({"ok": True})

@app.route("/api/storage/usb/auto-mount", methods=["PATCH"])
@require_admin
def api_storage_usb_automount():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    with _ext_lock:
        ext = _ext_load()
        if "enabled" in d:
            ext["usb_auto_mount"] = bool(d.get("enabled"))
        if "auto_repair" in d:
            ext["usb_auto_repair"] = bool(d.get("auto_repair"))
        _ext_save(ext)
    if "enabled" in d:
        _audit("usb_automount_changed", user, detail=str(ext["usb_auto_mount"]))
    if "auto_repair" in d:
        _audit("usb_autorepair_changed", user, detail=str(ext["usb_auto_repair"]))
    return jsonify({"ok": True, "auto_mount": ext["usb_auto_mount"], "auto_repair": ext["usb_auto_repair"]})

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
            ok, real_mp = external_storage.mount_usb(device)
            if not ok:
                _audit("usb_repair_failed", actor, target=device,
                       detail=f"Repaired, but it still won't mount: {str(real_mp)[:200]}")
                return
            priv_ok, _ = external_storage.check_bind_privilege()
            if priv_ok:
                target = EXTERNAL_DIR / _ext_slugify(info.get("label") or device)
                external_storage.bind_mount(real_mp, str(target))
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
    while True:
        try:
            ext = _ext_load()
            present = {d["device"] for d in external_storage.list_usb_devices()}
            for gone in [k for k in tried if k not in present]:
                tried.pop(gone, None)
            if ext.get("usb_auto_mount"):
                for dev in external_storage.list_usb_devices():
                    if dev["device"] in seen or dev.get("mountpoint") or dev["device"] in _repairing:
                        continue
                    if time.time() - tried.get(dev["device"], 0) < 600:
                        continue       # failed recently: try again in ten minutes, or when it is re-plugged
                    ok, real_mp = external_storage.mount_usb(dev["device"])
                    if not ok:
                        tried[dev["device"]] = time.time()
                        if ext.get("usb_auto_repair", True) and external_storage.looks_corrupt(real_mp):
                            _start_repair(dev["device"], dev, None)
                        continue
                    if ok:
                        priv_ok, _ = external_storage.check_bind_privilege()
                        if priv_ok:
                            target = EXTERNAL_DIR / _ext_slugify(dev.get("label") or dev["device"])
                            external_storage.bind_mount(real_mp, str(target))
                        _audit("usb_auto_mounted", None, target=dev["device"], detail=real_mp)
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
