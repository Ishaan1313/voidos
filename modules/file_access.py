"""
modules/file_access.py — Who may access which file path (_safe), protected folders, guest quotas.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify
from modules.winplat import IS_WIN
from pathlib import Path
import json
import os
import shutil
import time
from modules.config import (
    VOID_ROOT, APP_DATA_DIR, DRIVES_PARTS, EXTERNAL_DIR, FILE_ROOT, GUEST_QUOTA_MB, MIN_FREE_MB, SHARED_DIR,
    SHARED_PARTS, SHARED_QUOTA_MB, SHARED_REL, USERS_DIR, USERS_PARTS, USERS_REL,
)
from modules.web import _atomic_write_json
from modules.users import _ensure_user_subfolders, _users_load
from modules.error_log import _has_full_file_access
from modules import external_storage, winplat


# ══════════════════════════════════════════════════════════════════════
# FILE PERMISSIONS
# ══════════════════════════════════════════════════════════════════════
def _check_file_access(path: Path, user: dict) -> bool:
    """
    Superuser / unrestricted Admin → full access.
    Guest, or an Admin with guest_file_view turned off → only the Shared folder
    and their own folder under Users/.
    """
    if _has_full_file_access(user):
        return True
    try:
        parts = path.relative_to(FILE_ROOT).parts
    except ValueError:
        return False

    fold = (lambda x: x.casefold()) if IS_WIN else (lambda x: x)   # NTFS: "Bob" and "bob" are the same folder
    if tuple(fold(p) for p in parts[:len(SHARED_PARTS)]) == tuple(fold(p) for p in SHARED_PARTS):
        return True
    if tuple(fold(p) for p in parts[:len(USERS_PARTS)]) == tuple(fold(p) for p in USERS_PARTS):
        rest = parts[len(USERS_PARTS):]
        if not rest:         # Users/ itself is not listable
            return False
        own = user.get("personal_folder", user.get("username", ""))
        return fold(rest[0]) == fold(own)
    return False

# Void OS's own program and state files. No file API, share link, WebDAV request or search may reach
# them, whoever asks: they hold password hashes, session ids and the secret key that signs every login.
# (Only matters when the files folder contains, or is, the app folder -- an install that predates
# VOID_FILE_ROOT used the whole home folder.)
_VOID_OWN_NAMES = {
    ".env", "users.json", "sessions.json", "shares.json", "apps.json", "app_stacks.json", "cloud_tokens.json",
    "cloud_sync.json", "external_storage.json", "bluetooth_devices.json", "intercom.json", "notifications.json",
    "server_settings.json", "custom_domain.json", "audit.log", "error_log.jsonl", "voidos.py", "VERSION",
    "requirements.txt", "modules", "static", "templates", "first setup", ".venv", ".tools", "tmp",
    "app_catalog", "app_catalog.zip", "app_catalog_updated.zip", "icon-cache", "wallpapers", "rclone_conf",
    "install.log", "voidos.log",
}


def _is_void_internal(candidate) -> bool:
    """True when `candidate` is part of Void OS's own folder rather than user data."""
    try:
        rel = Path(os.path.normpath(candidate)).relative_to(Path(os.path.normpath(VOID_ROOT)))
    except ValueError:
        return False
    if FILE_ROOT != VOID_ROOT and _is_within(FILE_ROOT, VOID_ROOT):
        return not _is_within(candidate, FILE_ROOT)          # the files folder sits inside the app folder
    if FILE_ROOT == VOID_ROOT:
        return bool(rel.parts) and rel.parts[0] in _VOID_OWN_NAMES   # same folder: protect Void OS's own names
    return True                                              # the files folder CONTAINS the app folder


def _name_error(name: str) -> str | None:
    """Single path-component validation for anything that creates or renames
    an entry from a user-typed name. Cross-platform basics plus Windows' own
    rules (see winplat.win_name_problem) -- on Windows a name like "C:x"
    would otherwise make `parent / name` a drive-relative path outside
    FILE_ROOT entirely, and "a.txt:x" would write an alternate data stream."""
    if not name or not name.strip():
        return "The name can't be empty."
    if name in (".", ".."):
        return "\u201c.\u201d and \u201c..\u201d aren't allowed as names."
    if "/" in name or "\\" in name:
        return "Names can't contain / or \\."
    if "\x00" in name:
        return "That name contains an invalid character."
    return winplat.win_name_problem(name)

def _protected_reason(p: Path) -> str | None:
    """Folders that make up Void OS's own structure and must never be
    deleted, moved, renamed or overwritten through the file APIs or WebDAV
    -- by anyone. _check_file_access() grants guests "home/shared/**", which
    includes home/shared ITSELF, so before this a guest could trash or move
    the entire Shared folder (or its own personal root, dodging the quota).
    Returns a short reason, or None if the path is an ordinary entry."""
    try:
        parts = Path(os.path.normpath(p)).relative_to(FILE_ROOT).parts
    except ValueError:
        return None
    if not parts:
        return "the root folder"
    if _is_within(VOID_ROOT, Path(os.path.normpath(p))):
        return "a folder that contains Void OS itself"
    legacy_home = ("home",) if (SHARED_PARTS[0] == "home" or USERS_PARTS[0] == "home") else None
    if parts in (SHARED_PARTS, USERS_PARTS, DRIVES_PARTS, (TRASH_DIRNAME,)) or (legacy_home and parts == legacy_home):
        return f"the {parts[-1]} folder"
    if len(parts) == len(USERS_PARTS) + 1 and parts[:len(USERS_PARTS)] == USERS_PARTS:
        fold = (lambda x: x.casefold()) if IS_WIN else (lambda x: x)
        if any(fold(u.get("personal_folder", u.get("username", ""))) == fold(parts[-1]) for u in _users_load()):
            return "a user's personal folder"
    if len(parts) == len(DRIVES_PARTS) + 1 and parts[:len(DRIVES_PARTS)] == DRIVES_PARTS \
            and (external_storage.is_mounted(p) or winplat.is_link_dir(p)):
        return "a mounted drive or share -- unmount it from Settings > External Storage instead"
    return None

def _is_within(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.normpath(child)).relative_to(Path(os.path.normpath(parent)))
        return True
    except ValueError:
        return False

def _external_link_path(raw: str):
    """Windows External Storage support for _safe(). On Linux, USB drives and
    SMB/NFS shares are bind/kernel-mounted *at* FILE_ROOT/external/<slug>, so
    a resolved path stays inside FILE_ROOT. Windows has no bind mounts: those
    entries are NTFS junctions (USB drive letters) or directory symlinks (UNC
    shares), and .resolve() follows them to the drive letter / UNC path,
    which the plain relative_to(FILE_ROOT) check would reject.

    Accepted only when ALL of: the *lexical* path is under EXTERNAL_DIR; its
    first component is a link sitting directly in EXTERNAL_DIR (only Void
    OS's own External Storage code creates those -- the file manager has no
    way to create links); and the fully-resolved path is still inside that
    link's own target (so "external/usb/../../x" or a nested symlink can't
    escape). Returns the lexical path (so relative_to(FILE_ROOT) keeps
    working for every caller) or None."""
    if not IS_WIN:
        return None
    lexical = Path(os.path.normpath(FILE_ROOT / raw.lstrip("/\\")))
    try:
        rel = lexical.relative_to(EXTERNAL_DIR)
    except ValueError:
        return None
    if not rel.parts:
        return None
    link = EXTERNAL_DIR / rel.parts[0]
    if not winplat.is_link_dir(link):
        return None
    try:
        target = link.resolve()
        lexical.resolve().relative_to(target)
    except (OSError, ValueError):
        return None
    return lexical

def _safe(raw: str, user: dict | None = None) -> Path:
    """Resolve path within FILE_ROOT and enforce user permissions."""
    candidate = (FILE_ROOT / raw.lstrip("/\\")).resolve()
    try:
        candidate.relative_to(FILE_ROOT)
    except ValueError:
        linked = _external_link_path(raw)
        if linked is None:
            raise PermissionError("Path is outside the file root")
        candidate = linked
    # APP_DATA_DIR now defaults to a sibling of FILE_ROOT/home on disk (see
    # its definition above), which means it would otherwise pass the
    # relative_to(FILE_ROOT) check above — including for a full-access
    # admin/superuser, since _check_file_access short-circuits to True for
    # them regardless of path. App Store data (an Immich photo library, a
    # Vaultwarden vault file) is opaque infrastructure state that isn't meant
    # to be casually browsed/renamed/deleted through the file manager, same
    # as any other container data directory — this keeps it out of
    # /api/files scope entirely, deliberately, not as a side effect of the
    # two paths now living next to each other on disk.
    try:
        candidate.relative_to(APP_DATA_DIR)
        raise PermissionError("Access denied")
    except ValueError:
        pass
    if _is_void_internal(candidate):
        raise PermissionError("Access denied")
    if user and not _check_file_access(candidate, user):
        raise PermissionError("Access denied")
    return candidate

# ══════════════════════════════════════════════════════════════════════
# RECYCLE BIN — deletes move here first; "Empty Trash" is what's permanent
# ══════════════════════════════════════════════════════════════════════
TRASH_DIRNAME = ".void-trash"
TRASH_DIR  = FILE_ROOT / TRASH_DIRNAME
TRASH_META = TRASH_DIR / "trash.json"

def _walk_files(root: Path):
    """Regular files under `root`, never following symlinks or junctions.
    Folder downloads (zip) and folder share links used rglob() + is_file(),
    and is_file() follows symlinks -- so a symlink planted inside a shared
    folder (by a container with that folder bind-mounted, a terminal user,
    or simply sitting on a USB stick that got mounted) made a guest's zip or
    a PUBLIC share link include e.g. /etc/shadow, read with Void OS's own
    privileges. _safe() already refuses such links for single-file access;
    this closes the same hole for folder walks."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if not os.path.islink(os.path.join(dirpath, d))
                             and not winplat.is_link_dir(os.path.join(dirpath, d))
                             and not _is_void_internal(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp) or not os.path.isfile(fp) or _is_void_internal(fp):
                continue
            yield Path(fp)

def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file(): total += f.stat().st_size
        except Exception: pass
    return total

def _trash_load() -> list:
    if TRASH_META.exists():
        try:
            data = json.loads(TRASH_META.read_text(encoding="utf-8"))
            if isinstance(data, list): return data
        except Exception: pass
    return []

def _trash_save(items: list):
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(TRASH_META, items)

def _trash_visible(items: list, user: dict) -> list:
    """Full-access users (superuser, unrestricted admin) see everything;
    everyone else only sees items trashed from somewhere they still have
    access to (Shared or their own personal folder)."""
    out = []
    for it in items:
        try:
            if _check_file_access(FILE_ROOT / it["original_path"], user):
                out.append(it)
        except Exception: pass
    return out

# ── Per-guest storage quota (VOID_GUEST_QUOTA_MB, 0 = unlimited) ────────
def _personal_root(user: dict) -> Path:
    pf = user.get("personal_folder", user.get("username", ""))
    return (USERS_DIR / pf).resolve()

def _ensure_personal_folder(user: dict) -> Path:
    """The account's own "My Files" folder (every rank has one), created with the
    usual Documents / Pictures / Music... inside it the first time it's needed."""
    root = _personal_root(user)
    try:
        existed = root.is_dir()
        root.mkdir(parents=True, exist_ok=True)
        if not existed:
            _ensure_user_subfolders(root)
    except OSError:
        pass
    return root

def _in_personal_folder(user: dict, path: Path) -> bool:
    try:
        path.resolve().relative_to(_personal_root(user))
        return True
    except ValueError:
        return False

def _quota_used_bytes(user: dict) -> int:
    root = _personal_root(user)
    return _dir_size(root) if root.exists() else 0

def _quota_remaining_bytes(user: dict) -> int | None:
    """None means unlimited (non-guest roles, or no quota configured)."""
    if GUEST_QUOTA_MB <= 0 or user.get("role") != "guest":
        return None
    cap = GUEST_QUOTA_MB * 1024 * 1024
    return max(0, cap - _quota_used_bytes(user))

def _fmt_size(n: int) -> str:
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def _free_space_problem(path: Path, need: int = 0) -> str | None:
    """None if writing `need` more bytes still leaves VOID_MIN_FREE_MB free on the
    drive that holds `path`; otherwise a message saying why it's refused. The
    reserve keeps one big upload or copy from filling the drive and taking
    Void OS, Docker and everything else on it down."""
    try:
        free = shutil.disk_usage(str(path)).free
    except OSError:
        return None
    reserve = MIN_FREE_MB * 1024 * 1024
    if free - max(0, need) >= reserve:
        return None
    return (f"Not enough free disk space \u2014 this needs {_fmt_size(need)} and only {_fmt_size(free)} is free "
            f"(Void OS keeps {_fmt_size(reserve)} free so the server doesn't run out of room).")

_shared_size = {"t": 0.0, "v": 0}

def _shared_remaining_bytes(user: dict, dest: Path) -> int | None:
    """How much more a Guest may put into the Shared folder (VOID_SHARED_QUOTA_MB), or
    None when no cap applies to this user / destination."""
    if SHARED_QUOTA_MB <= 0 or user.get("role") != "guest":
        return None
    shared = SHARED_DIR
    try:
        dest.resolve().relative_to(shared.resolve())
    except ValueError:
        return None
    now = time.time()
    if now - _shared_size["t"] > 5:           # a full walk per request would be wasteful
        _shared_size["v"] = _dir_size(shared) if shared.exists() else 0
        _shared_size["t"] = now
    return max(0, SHARED_QUOTA_MB * 1024 * 1024 - _shared_size["v"])

def _shared_quota_error() -> str:
    return f"The Shared folder is full \u2014 Guests can add up to {SHARED_QUOTA_MB} MB there"

def _quota_error(mb_cap: int) -> str:
    return f"Storage quota exceeded — personal folder is limited to {mb_cap} MB"

def _guest_virtual_root(user: dict):
    """Return a virtual directory listing for guests at path=''."""
    entries = []
    shared = SHARED_DIR
    if shared.exists():
        entries.append({"name": "Shared", "type": "dir", "size": None,
            "modified": int(shared.stat().st_mtime), "path": SHARED_REL, "protected": True})
    pf = user.get("personal_folder", user.get("username", ""))
    personal = _ensure_personal_folder(user)
    _ensure_user_subfolders(personal)
    entries.append({"name": "My Files", "type": "dir", "size": None,
        "modified": int(personal.stat().st_mtime),
        "path": f"{USERS_REL}/{pf}", "protected": True})
    return jsonify({"path": "", "breadcrumb": [],
        "entries": entries, "virtual_root": True})

def _guest_breadcrumb(rel_parts: tuple, user: dict) -> list:
    """Build breadcrumb for guest, replacing deep paths with friendly names."""
    pf = user.get("personal_folder", user.get("username", ""))
    bc = [{"name": "Home", "path": ""}]
    sh, us = len(SHARED_PARTS), len(USERS_PARTS)
    if tuple(rel_parts[:sh]) == SHARED_PARTS:
        bc.append({"name": "Shared", "path": SHARED_REL})
        for i, part in enumerate(rel_parts[sh:], sh):
            bc.append({"name": part, "path": "/".join(rel_parts[:i+1])})
    elif tuple(rel_parts[:us]) == USERS_PARTS and len(rel_parts) > us and rel_parts[us] == pf:
        bc.append({"name": "My Files", "path": f"{USERS_REL}/{pf}"})
        for i, part in enumerate(rel_parts[us+1:], us+1):
            bc.append({"name": part, "path": "/".join(rel_parts[:i+1])})
    return bc
