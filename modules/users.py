"""
modules/users.py — User store, server-wide UI default, .env read/write, terminal password, per-user wallpapers.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from itsdangerous import BadSignature
from itsdangerous import SignatureExpired
from pathlib import Path
import json
import os
import threading
from modules.config import SESSION_HRS, USERS_FILE, VOID_ROOT, WALLPAPER_DIR, _void_path
from modules.web import _COOKIE, _atomic_write_json, _serial, _sessions_lock


# ══════════════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════
def _users_load() -> list:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8")).get("users", [])
        except Exception:
            pass
    return []

def _users_save(users: list):
    _atomic_write_json(USERS_FILE, {"users": users})
    from modules.web import _broadcast_coalesced
    _broadcast_coalesced("users", {"type": "users_changed"}, roles=("admin", "superuser"))

def _get_user_by_id(uid: str) -> dict | None:
    return next((u for u in _users_load() if u.get("id") == uid), None)

def _get_user_by_username(uname: str) -> dict | None:
    return next((u for u in _users_load() if u.get("username") == uname), None)

def _safe_user(u: dict) -> dict:
    """Return user dict without password_hash or any two-factor secret; only
    whether 2FA is on is exposed (`twofa_enabled`)."""
    out = {k: v for k, v in u.items() if k not in ("password_hash", "twofa", "twofa_pending")}
    out["twofa_enabled"] = bool((u.get("twofa") or {}).get("enabled"))
    return out

DEFAULT_SETTINGS = {
    "display_name": "",
    "show_blobs": True,
    "sidebar_pinned": False,
    "theme": "horizon",
    "blob_speed": "slow",
    # Clock format on the home screen and top bar: "12" (default) or "24".
    "time_format": "12",
    # "auto" defers to the server-wide default (SERVER_SETTINGS_FILE /
    # VOID_UI_MODE_DEFAULT below); "classic" / "home" always overrides it
    # for this account regardless of the server default — see Settings →
    # Appearance and _resolve_ui_mode().
    "ui_mode": "auto",
    # "" = default gradient background; "preset:<id>" one of WALLPAPER_PRESETS;
    # "custom:<filename>" an image the user uploaded via /api/me/wallpaper/upload.
    "wallpaper": "",
}
VALID_THEMES = {"horizon", "void", "light", "emerald", "crimson"}
VALID_UI_MODES = {"auto", "classic", "home"}

WALLPAPER_PRESETS = {"nebula", "aurora", "sunset", "mono", "forest", "midnight"}

# Home-cloud-style default subfolders (scaffolded
# inside every user's own space). Only ever created inside a guest's own
# home/personal/{username} — Admins/Super User browse the full FILE_ROOT
# tree directly and don't have a single "personal folder" the same way.
DEFAULT_USER_SUBFOLDERS = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]

def _ensure_user_subfolders(personal_dir: Path) -> None:
    for name in DEFAULT_USER_SUBFOLDERS:
        try:
            (personal_dir / name).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # best-effort — a permissions/disk hiccup here shouldn't block login

# ── Server-wide UI mode default ─────────────────────────────────────────
# A per-account "ui_mode":"auto" setting resolves to this. Seeded from
# VOID_UI_MODE_DEFAULT in .env on first run, then editable at runtime by
# the Super User (Settings → Appearance, server-wide section) without a
# restart — persisted in SERVER_SETTINGS_FILE, the same small-JSON-store
# pattern USERS_FILE/AUDIT_FILE already use elsewhere in this app.
SERVER_SETTINGS_FILE = _void_path("VOID_SERVER_SETTINGS_FILE", "server_settings.json")
_server_settings_lock = threading.Lock()

def _server_settings_load() -> dict:
    try:
        return json.loads(SERVER_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def _server_settings_save(d: dict) -> None:
    _atomic_write_json(SERVER_SETTINGS_FILE, d)

def _ui_mode_default() -> str:
    with _server_settings_lock:
        val = _server_settings_load().get("ui_mode_default")
    if val in ("classic", "home"):
        return val
    env_val = os.environ.get("VOID_UI_MODE_DEFAULT", "classic").strip().lower()
    return env_val if env_val in ("classic", "home") else "classic"

# ── .env read/write (onboarding wizard) ─────────────────────────────────
# Void OS reads .env exactly once, at process start (see the top-of-file
# load_dotenv() call and the VOID_SECRET_KEY boot check right after it) —
# writing here can never make a change take effect in the running
# process; every write from the wizard below is paired with a clear
# "restart to apply" step, never a claim that it applied immediately.
ENV_FILE = VOID_ROOT / ".env"

def _write_env_updates(updates: dict) -> None:
    """Merges `updates` (KEY -> value-string, or KEY -> None to remove that
    line) into .env. Delegates to modules/envfile.py — the same editor the
    installer uses — so a setting's inline comment survives being changed,
    a commented-out example line (`# KEY=…`) is switched on in place instead
    of being duplicated, and values are quoted safely."""
    from modules import envfile
    envfile.update(ENV_FILE, updates)

def _needs_onboarding() -> bool:
    """True until the setup wizard's final step has actually run — distinct
    from _needs_setup() (which only tracks whether an admin account
    exists) so the wizard can keep offering its later steps (storage
    paths, terminal password, optional integrations) across a closed tab/
    browser restart, rather than only ever getting one shot at them
    immediately after admin-account creation."""
    if _needs_setup():
        return True
    with _server_settings_lock:
        return not _server_settings_load().get("onboarding_complete", False)

def _mark_onboarding_complete() -> None:
    with _server_settings_lock:
        cur = _server_settings_load()
        cur["onboarding_complete"] = True
        _server_settings_save(cur)

# ── Terminal password (separate from account login) ─────────────────────
# A second, independent gate in front of the Terminal tab — knowing an
# account's password is enough to log in, but NOT enough to open a shell,
# unless this is also known. Same idea, and the same env-stored-hash
# mechanism, as the pre-existing Access Gate PIN for app tunnels
# (VOID_GATE_PIN_HASH) — one more thing this needs a restart to change,
# same as everything else the wizard writes to .env.
TERM_PW_HASH = os.environ.get("VOID_TERMINAL_PASSWORD_HASH", "")

def _terminal_session_unlocked(sid: str) -> bool:
    if not TERM_PW_HASH:
        return True  # no terminal password configured — nothing to unlock
    if not sid:
        return False
    return any(s.get("id") == sid and s.get("terminal_unlocked") for s in _sessions_load())

def _mark_terminal_unlocked(sid: str) -> None:
    with _sessions_lock:
        sessions = _sessions_load()
        for s in sessions:
            if s.get("id") == sid:
                s["terminal_unlocked"] = True
        _sessions_save(sessions)

def _sid_from_cookies(cookies) -> str | None:
    tok = cookies.get(_COOKIE, "")
    if not tok:
        return None
    try:
        return _serial.loads(tok, max_age=SESSION_HRS * 3600).get("sid")
    except (BadSignature, SignatureExpired):
        return None

def _resolve_ui_mode(settings: dict) -> str:
    m = settings.get("ui_mode", "auto")
    return m if m in ("classic", "home") else _ui_mode_default()

# ── Custom wallpapers ────────────────────────────────────────────────────
def _user_wallpaper_dir(user: dict) -> Path:
    d = WALLPAPER_DIR / user["id"]
    d.mkdir(parents=True, exist_ok=True)
    return d

def _safe_wallpaper_filename(user: dict, filename: str) -> Path | None:
    """Resolve filename inside this user's own wallpaper folder, or None
    if it doesn't exist / would escape the folder (Path.relative_to guard,
    same technique _safe() uses for the main file browser)."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        return None
    p = (_user_wallpaper_dir(user) / filename).resolve()
    try:
        p.relative_to(WALLPAPER_DIR.resolve())
    except ValueError:
        return None
    return p if p.exists() else None

def _wallpaper_url(user: dict, wallpaper_setting: str) -> str:
    if not wallpaper_setting:
        return ""
    if wallpaper_setting.startswith("preset:"):
        return wallpaper_setting  # frontend maps preset ids to CSS gradients directly
    if wallpaper_setting.startswith("custom:"):
        fname = wallpaper_setting.split(":", 1)[1]
        if _safe_wallpaper_filename(user, fname):
            return f"/api/me/wallpaper/file/{fname}"
    return ""
VALID_BLOB_SPEEDS = {"static", "slow", "fast"}
VALID_TIME_FORMATS = {"12", "24"}

def _get_user_settings(user: dict) -> dict:
    """Merge stored settings over defaults so new keys are always present,
    and silently drop any stored keys that are no longer recognized
    (e.g. a setting removed in a later version)."""
    stored = user.get("settings", {}) if user else {}
    stored = {k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}
    # a value this version doesn't know (a renamed theme or layout from an older version) falls back to the default
    if stored.get("theme") not in VALID_THEMES:
        stored.pop("theme", None)
    if stored.get("ui_mode") not in VALID_UI_MODES:
        stored.pop("ui_mode", None)
    return {**DEFAULT_SETTINGS, **stored}

def _save_user_settings(uid: str, patch: dict) -> dict | None:
    """Merge `patch` into this user's stored settings. Returns merged settings or None."""
    users = _users_load()
    for i, u in enumerate(users):
        if u["id"] == uid:
            current = u.get("settings", {})
            current.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            users[i]["settings"] = current
            _users_save(users)
            return _get_user_settings(users[i])
    return None


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _needs_setup, _sessions_load, _sessions_save
    from modules.security import _needs_setup
    from modules.auth import _sessions_load, _sessions_save
