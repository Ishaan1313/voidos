"""
modules/activity.py — Activity (audit) log, outbound notifications, and the in-app notification feed.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from datetime import datetime
from datetime import timezone
from flask import request
import json
import random
import threading
import time
import uuid
from modules.config import AUDIT_FILE, OS_NAME, _void_path
from modules.web import _atomic_write_json, _audit_lock, _push_to_live_sessions
from modules.auth import _ip
from modules.users import _users_load
from modules import notifications, secret_store


# ── Audit log — append-only JSONL, read newest-first, lazily trimmed ────
def _audit(action: str, user: dict | None = None, target: str = "", detail: str = "", ip: str = None):
    """Record one audit entry. Never raises — logging must never break a request."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "username": (user or {}).get("username", "system"),
            "role": (user or {}).get("role", ""),
            "ip": ip if ip is not None else (_ip() if request else ""),
            "action": action,
            "target": target,
            "detail": detail,
        }
        with _audit_lock:
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            if random.random() < 0.02:  # trim occasionally, not on every write
                _audit_trim()
        _maybe_notify(entry)
    except Exception:
        pass

# ── Notifications (email / Discord / Telegram) for major events ─────────
# A curated subset of the audit-action vocabulary above — not every audit
# entry (file uploads, renames, etc. would be constant noise), just the
# security- and administration-relevant ones a home-server owner actually
# wants pushed to them, matching the examples this feature was asked for
# ("user logged in", "admin setting changed"). Title only, kept short —
# the "detail"/"target" fields (already on the entry) fill in the body.
MAJOR_EVENTS = {
    "login": "\U0001F513 New login",
    "login_failed": "\u26a0\ufe0f Failed login attempt",
    "user_created": "\U0001F464 User created",
    "user_deleted": "\U0001F5D1\ufe0f User deleted",
    "password_changed": "\U0001F511 Password changed",
    "password_reset": "\U0001F511 Password reset",
    "2fa_enabled": "\U0001F6E1\ufe0f Two-factor sign-in turned on",
    "2fa_disabled": "\u26a0\ufe0f Two-factor sign-in turned off",
    "2fa_reset": "\u26a0\ufe0f Two-factor sign-in reset by an admin",
    "2fa_backup_used": "\U0001F511 Backup code used to sign in",
    "2fa_backup_regenerated": "\U0001F511 Backup codes regenerated",
    "role_changed": "\U0001F6E1\ufe0f Role changed",
    "permission_changed": "\U0001F6E1\ufe0f Permission changed",
    "server_settings_changed": "\u2699\ufe0f Server setting changed",
    "panic_triggered": "\U0001F6A8 Panic button triggered",
    "panic_undo": "\u21a9\ufe0f Panic undone",
    "catalog_updated": "🧩 App list updated",
    "domain_connected": "\U0001F310 Custom domain connected",
    "domain_disconnected": "\U0001F310 Custom domain disconnected",
    "setup_completed": "\U0001F389 Void OS setup completed",
    "superuser_transferred": "\U0001F451 Super User transferred",
    "session_revoked": "\U0001F512 Session revoked",
    "sessions_revoked_others": "\U0001F512 Other sessions revoked",
    "backup_exported": "\U0001F4BE Backup exported",
    "backup_imported": "\U0001F4BE Backup imported",
    "audit_log_cleared": "\U0001F9F9 Audit log cleared",
    "cross_site_blocked": "\U0001F6E1\ufe0f Cross-site request blocked",
    "disk_space_low": "\U0001F4BD Disk space low",
    "app_install_failed": "\U0001F4E6 App install failed",
    "cloud_sync_failed": "\u2601\ufe0f Cloud sync failing",
}
NOTIFY_SETTINGS_FILE = _void_path("VOID_NOTIFY_SETTINGS_FILE", "notify_settings.json")
_notify_lock = threading.RLock()
_NOTIFY_DEFAULTS = {
    "events": {k: True for k in MAJOR_EVENTS},  # per-event on/off, all on by default
    "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "username": "", "password": "",
              "use_tls": True, "from_addr": "", "to_addr": ""},
    "discord": {"enabled": False, "webhook_url": ""},
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
}

def _notify_load() -> dict:
    """Notification settings. The file holds an SMTP password, a Discord webhook and
    a Telegram bot token, so it is stored encrypted (modules/secret_store.py) and
    owner-only. A plain file from an older version is read as-is and re-saved
    encrypted. If the secret key changed, the unreadable file is set aside and the
    channels start out unconfigured."""
    with _notify_lock:
        try:
            stored = json.loads(NOTIFY_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        if secret_store.is_sealed(stored):
            try:
                stored = secret_store.unseal(stored)
            except ValueError:
                try:
                    NOTIFY_SETTINGS_FILE.replace(NOTIFY_SETTINGS_FILE.with_name(
                        NOTIFY_SETTINGS_FILE.name + f".unreadable-{int(time.time())}"))
                except OSError:
                    pass
                stored = {}
        elif stored and secret_store.available():
            try:
                _atomic_write_json(NOTIFY_SETTINGS_FILE, secret_store.seal(stored), private=True)  # migrate
            except OSError:
                pass
        if not isinstance(stored, dict):
            stored = {}
    out = json.loads(json.dumps(_NOTIFY_DEFAULTS))  # deep copy, cheap and simple for a tiny dict
    for k in ("email", "discord", "telegram"):
        out[k].update(stored.get(k, {}))
    out["events"].update(stored.get("events", {}))
    return out

def _notify_save(d: dict) -> None:
    with _notify_lock:
        _atomic_write_json(NOTIFY_SETTINGS_FILE, secret_store.seal(d), private=True)

def _notify_dispatch(cfg: dict, title: str, body: str) -> dict:
    """Fires every enabled+configured channel, best-effort, and returns a
    per-channel {ok, message} result — used by both the real event path
    and the Settings 'Send test notification' button, so 'test' actually
    exercises the exact same send path a real event would."""
    results = {}
    if cfg["email"]["enabled"] and cfg["email"]["smtp_host"] and cfg["email"]["to_addr"]:
        e = cfg["email"]
        ok, msg = notifications.send_email(e["smtp_host"], int(e["smtp_port"] or 587), e["username"],
            e["password"], bool(e["use_tls"]), e["from_addr"] or e["username"], e["to_addr"], title, body)
        results["email"] = {"ok": ok, "message": msg}
    if cfg["discord"]["enabled"] and cfg["discord"]["webhook_url"]:
        ok, msg = notifications.send_discord(cfg["discord"]["webhook_url"], title, body)
        results["discord"] = {"ok": ok, "message": msg}
    if cfg["telegram"]["enabled"] and cfg["telegram"]["bot_token"] and cfg["telegram"]["chat_id"]:
        ok, msg = notifications.send_telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"], title, body)
        results["telegram"] = {"ok": ok, "message": msg}
    return results

# Delivery status per channel, shown under each channel in Settings so a
# broken webhook or expired password is visible instead of silently failing.
NOTIFY_STATUS_FILE = _void_path("VOID_NOTIFY_STATUS_FILE", "notify_status.json")

def _notify_status_load() -> dict:
    try:
        return json.loads(NOTIFY_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def _notify_record(results: dict, title: str) -> None:
    with _notify_lock:
        st = _notify_status_load()
        for ch, r in results.items():
            rec = st.setdefault(ch, {})
            rec["last_attempt"] = time.time()
            rec["last_title"] = title[:120]
            if r["ok"]:
                rec.update(last_ok=time.time(), last_error="", fail_streak=0)
            else:
                rec.update(last_error=r["message"][:300], last_error_at=time.time(),
                           fail_streak=int(rec.get("fail_streak", 0)) + 1)
        try:
            _atomic_write_json(NOTIFY_STATUS_FILE, st)
        except OSError:
            pass

def _notify_send_with_retry(cfg: dict, title: str, body: str) -> None:
    """Background thread: send; any channel that fails is retried once
    after 30s (most failures — a network blip, a 429 — are transient).
    A channel still failing after that is recorded, logged to the Error
    Log, and admins get an in-app notification (at most once per channel
    per 6h, so a dead webhook can't flood the bell)."""
    results = _notify_dispatch(cfg, title, body)
    failed = [ch for ch, r in results.items() if not r["ok"]]
    if failed:
        time.sleep(30)
        retry_cfg = json.loads(json.dumps(cfg))
        for ch in ("email", "discord", "telegram"):
            if ch not in failed:
                retry_cfg[ch]["enabled"] = False
        results.update(_notify_dispatch(retry_cfg, title, body))
    _notify_record(results, title)
    for ch, r in results.items():
        if r["ok"]:
            continue
        _record_error({"ref": uuid.uuid4().hex[:8], "ts": time.time(), "source": "notification",
                       "code": f"{ch}_failed", "status": 0, "message": r["message"],
                       "exception": "", "route": "", "context": title, "user": "", "trace": ""})
        if _throttle_ok(f"notify-fail:{ch}", 6 * 3600):
            _inapp_notify_admins(f"{ch.title()} notifications are failing", r["message"],
                                 level="error", kind="notify_failed", link="settings")

# Duplicate suppression: the same event about the same target from the same
# place within the window is counted rather than re-sent, then reported as
# "(+N similar)" on the next one that does go out. Without this, one
# password-guessing script produced one email per guess.
_NOTIFY_DEDUP_WINDOW = 600
_notify_dedup: dict = {}
_throttle_marks: dict = {}

def _throttle_ok(key: str, window: float) -> bool:
    now = time.time()
    if now - _throttle_marks.get(key, 0) < window:
        return False
    _throttle_marks[key] = now
    return True

def _maybe_notify(entry: dict) -> None:
    """Called from _audit() for every logged action — cheap no-op for the
    vast majority (not a MAJOR_EVENTS key) so this never adds overhead to
    routine file operations. Actual sending happens on a daemon thread:
    SMTP/webhook calls are real network I/O and must never make the
    triggering request (a login, a settings save) wait on them."""
    _inapp_for_audit(entry)
    title = MAJOR_EVENTS.get(entry["action"])
    if not title:
        return
    cfg = _notify_load()
    if not cfg["events"].get(entry["action"], True):
        return
    if not any(cfg[c]["enabled"] for c in ("email", "discord", "telegram")):
        return  # skip the config-load-and-thread-spawn entirely when nothing's on
    key = f"{entry['action']}|{entry.get('target') or ''}|{entry.get('ip') or ''}"
    now = time.time()
    rec = _notify_dedup.get(key)
    if rec and now - rec["t"] < _NOTIFY_DEDUP_WINDOW:
        rec["suppressed"] += 1
        return
    suppressed = rec["suppressed"] if rec else 0
    _notify_dedup[key] = {"t": now, "suppressed": 0}
    if len(_notify_dedup) > 2000:
        for k in [k for k, v in _notify_dedup.items() if now - v["t"] > _NOTIFY_DEDUP_WINDOW]:
            _notify_dedup.pop(k, None)
    who = entry["username"] or "system"
    body = f"User: {who} ({entry['role'] or '—'})\nIP: {entry['ip'] or '—'}"
    if entry["target"]:
        body += f"\nTarget: {entry['target']}"
    if entry["detail"]:
        body += f"\nDetail: {entry['detail']}"
    body += f"\nTime: {entry['ts']}"
    if suppressed:
        body += f"\n(+{suppressed} similar in the last {_NOTIFY_DEDUP_WINDOW // 60} minutes, not sent individually)"
    body += f"\nServer: {OS_NAME}"
    threading.Thread(target=_notify_send_with_retry, args=(cfg, title, body), daemon=True).start()

# ── In-app notifications (the bell) ─────────────────────────────────────
# Per-user feed, persisted, capped. Pushed live over /ws/control to every
# open tab of that user, so finished transfers, failed installs and
# security events show up without a reload — and are still there later,
# unlike a toast that disappears after a few seconds.
INAPP_FILE = _void_path("VOID_INAPP_NOTIFICATIONS_FILE", "notifications.json")
_INAPP_MAX_PER_USER = 100
_inapp_lock = threading.Lock()

def _inapp_load() -> dict:
    try:
        d = json.loads(INAPP_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

def _inapp_notify(uid: str, title: str, body: str = "", level: str = "info",
                  kind: str = "", link: str | None = None, ref: str | None = None, data: dict | None = None,
                  read: bool = False) -> dict:
    item = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "title": str(title)[:160], "body": str(body)[:600],
            "level": level if level in ("info", "success", "warning", "error") else "info",
            "kind": kind, "link": link, "ref": ref, "data": data or {}, "read": bool(read)}
    with _inapp_lock:
        feed = _inapp_load()
        items = feed.get(uid, [])
        items.insert(0, item)
        feed[uid] = items[:_INAPP_MAX_PER_USER]
        try:
            _atomic_write_json(INAPP_FILE, feed)
        except OSError:
            pass
    try:
        _push_to_live_sessions(uid, {"type": "notification", "item": item})
    except Exception:
        pass
    return item

def _inapp_update(uid: str, nid: str, **changes) -> dict | None:
    """Change an existing notification in place (a progress bar that advances, an
    "Installing..." that turns into "Installed") and push the new version to every
    open tab. Accepts title, body, level, link, read and data (merged into the
    item's data). Returns the item, or None if it no longer exists (cleared)."""
    item = None
    with _inapp_lock:
        feed = _inapp_load()
        items = feed.get(uid, [])
        item = next((i for i in items if i.get("id") == nid), None)
        if item is None:
            return None
        if "title" in changes: item["title"] = str(changes["title"])[:160]
        if "body" in changes: item["body"] = str(changes["body"])[:600]
        if changes.get("level") in ("info", "success", "warning", "error"): item["level"] = changes["level"]
        if "link" in changes: item["link"] = changes["link"]
        if "read" in changes: item["read"] = bool(changes["read"])
        if isinstance(changes.get("data"), dict):
            item["data"] = {**(item.get("data") or {}), **changes["data"]}
        item["updated"] = time.time()
        try:
            _atomic_write_json(INAPP_FILE, feed)
        except OSError:
            pass
    try:
        _push_to_live_sessions(uid, {"type": "notification_update", "item": item})
    except Exception:
        pass
    return item

def _admin_ids() -> list:
    return [u["id"] for u in _users_load() if u.get("role") in ("admin", "superuser")]

def _inapp_notify_admins(title: str, body: str = "", **kw) -> None:
    for uid in _admin_ids():
        _inapp_notify(uid, title, body, **kw)

# Audit actions that also go to the bell. "admins" = every admin/superuser;
# "target" = the user the action was done *to* (looked up by username).
_INAPP_AUDIT = {
    "login_failed":          ("admins", "warning", "Failed sign-in attempt", "security"),
    "cross_site_blocked":    ("admins", "warning", "Blocked a cross-site request", "security"),
    "panic_triggered":       ("admins", "error",   "Panic button triggered", "security"),
    "superuser_transferred": ("admins", "warning", "Super User transferred", "security"),
    "backup_imported":       ("admins", "warning", "Backup restored", "admin"),
    "audit_log_cleared":     ("admins", "warning", "Activity log cleared", "security"),
    "app_install_failed":    ("admins", "error",   "App install failed", "apps"),
    "disk_space_low":        ("admins", "error",   "Disk space low", "system"),
    "cloud_sync_failed":     ("admins", "error",   "Cloud sync failing", "system"),
    "password_reset":        ("target", "warning", "Your password was reset", "security"),
    "2fa_disabled":          ("target", "warning", "Two-factor sign-in was turned off", "security"),
    "2fa_reset":             ("target", "warning", "Your two-factor sign-in was reset by an admin", "security"),
    "2fa_backup_used":       ("target", "warning", "A backup code was used to sign in", "security"),
    "role_changed":          ("target", "info",    "Your role changed", "account"),
    "permission_changed":    ("target", "info",    "Your permissions changed", "account"),
    "session_revoked":       ("target", "warning", "One of your sessions was signed out", "security"),
}

def _inapp_for_audit(entry: dict) -> None:
    spec = _INAPP_AUDIT.get(entry.get("action"))
    if not spec:
        return
    who_gets, level, title, kind = spec
    actor = entry.get("username") or "system"
    parts = []
    if entry.get("target"): parts.append(str(entry["target"]))
    if entry.get("detail"): parts.append(str(entry["detail"]))
    body = " — ".join(parts)
    if who_gets == "admins":
        # Repeated security noise (e.g. guessing) is collapsed: one bell item
        # per action+target+IP per 10 minutes.
        if not _throttle_ok(f"inapp:{entry['action']}:{entry.get('target')}:{entry.get('ip')}", 600):
            return
        if entry.get("username") or entry.get("ip"):
            body = (f"{body} (by {actor}" + (f", from {entry['ip']}" if entry.get("ip") else "") + ")").strip()
        _inapp_notify_admins(title, body, level=level, kind=kind, link="access")
    else:
        target = next((u for u in _users_load() if u.get("username") == entry.get("target")), None)
        if target and target.get("username") != entry.get("username"):
            _inapp_notify(target["id"], title, f"By {actor}" + (f" — {entry['detail']}" if entry.get("detail") else ""),
                          level=level, kind=kind, link="settings")


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _audit_trim, _record_error
    from modules.error_log import _audit_trim, _record_error
