"""
modules/cloud_routes.py — Dropbox / Google Drive: connect, browse, import, export, continuous sync.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from datetime import datetime
from datetime import timezone
from flask import jsonify
from flask import redirect
from flask import request
from itsdangerous import BadSignature
from itsdangerous import SignatureExpired
from urllib.parse import quote
import json
import os
import shutil
import threading
import time
import uuid
from modules import cloud_storage, cloud_sync, rootguard, secret_store, winplat
from modules.config import (
    CLOUD_SYNC_FILE, CLOUD_TOKENS_FILE, FILE_ROOT, GUEST_QUOTA_MB, MIN_FREE_MB, TMP_DIR, USERS_DIR, USERS_REL,
    VOID_ROOT,
)
from modules.error_log import _api_error, _current_user, _err, _is_admin_level
from modules.web import _atomic_write_json, _cloud_serial, app
from modules.activity import _audit, _inapp_notify
from modules.file_access import (
    _in_personal_folder, _name_error, _quota_error, _quota_remaining_bytes, _safe,
)
from modules.users import _users_load
from modules.decorators import require_auth


# ══════════════════════════════════════════════════════════════════════
# CLOUD STORAGE — Dropbox / Google Drive (see modules/cloud_storage.py)
# ══════════════════════════════════════════════════════════════════════
_cloud_lock = threading.RLock()

CLOUD_PROVIDER_ENV = {
    "dropbox": ("VOID_DROPBOX_CLIENT_ID", "VOID_DROPBOX_CLIENT_SECRET"),
    "gdrive":  ("VOID_GDRIVE_CLIENT_ID",  "VOID_GDRIVE_CLIENT_SECRET"),
}

def _cloud_provider_config(provider: str):
    """(client_id, client_secret) from .env, or None if this provider hasn't
    been configured with an app registration yet — see README's Cloud
    Storage section for how to get these from Dropbox's/Google's own
    developer consoles. Never hardcoded, never bundled — same reasoning as
    every other externally-issued credential in this app (Cloudflare's
    wrangler login, App Store's per-install secrets)."""
    env_ids = CLOUD_PROVIDER_ENV.get(provider)
    if not env_ids:
        return None
    cid = os.environ.get(env_ids[0], "").strip()
    csec = os.environ.get(env_ids[1], "").strip()
    return (cid, csec) if cid and csec else None

def _cloud_tokens_load() -> dict:
    """The saved login tokens, {user_id: {provider: record}}. Stored encrypted
    (modules/secret_store.py) and owner-only; a file written by an older
    version (plain JSON) is read as-is and re-saved encrypted right away."""
    with _cloud_lock:
        try:
            raw = json.loads(CLOUD_TOKENS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if secret_store.is_sealed(raw):
            try:
                data = secret_store.unseal(raw)
            except ValueError as e:
                # Wrong/changed VOID_SECRET_KEY: keep the file aside (don't let the
                # next save overwrite it) and treat everyone as disconnected.
                aside = CLOUD_TOKENS_FILE.with_name(CLOUD_TOKENS_FILE.name + f".unreadable-{int(time.time())}")
                try: CLOUD_TOKENS_FILE.replace(aside)
                except OSError: pass
                app.logger.warning(f"Cloud login tokens couldn't be decrypted ({e}); cloud accounts need reconnecting.")
                return {}
            return data if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return {}
        if raw and secret_store.available():
            _cloud_tokens_save(raw)  # migrate an old plaintext file
        return raw

def _cloud_tokens_save(d: dict) -> None:
    with _cloud_lock:
        _atomic_write_json(CLOUD_TOKENS_FILE, secret_store.seal(d), private=True)

def _cloud_get_token(user_id: str, provider: str) -> dict | None:
    return _cloud_tokens_load().get(user_id, {}).get(provider)

def _cloud_set_token(user_id: str, provider: str, record: dict | None) -> None:
    with _cloud_lock:
        data = _cloud_tokens_load()
        acct = data.setdefault(user_id, {})
        if record is None:
            acct.pop(provider, None)
        else:
            acct[provider] = record
        _cloud_tokens_save(data)

def _cloud_redirect_uri(provider: str) -> str:
    base = os.environ.get("VOID_CLOUD_REDIRECT_BASE", "").rstrip("/")
    if not base:
        base = request.url_root.rstrip("/")
    return f"{base}/api/cloud/{provider}/callback"

_CLOUD_OAUTH_SALT = "voidos-cloud-oauth-state"  # documentation only — real isolation is _cloud_serial's own salt above

@app.route("/api/cloud/providers")
@require_auth
def api_cloud_providers():
    user = _current_user()
    out = []
    for pid, meta in cloud_storage.PROVIDERS.items():
        cfg = _cloud_provider_config(pid)
        tok = _cloud_get_token(user["id"], pid)
        out.append({
            "id": pid, "label": meta["label"],
            "configured": cfg is not None,
            "connected": bool(tok),
        })
    return jsonify(out)

@app.route("/api/cloud/<provider>/connect")
@require_auth
def api_cloud_connect(provider):
    if provider not in cloud_storage.PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 404
    cfg = _cloud_provider_config(provider)
    if not cfg:
        return jsonify({"error": f"{cloud_storage.PROVIDERS[provider]['label']} isn't configured — "
                                  f"set {CLOUD_PROVIDER_ENV[provider][0]}/{CLOUD_PROVIDER_ENV[provider][1]} in .env "
                                  f"(see README's Cloud Storage section)"}), 400
    user = _current_user()
    state = _cloud_serial.dumps({"uid": user["id"], "provider": provider})
    url = cloud_storage.build_authorize_url(provider, cfg[0], _cloud_redirect_uri(provider), state)
    return jsonify({"url": url})

@app.route("/api/cloud/<provider>/callback")
def api_cloud_callback(provider):
    # Not @require_auth — the provider redirects the browser here directly
    # after consent, so identity comes from the signed `state` token minted
    # in api_cloud_connect above, the same pattern share-link/gate tokens
    # in this app already use rather than relying on the session cookie
    # surviving a third-party redirect.
    if provider not in cloud_storage.PROVIDERS:
        return "Unknown provider", 404
    state = request.args.get("state", "")
    try:
        payload = _cloud_serial.loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        return "This connection link expired or is invalid — go back to Settings and try again.", 400
    if payload.get("provider") != provider:
        return "State mismatch", 400
    signed_in = _current_user()
    if signed_in and signed_in["id"] != payload.get("uid"):
        # The consent was started by a different Void OS account than the one
        # this browser is signed in as -- never attach a cloud account to a
        # user who didn't start the flow from this browser.
        return "This connection was started from a different account — go back to Settings and try again.", 400
    err = request.args.get("error")
    if err:
        return redirect(f"/?cloud_error={quote(err)}#settings")
    code = request.args.get("code", "")
    cfg = _cloud_provider_config(provider)
    if not cfg or not code:
        return redirect(f"/?cloud_error=missing_code#settings")
    try:
        record = cloud_storage.exchange_code(provider, cfg[0], cfg[1], code, _cloud_redirect_uri(provider))
    except cloud_storage.CloudError as e:
        return redirect(f"/?cloud_error={quote(str(e))}#settings")
    _cloud_set_token(payload["uid"], provider, record)
    users = _users_load()
    u = next((x for x in users if x["id"] == payload["uid"]), None)
    if u:
        _audit("cloud_connected", u, detail=provider)
    return redirect("/?cloud_connected=" + provider + "#settings")

@app.route("/api/cloud/<provider>/disconnect", methods=["POST"])
@require_auth
def api_cloud_disconnect(provider):
    user = _current_user()
    # Disconnecting must also end continuous sync for that account.
    with _sync_lock:
        pairs = _sync_pairs_load()
        mine = [p for p in pairs if p.get("user_id") == user["id"] and p.get("provider") == provider]
        if mine:
            _sync_pairs_save([p for p in pairs if p not in mine])
    for p in mine:
        _sync_stop_supervisor(p["id"])
        _sync_status.pop(p["id"], None)
    _cloud_set_token(user["id"], provider, None)
    _audit("cloud_disconnected", user, detail=provider)
    return jsonify({"ok": True})

def _cloud_authed_token(user: dict, provider: str):
    """Returns a fresh access token, refreshing + persisting if needed.
    Raises cloud_storage.CloudError (caller turns that into a 400) if not
    connected or the refresh itself fails."""
    cfg = _cloud_provider_config(provider)
    if not cfg:
        raise cloud_storage.CloudError(f"{provider} isn't configured on this server")
    rec = _cloud_get_token(user["id"], provider)
    if not rec:
        raise cloud_storage.CloudError("Not connected")
    fresh = cloud_storage.ensure_fresh(provider, cfg[0], cfg[1], rec)
    if fresh is not rec:
        _cloud_set_token(user["id"], provider, fresh)
    return fresh["access_token"]

@app.route("/api/cloud/<provider>/list")
@require_auth
def api_cloud_list(provider):
    user = _current_user()
    path = request.args.get("path", "")
    try:
        tok = _cloud_authed_token(user, provider)
        entries = cloud_storage.list_folder(provider, tok, path)
    except cloud_storage.CloudError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": path, "entries": entries})

@app.route("/api/cloud/<provider>/import", methods=["POST"])
@require_auth
def api_cloud_import(provider):
    """Downloads one remote file into this user's own storage — Shared
    (guests only if they have write access there, same as any upload) or
    their own personal folder, under a Cloud Imports subfolder created on
    demand. Folders aren't recursed into in this first pass — see README."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    remote_id = str(d.get("remote_id", ""))
    name = str(d.get("name", "")).strip()
    dest_dir_rel = str(d.get("dest", "")).strip()  # relative path the user is currently browsing
    if not remote_id or not name:
        return jsonify({"error": "remote_id and name are required"}), 400
    try:
        dest_root = _safe(dest_dir_rel, user) if dest_dir_rel else None
    except Exception:
        dest_root = None
    if dest_root is None:
        pf = user.get("personal_folder", user.get("username", ""))
        dest_root = USERS_DIR / pf / "Cloud Imports"
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _err(e)
    safe_name = winplat.sanitize_win_name(os.path.basename(name.replace("\\", "/")))
    if _name_error(safe_name):
        return _api_error("That file's name can't be stored on this server.", 400, "invalid_name")
    try:
        _cloud_authed_token(user, provider)
    except cloud_storage.CloudError as e:
        return _api_error(str(e), 400, "cloud_error")
    size = d.get("size") if isinstance(d.get("size"), int) and d.get("size") >= 0 else 0
    remaining = _quota_remaining_bytes(user) if _in_personal_folder(user, dest_root) else None
    if remaining is not None and size > remaining:
        return _api_error(_quota_error(GUEST_QUOTA_MB), 507, "quota_exceeded")
    busy = _file_job_limit_error(user)
    if busy:
        return busy
    job = _launch_file_job(user, "cloud_import", [{
        "name": safe_name, "provider": provider, "remote_id": remote_id,
        "dst_p": dest_root / safe_name, "bytes": size, "files": 1}])
    return jsonify({"ok": True, "job_id": job["id"], "job": _job_public(job),
                    "path": _rel(dest_root / safe_name)})

@app.route("/api/cloud/<provider>/export", methods=["POST"])
@require_auth
def api_cloud_export(provider):
    """Pushes one of the user's own files up to the connected provider."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    rel = str(d.get("path", ""))
    remote_folder = str(d.get("remote_folder", ""))
    try:
        src = _safe(rel, user)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    if not src or not src.is_file():
        return jsonify({"error": "File not found"}), 404
    try:
        tok = _cloud_authed_token(user, provider)
        with open(src, "rb") as fp:
            cloud_storage.upload_from(provider, tok, remote_folder, src.name, fp, src.stat().st_size)
    except cloud_storage.CloudError as e:
        return jsonify({"error": str(e)}), 400
    _audit("cloud_export", user, target=rel, detail=provider)
    return jsonify({"ok": True})

# ══════════════════════════════════════════════════════════════════════
# CONTINUOUS CLOUD SYNC — rclone bisync (see modules/cloud_sync.py)
# ══════════════════════════════════════════════════════════════════════
_sync_lock = threading.RLock()
_sync_supervisors: dict[str, cloud_sync.SyncSupervisor] = {}  # pair_id -> live supervisor
_sync_status: dict[str, dict] = {}  # pair_id -> {ok, message, at} — last run result, in-memory only

def _sync_pairs_load() -> list:
    with _sync_lock:
        try:
            data = json.loads(CLOUD_SYNC_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

def _sync_pairs_save(pairs: list) -> None:
    with _sync_lock:
        _atomic_write_json(CLOUD_SYNC_FILE, pairs)

cloud_sync.ROOT_CHECK = lambda: (rootguard.ok(), "")


def _pause_sync_pairs() -> None:
    """The storage drive went away: stop every sync loop (the pairs stay saved and resume when it is back)."""
    for pid in list(_sync_supervisors):
        _sync_stop_supervisor(pid)
        st = _sync_status.setdefault(pid, {})
        st.update(ok=True, state="idle", message="Paused: the storage drive isn't present.", at=time.time())


def _resume_sync_pairs() -> None:
    for pair in _sync_pairs_load():
        if pair.get("enabled") and pair["id"] not in _sync_supervisors:
            try:
                _sync_start_supervisor(pair)
            except Exception as e:
                app.logger.warning(f"Failed to resume sync pair {pair.get('id')}: {e}")


def _sync_on_result(pair_id, ok, message, at):
    if str(message).startswith("Paused:"):        # not a failure: just waiting for the drive
        _sync_status[pair_id] = {"ok": True, "message": message, "at": at, "state": "idle"}
        return
    prev = _sync_status.get(pair_id)
    _sync_status[pair_id] = {"ok": ok, "message": message, "at": at, "state": (prev or {}).get("state", "idle")}
    was_ok = prev is None or prev.get("ok")
    if ok != was_ok or (not ok and prev is None):
        # Only on a change of state: a sync failing every 5 minutes for a
        # day is one "failing" notice and one "recovered", not 288 alerts.
        pairs = _sync_pairs_load()
        p = next((x for x in pairs if x["id"] == pair_id), None)
        if p:
            if ok:
                _inapp_notify(p["user_id"], "Cloud sync recovered", f"{p['provider'].title()} is syncing again.",
                              level="success", kind="sync", link="settings")
            else:
                u = next((x for x in _users_load() if x["id"] == p["user_id"]), None)
                if u:
                    _audit("cloud_sync_failed", u, detail=f"{p['provider']}: {message[:200]}")
                    _inapp_notify(u["id"], f"{p['provider'].title()} sync is failing", message[:600],
                                  level="error", kind="sync", link="settings")
    if not ok:
        pairs = _sync_pairs_load()
        p = next((x for x in pairs if x["id"] == pair_id), None)
        if p:
            users = _users_load()
            u = next((x for x in users if x["id"] == p["user_id"]), None)
            if u:
                _audit("cloud_sync_error", u, detail=f"{p['provider']}: {message[:200]}")

def _sync_on_resync_done(pair_id):
    with _sync_lock:
        pairs = _sync_pairs_load()
        for p in pairs:
            if p["id"] == pair_id:
                p["resynced"] = True
        _sync_pairs_save(pairs)

def _sync_remote_env(pair: dict):
    """(rclone environment variables, None) for this pair right now, or
    (None, a message saying why not). Called before every sync run, so each run
    starts from a fresh, decrypted token — nothing is kept in a config file."""
    cfg = _cloud_provider_config(pair["provider"])
    tok = _cloud_get_token(pair["user_id"], pair["provider"])
    if not cfg or not tok:
        return None, (f"{pair['provider'].title()} isn't connected any more — reconnect it in Settings, "
                      f"then set up the sync again.")
    try:
        fresh = cloud_storage.ensure_fresh(pair["provider"], cfg[0], cfg[1], tok)
        if fresh is not tok:
            _cloud_set_token(pair["user_id"], pair["provider"], fresh)
    except cloud_storage.CloudError as e:
        return None, str(e)
    return cloud_sync.remote_env(pair["remote_name"], pair["provider"], cfg[0], cfg[1], fresh), None

def _sync_access_token(pair: dict) -> str:
    """A current access token for this pair's account (used for Dropbox change notifications)."""
    cfg = _cloud_provider_config(pair["provider"])
    tok = _cloud_get_token(pair["user_id"], pair["provider"])
    if not cfg or not tok:
        raise RuntimeError("not connected")
    fresh = cloud_storage.ensure_fresh(pair["provider"], cfg[0], cfg[1], tok)
    if fresh is not tok:
        _cloud_set_token(pair["user_id"], pair["provider"], fresh)
    return fresh["access_token"]

def _sync_on_state(pair_id, state):
    st = _sync_status.setdefault(pair_id, {})
    st["state"] = state

def _sync_start_supervisor(pair: dict) -> None:
    if not rootguard.ok():                 # started again by rootguard when the drive returns
        return
    local_path = FILE_ROOT / pair["local_path"]
    sup = cloud_sync.SyncSupervisor(
        pair_id=pair["id"], get_remote=lambda: _sync_remote_env(pair), remote_name=pair["remote_name"],
        remote_path=pair.get("remote_path", ""), local_path=local_path,
        interval_s=pair.get("interval_s", 900), first_resync_done=pair.get("resynced", False),
        on_result=_sync_on_result, on_resync_done=_sync_on_resync_done,
        live=bool(pair.get("live")), provider=pair["provider"],
        get_access_token=lambda: _sync_access_token(pair), on_state=_sync_on_state)
    with _sync_lock:
        _sync_supervisors[pair["id"]] = sup
    sup.start()

def _sync_stop_supervisor(pair_id: str) -> None:
    with _sync_lock:
        sup = _sync_supervisors.pop(pair_id, None)
    if sup:
        sup.stop()

def _restore_sync_pairs_on_boot() -> None:
    """Called once at startup (see bottom of file) — restarts every
    previously-enabled sync pair's background loop, same idea as the app
    tunnel supervisors restarting their own cloudflared processes on boot."""
    for pair in _sync_pairs_load():
        if pair.get("enabled"):
            try:
                _sync_start_supervisor(pair)
            except Exception as e:
                app.logger.warning(f"Failed to restore sync pair {pair.get('id')}: {e}")
    # Older versions kept each user's login token in an rclone config file
    # (<app>/rclone_conf, briefly tmp/rclone_conf). Nothing needs those now.
    for legacy in (VOID_ROOT / "rclone_conf", TMP_DIR / "rclone_conf"):
        try:
            if legacy.is_dir():
                shutil.rmtree(legacy)
        except OSError as e:
            app.logger.warning(f"Couldn't remove the old {legacy.name} folder: {e}")

@app.route("/api/rclone/check")
@require_auth
def api_rclone_check():
    ok, msg = cloud_sync.check_rclone()
    if not _is_admin_level(_current_user()):
        # Install paths / version strings are for whoever runs the server.
        msg = ("Continuous sync is available" if ok else
               "Continuous sync isn't available on this server — ask an Admin to install rclone")
    return jsonify({"ok": ok, "message": msg})

_progress_cache: dict[str, tuple[float, dict]] = {}   # pair_id -> (computed_at, {"local","total"})

def _first_sync_progress(pair: dict, user_id: str) -> dict | None:
    """Rough progress of the first full download: bytes now in the local folder against the bytes
    the account holds (Dropbox reports that; other providers don't, so only the local size is given).
    Cached for a few seconds because the page asks every 3 s and the folder can be large."""
    now = time.time()
    hit = _progress_cache.get(pair["id"])
    if hit and now - hit[0] < 8:
        return hit[1]
    local = 0
    try:
        for root, _dirs, files in os.walk(FILE_ROOT / pair["local_path"]):
            for f in files:
                try:
                    local += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    total = None
    if pair.get("provider") == "dropbox":
        try:
            total = cloud_sync.dbx_space(_sync_access_token(pair))["used"] or None
        except Exception:
            total = hit[1].get("total") if hit else None
    out = {"local": local, "total": total}
    _progress_cache[pair["id"]] = (now, out)
    return out

@app.route("/api/cloud/<provider>/sync", methods=["GET"])
@require_auth
def api_cloud_sync_list(provider):
    user = _current_user()
    pairs = [p for p in _sync_pairs_load() if p["user_id"] == user["id"] and p["provider"] == provider]
    out = []
    for p in pairs:
        st = _sync_status.get(p["id"], {})
        sup = _sync_supervisors.get(p["id"])
        prog = _first_sync_progress(p, user["id"]) if (p.get("entire") and not p.get("resynced")
                                                       and p.get("enabled")) else None
        out.append({"id": p["id"], "progress": prog, "local_path": p["local_path"], "remote_path": p.get("remote_path", ""),
                     "interval_s": p.get("interval_s", 900), "enabled": p.get("enabled", False),
                     "resynced": p.get("resynced", False), "last_result": st,
                     "live": bool(p.get("live")), "entire": bool(p.get("entire")),
                     "state": st.get("state", "idle"), "watching": bool(sup and sup.watching_local)})
    return jsonify(out)

@app.route("/api/cloud/<provider>/sync", methods=["POST"])
@require_auth
def api_cloud_sync_create(provider):
    """Creates and immediately enables one continuous sync pair between a
    local Void OS folder and a folder on the connected provider."""
    if provider not in cloud_storage.PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 404
    ok, msg = cloud_sync.check_rclone()
    if not ok:
        return jsonify({"error": msg}), 400
    user = _current_user()
    cfg = _cloud_provider_config(provider)
    tok = _cloud_get_token(user["id"], provider)
    if not cfg or not tok:
        return jsonify({"error": "Connect this provider first"}), 400
    d = request.get_json(silent=True) or {}
    entire = bool(d.get("entire"))
    local_rel = str(d.get("local_path", "")).strip()
    remote_path = str(d.get("remote_path", "")).strip()
    if entire:
        # The whole account, kept as a normal folder in My Files — like the desktop app's Dropbox folder.
        remote_path = ""
        if not local_rel:
            local_rel = f"{USERS_REL}/{user['username']}/{'Dropbox' if provider == 'dropbox' else 'Google Drive'}"
        if any(p.get("provider") == provider and p["user_id"] == user["id"] and p.get("entire")
               for p in _sync_pairs_load()):
            return jsonify({"error": "Your whole account is already being synced."}), 400
    try:
        interval_min = max(1, int(d.get("interval_minutes", 5 if entire else 15)))
    except (TypeError, ValueError):
        return jsonify({"error": "interval_minutes must be a number"}), 400
    if not local_rel:
        return jsonify({"error": "local_path is required"}), 400
    try:
        local_abs = _safe(local_rel, user)
    except Exception:
        return jsonify({"error": "Invalid local path"}), 400
    local_abs.mkdir(parents=True, exist_ok=True)
    # Refresh first, so the first sync starts with a token that has real time
    # left on it rather than one about to expire mid-sync.
    try:
        tok = cloud_storage.ensure_fresh(provider, cfg[0], cfg[1], tok)
    except cloud_storage.CloudError as e:
        return jsonify({"error": str(e)}), 400
    _cloud_set_token(user["id"], provider, tok)
    if entire and provider == "dropbox":
        # A full local copy has to fit: say so now, not when the disk is full halfway through.
        try:
            sp = cloud_sync.dbx_space(tok["access_token"])
            free = shutil.disk_usage(str(local_abs)).free
            reserve = int(MIN_FREE_MB) * 1024 * 1024
            if sp["used"] and sp["used"] + reserve > free:
                gb = lambda n: f"{n / 1024 ** 3:.1f} GB"
                return jsonify({"error": f"Your Dropbox holds {gb(sp['used'])} but this server has only "
                                         f"{gb(free)} free for it. Free up space or choose a folder on a "
                                         f"bigger drive."}), 400
        except Exception:
            pass                                  # the check is a courtesy; never block sync on it
    pair_id = str(uuid.uuid4())
    remote_name = f"voidos_{provider}_{user['id'][:8]}"
    pair = {
        "id": pair_id, "user_id": user["id"], "provider": provider,
        "remote_name": remote_name, "remote_path": remote_path,
        "local_path": local_abs.relative_to(FILE_ROOT).as_posix(),
        "interval_s": interval_min * 60, "enabled": True, "resynced": False,
        "live": True, "entire": entire,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _sync_lock:
        pairs = _sync_pairs_load()
        pairs.append(pair)
        _sync_pairs_save(pairs)
    _sync_start_supervisor(pair)
    _audit("cloud_sync_enabled", user, target=pair["local_path"], detail=f"{provider} <-> {remote_path or '/'}")
    return jsonify({"ok": True, "id": pair_id})

@app.route("/api/cloud/sync/<pair_id>", methods=["DELETE"])
@require_auth
def api_cloud_sync_delete(pair_id):
    user = _current_user()
    with _sync_lock:
        pairs = _sync_pairs_load()
        pair = next((p for p in pairs if p["id"] == pair_id), None)
        if not pair or (pair["user_id"] != user["id"] and user.get("role") != "superuser"):
            return jsonify({"error": "Not found"}), 404
        pairs = [p for p in pairs if p["id"] != pair_id]
        _sync_pairs_save(pairs)
    _sync_stop_supervisor(pair_id)
    _sync_status.pop(pair_id, None)
    _audit("cloud_sync_disabled", user, target=pair["local_path"], detail=pair["provider"])
    return jsonify({"ok": True})


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _job_public, _launch_file_job, _rel, _file_job_limit_error
    from modules.file_ops import _file_job_limit_error, _job_public, _launch_file_job, _rel
