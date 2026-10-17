"""
modules/config.py — Startup, environment (.env), paths and every configuration constant.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from modules import state


"""
Void OS — Multi-user self-hosted home server dashboard.
Auth: role-based (admin/guest), all credentials in users.json.
Run: python voidos.py
"""

import os, json, time, mimetypes, secrets, zipfile, shutil, warnings, sys, random
import subprocess, threading, io, struct, base64, uuid, re, atexit, platform, errno
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
from xml.sax.saxutils import escape as xml_escape  # WebDAV response bodies
from email.utils import formatdate                  # RFC1123 dates for WebDAV
from urllib.parse import urlparse, unquote, quote    # WebDAV Destination header / hrefs
import ipaddress
import urllib.request
from flask import (Flask, render_template, request, jsonify,
                   Response, send_file, redirect, make_response, g, after_this_request)
import psutil, tempfile
import yaml
from modules import consolelog, intercom, cloud_storage, cloud_sync, external_storage, notifications, winplat, errors
from modules.winplat import IS_WIN

# Windows: stdout/stderr may be a plain file or pipe (pythonw, NSSM, Task
# Scheduler, `> log.txt`) rather than a console, in which case Python encodes
# with the ANSI code page (cp1252) and the banner's "═"/"✓"/"⚠" characters
# raise UnicodeEncodeError and kill startup. Force UTF-8 with replacement so
# printing can never be what takes the server down.
# The app folder (where voidos.py lives) and its .env — needed before
# VOID_ROOT below, because .env has to be loaded first.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_ENV = os.path.join(_APP_DIR, ".env")

if IS_WIN:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_sock import Sock
from werkzeug.security import check_password_hash, generate_password_hash

try:
    # Explicit path: a bare load_dotenv() searches upward from this file's
    # folder and could pick up an unrelated .env higher up the disk.
    from dotenv import load_dotenv; load_dotenv(_APP_ENV)
except ImportError:
    pass

# ── First-run secret-key setup ──────────────────────────────────────────
# The first thing this module does after loading .env, deliberately BEFORE
# anything else below (Docker probing, App Store catalog extraction, the Flask
# app object, tunnels, Workers KV, all of it). If .env has no VOID_SECRET_KEY
# yet, this is a first run (or a not-yet-finished setup): create .env from the
# example's essentials if needed and write a generated key into it. Never fall
# back to a throwaway in-memory secret — that would sign everyone out on every
# restart without anyone necessarily noticing why.
_ENV_SECRET_KEY = os.environ.get("VOID_SECRET_KEY", "")
if not _ENV_SECRET_KEY:
    # First run without a key: create .env (from "first setup/.env.example")
    # if needed and write a fresh key into it, then keep starting. This used
    # to print the key and exit, leaving the user to paste it in by hand.
    from modules import envfile
    _generated_secret = secrets.token_hex(32)
    try:
        _example = os.path.join(_APP_DIR, "first setup", ".env.example")
        envfile.create_from_example(_APP_ENV, _example)
        envfile.sync_missing(_APP_ENV, _example)      # an existing short .env gains every listed setting
        envfile.update(_APP_ENV, {"VOID_SECRET_KEY": _generated_secret})
        os.environ["VOID_SECRET_KEY"] = _generated_secret
        _ENV_SECRET_KEY = _generated_secret  # used for SECRET_KEY below — without this the first run signed cookies with an EMPTY key
        consolelog.blank(); consolelog.say(f"Created a secret key in {_APP_ENV}"); consolelog.blank()
    except OSError as e:
        # Read-only folder or similar: fall back to telling the user what to add.
        consolelog.blank()
        consolelog.error(f"Couldn't write {_APP_ENV} ({e}). Add this line to it:")
        consolelog.blank()
        consolelog.say(f"VOID_SECRET_KEY={_generated_secret}")
        consolelog.blank()
        if sys.platform == "win32":
            os.system("pause")   # keep a double-clicked console window open long enough to read
        sys.exit(1)


# ── Optional platform libs ────────────────────────────────────────────
PTY_OK = False
if sys.platform != "win32":
    try:
        import pty, fcntl, termios
        import select as _sel
        PTY_OK = True
    except ImportError:
        pass

WIN_PTY_OK = False; _WinPTY = None
if sys.platform == "win32":
    try:
        from winpty import PtyProcess as _WinPTY; WIN_PTY_OK = True
    except ImportError:
        pass

state._docker = None; state.DOCKER_OK = False; state.DOCKER_ERROR = ""
def _try_docker():
    pass  # (shared value now lives in modules/state.py)
    try:
        import docker as _dsdk
    except ImportError:
        state.DOCKER_ERROR = "docker package not installed — run: pip install docker"; return
    last = ""
    try:
        c = _dsdk.from_env(timeout=3); c.ping(); state._docker = c; state.DOCKER_OK = True; return
    except Exception as e: last = str(e)
    if sys.platform != "win32":
        for sock in ["/var/run/docker.sock","/run/docker.sock",
                     os.path.expanduser("~/.docker/desktop/docker.sock"),
                     f"/run/user/{os.getuid()}/docker.sock"]:
            if os.path.exists(sock):
                try:
                    c = _dsdk.DockerClient(base_url=f"unix://{sock}",timeout=3)
                    c.ping(); state._docker = c; state.DOCKER_OK = True; return
                except Exception as e: last = str(e)
    if IS_WIN:
        # docker-py talks to Docker Desktop over the npipe:////./pipe/docker_engine
        # named pipe, which needs pywin32 — the most common Windows failure is
        # simply Docker Desktop not running (FileNotFoundError on the pipe).
        if "pywin32" in last or "win32" in last.lower() and "import" in last.lower():
            state.DOCKER_ERROR = "pywin32 missing — run: pip install pywin32"
        elif "Access is denied" in last or "Permission denied" in last:
            state.DOCKER_ERROR = "Access denied — add your user to the 'docker-users' group, then sign out and back in"
        elif "FileNotFound" in last or "pipe" in last.lower() or "refused" in last:
            state.DOCKER_ERROR = "Docker Desktop isn't running — start Docker Desktop and restart Void OS"
        else:
            state.DOCKER_ERROR = last[:200]
    else:
        state.DOCKER_ERROR = ("Permission denied — sudo usermod -aG docker $USER" if "Permission denied" in last
                        else "Docker daemon not running — sudo systemctl start docker" if "refused" in last
                        else last[:200])
_try_docker()

# ── Config ────────────────────────────────────────────────────────────
# Void OS's own install directory — everything that's Void's own bookkeeping
# (accounts, tiles, sessions, share links, installed-stack records, the
# audit log, the scratch/tmp folder) anchors here by default, deliberately
# NOT the process's current working directory. There are exactly two
# categories of data that are meant to live somewhere else entirely — real
# user storage (FILE_ROOT, e.g. an external drive or NAS mount) and Docker
# container data (APP_DATA_DIR, sized for a fast SSD or whatever bulk
# storage a self-hosted app actually needs) — both explicitly configurable
# below. Everything else is infrastructure state that belongs next to
# voidos.py itself, where an admin looking for it would actually find it.
# The app folder (where voidos.py, templates/ and static/ live). Defined
# relative to THIS file's location so it stays correct wherever the code
# lives: the module that defines it sits in <app>/modules/.
VOID_ROOT = Path(__file__).resolve().parent if (Path(__file__).resolve().parent / "voidos.py").exists() \
    else Path(__file__).resolve().parent.parent

def _void_path(env_var: str, default_name: str) -> Path:
    """Resolve one of Void OS's own bookkeeping files/folders. A relative
    value — whether it's the built-in default above or something explicitly
    set in .env — is always anchored to VOID_ROOT, never to the process's
    current working directory. Without this, launching Void OS from a
    different cwd (a systemd unit, a cron job, a desktop shortcut with a
    different "start in" folder, `python3 /some/other/path/voidos.py`)
    silently scatters users.json/apps.json/audit.log/tmp/etc. into whatever
    directory the process happened to start in — exactly the same class of
    bug APP_DATA_DIR's old cwd-relative default used to have (see below),
    just never fixed for these. An absolute value (default or override) is
    left untouched, so anyone who deliberately wants one of these somewhere
    else still can — e.g. a synced/backed-up folder for users.json."""
    p = Path(os.environ.get(env_var, default_name))
    return p if p.is_absolute() else (VOID_ROOT / p)

PORT        = int(os.environ.get("VOID_PORT",        "7777"))
OS_NAME     = os.environ.get("VOID_NAME",            "Void OS")
# The folder the Files tab shows. A brand-new install (no accounts yet) that wasn't told
# where gets its own ~/voidos-files, like the installer, and remembers that in .env so it
# can't change later; an existing install without the setting keeps using the home folder.
_fr_env = os.environ.get("VOID_FILE_ROOT", "").strip()
if not _fr_env:
    _fr_users = Path(os.environ.get("VOID_USERS_FILE") or (VOID_ROOT / "users.json"))
    if not _fr_users.exists():
        try:
            from modules import envfile as _envfile_fr
            _fr_env = str(Path.home() / "voidos-files")
            _envfile_fr.update(_APP_ENV, {"VOID_FILE_ROOT": _fr_env})
            os.environ["VOID_FILE_ROOT"] = _fr_env
        except OSError:
            _fr_env = ""
FILE_ROOT   = Path(_fr_env or str(Path.home())).resolve()
USERS_FILE  = _void_path("VOID_USERS_FILE", "users.json")
APPS_FILE   = _void_path("VOID_APPS_FILE",  "apps.json")
SESSIONS_FILE = _void_path("VOID_SESSIONS_FILE", "sessions.json")
SHARES_FILE = _void_path("VOID_SHARES_FILE", "shares.json")
STACKS_FILE = _void_path("VOID_APP_STACKS_FILE", "app_stacks.json")
# Bluetooth Intercom / Audio Gateway (FR_Bluetooth_Intercom_Audio_Gateway.md):
# paired-device metadata (friendly name, last-known profile capabilities),
# anchored the same VOID_ROOT-relative way as every other bookkeeping file
# above — never cwd-relative. See modules/intercom.py for everything else.
BLUETOOTH_FILE = _void_path("VOID_BLUETOOTH_FILE", "bluetooth_devices.json")
# Cloud Storage (Dropbox / Google Drive connect-and-browse — see
# modules/cloud_storage.py for the actual OAuth/API mechanics). Same flat-
# JSON-per-account-id shape as every other *_FILE store here; see that
# module's own SECURITY NOTE docstring for what "stored" means in practice.
CLOUD_TOKENS_FILE = _void_path("VOID_CLOUD_TOKENS_FILE", "cloud_tokens.json")
# Continuous cloud sync (see modules/cloud_sync.py) — sync-pair bookkeeping
# (tokens themselves live encrypted in CLOUD_TOKENS_FILE; rclone is handed
# them through its environment for each run — no rclone config file at rest).
CLOUD_SYNC_FILE = _void_path("VOID_CLOUD_SYNC_FILE", "cloud_sync.json")
# External Storage — mounted USB/SMB/NFS storage config (see
# modules/external_storage.py). Actual mounted content lives under
# FILE_ROOT/external/<name>, so it's automatically covered by the existing
# file browser, upload, WebDAV, etc. with no changes to any of that code —
# only this file tracks *which* mounts exist and how to (re)mount them.
EXTERNAL_STORAGE_FILE = _void_path("VOID_EXTERNAL_STORAGE_FILE", "external_storage.json")
# ── Folder layout inside FILE_ROOT ──────────────────────────────────────
#   Shared/          everyone's shared folder
#   Users/<name>/    each account's own "My Files"
#   Drives/<name>/   USB drives and network shares (see modules/external_storage.py)
# Older installs used home/shared, home/personal and external/. They are renamed once,
# at startup, if possible; if a rename can't be done safely (the new name is already in
# use, or a drive is busy) that one folder keeps its old name and everything follows
# these constants, so nothing else needs to know which layout is in use.
def _settle_layout_name(new: tuple, old: tuple) -> tuple:
    n, o = FILE_ROOT.joinpath(*new), FILE_ROOT.joinpath(*old)
    if not o.is_dir():
        return new
    try:
        if n.exists():
            if n.is_dir() and not any(n.iterdir()):
                n.rmdir()                     # an empty leftover: make room for the real one
            else:
                consolelog.warn(f"Both '{'/'.join(old)}' and '{'/'.join(new)}' exist in {FILE_ROOT}; "
                                f"keeping '{'/'.join(old)}' as it is.", "files")
                return old
        n.parent.mkdir(parents=True, exist_ok=True)
        os.rename(o, n)
        consolelog.say(f"Renamed {'/'.join(old)} to {'/'.join(new)} in {FILE_ROOT}", "files")
        return new
    except OSError as e:
        consolelog.warn(f"Couldn't rename '{'/'.join(old)}' to '{'/'.join(new)}' ({e}); keeping the old name.", "files")
        return old

_SHARED_PARTS = _settle_layout_name(("Shared",), ("home", "shared"))
_USERS_PARTS = _settle_layout_name(("Users",), ("home", "personal"))
_DRIVES_PARTS = _settle_layout_name(("Drives",), ("external",))
if not FILE_ROOT.joinpath(*_DRIVES_PARTS).exists() and not os.access(FILE_ROOT, os.W_OK | os.X_OK):
    # The top of the storage is read-only for this user (e.g. a drive mounted by root), so a new
    # folder can't be made there; use one inside the writable home folder instead of failing to start.
    _DRIVES_PARTS = ("home", "external")
    consolelog.warn(f"{FILE_ROOT} isn't writable by this user, so drives will be mounted under home/external. "
                    f"Fix it with: sudo chown -R $USER {FILE_ROOT}", "files")
try:                                          # the old parent folder, if nothing else is in it
    _old_home = FILE_ROOT / "home"
    if _old_home.is_dir() and not any(_old_home.iterdir()):
        _old_home.rmdir()
except OSError:
    pass
SHARED_PARTS, USERS_PARTS, DRIVES_PARTS = _SHARED_PARTS, _USERS_PARTS, _DRIVES_PARTS
SHARED_REL = "/".join(SHARED_PARTS)           # as used in URLs and stored paths
USERS_REL = "/".join(USERS_PARTS)
DRIVES_REL = "/".join(DRIVES_PARTS)
SHARED_DIR = FILE_ROOT.joinpath(*SHARED_PARTS)
USERS_DIR = FILE_ROOT.joinpath(*USERS_PARTS)
EXTERNAL_DIR = FILE_ROOT.joinpath(*DRIVES_PARTS)
LEGACY_LAYOUT_MAP = [(o, n) for o, n in (("home/shared", SHARED_REL), ("home/personal", USERS_REL), ("external", DRIVES_REL))
                     if o != n]               # old relative path -> new, for stored paths
# Custom-uploaded wallpaper images (see Settings → Appearance → Background).
# Presets (gradients) need no storage — only a user's own uploaded image does.
WALLPAPER_DIR = _void_path("VOID_WALLPAPER_DIR", "wallpapers")     # created by the first upload, not before
# MAC addresses arriving from request bodies get validated against this
# before ever reaching a bluetoothctl subprocess argument — same allowlist
# discipline as the file-icon extension regex elsewhere in this file.
_MAC_ROUTE_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\Z")
# Default is a sibling of FILE_ROOT/home (both live under the same Void OS
# root) rather than resolving relative to the process's cwd — the old
# relative default ("app-data") turned into <cwd>/app-data via .resolve(),
# which silently put App Store data (an Immich photo library, a Vaultwarden
# vault) wherever the process happened to be launched from, not visibly next
# to the `home` folder the Files UI shows. VOID_APP_DATA_DIR remains a full
# override for anyone who deliberately wants app data on a different
# disk/mount (e.g. a faster SSD for Immich while FILE_ROOT sits on slower
# bulk storage) — this only changes the *default*.
APP_DATA_DIR = Path(os.environ.get("VOID_APP_DATA_DIR", str(FILE_ROOT / "app-data"))).resolve()
# Deliberately outside FILE_ROOT/home — that's the user-facing file browser's
# scope (Shared + personal folders); installed-app data (an Immich photo
# library, a Vaultwarden vault file) is opaque infrastructure state, not
# something that belongs mixed into what Guests can browse. It's a sibling of
# `home` on disk, but stays out of the Files UI/`/api/files` scope entirely —
# container data directories aren't meant to be casually renamed/moved/
# deleted through a file browser.
_OLD_APP_DATA_DIR_DEFAULT = Path("app-data").resolve()
if ("VOID_APP_DATA_DIR" not in os.environ
        and _OLD_APP_DATA_DIR_DEFAULT != APP_DATA_DIR
        and _OLD_APP_DATA_DIR_DEFAULT.is_dir()
        and any(_OLD_APP_DATA_DIR_DEFAULT.iterdir())
        and not APP_DATA_DIR.exists()):
    # Existing installs with real data under the *old* resolved location —
    # never silently move a live Docker bind-mount out from under running
    # containers. app_stacks.json's compose_path is stored absolutely, so
    # already-running containers keep working either way; this only warns
    # that any *new* install after this change would land in a different
    # folder, splitting data across two roots.
    consolelog.blank()
    consolelog.warn(f"App data found at the old default location ({_OLD_APP_DATA_DIR_DEFAULT}), but the new "
                    f"default is {APP_DATA_DIR}. Nothing has been moved. Either set "
                    f"VOID_APP_DATA_DIR={_OLD_APP_DATA_DIR_DEFAULT} in your .env to keep using the old "
                    f"location, or move that folder to {APP_DATA_DIR} yourself.", "app-data")
    consolelog.blank()
CATALOG_DIR = VOID_ROOT / "app_catalog"
CATALOG_ZIP = VOID_ROOT / "app_catalog.zip"
_CATALOG_MARKER = CATALOG_DIR / ".extracted_from_zip"

# Flexible Host Bind Mount Support (FR): most official Docker Compose files
# for popular self-hosted apps (Immich, Jellyfin, Nextcloud, Frigate, ...)
# bind-mount arbitrary host paths (external drives, /etc/localtime, NAS
# mounts) that live outside any one app's own data folder. Historically
# _scan_compose_risks() flagged every such mount as "risky", forcing a
# confirm-and-resubmit round trip even for completely benign, upstream-
# documented volumes. Default is now "unrestricted": arbitrary absolute host
# paths are allowed with no extra confirmation step, and only genuinely
# dangerous host paths (see _PROTECTED_HOST_PATHS below) still surface as a
# warning. Admins who prefer the old, stricter behavior can set
# VOID_BIND_MOUNT_MODE=restricted to go back to requiring every out-of-
# data-folder mount to be explicitly confirmed.
BIND_MOUNT_MODE = os.environ.get("VOID_BIND_MOUNT_MODE", "unrestricted").strip().lower()
if BIND_MOUNT_MODE not in ("unrestricted", "restricted"):
    BIND_MOUNT_MODE = "unrestricted"
# Mounting any of these directly (as the bind source, not a subpath under
# them) risks the host OS itself, regardless of bind-mount mode.
_PROTECTED_HOST_PATHS = {"/", "/boot", "/proc", "/sys", "/dev", "/run"}
if sys.platform == "win32":
    _sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
    _PROTECTED_HOST_PATHS |= {_sysdrive, os.environ.get("SystemRoot", _sysdrive + "Windows"),
                              os.environ.get("ProgramFiles", _sysdrive + "Program Files"),
                              os.environ.get("ProgramData", _sysdrive + "ProgramData")}

# Per-extension file icons (Files tab). Real icon artwork
# for 800+ extensions instead of one generic gear for everything outside
# the six broad _ft() categories — see /api/file-icon/<ext> below for the
# actual serving logic. Icon set: vscode-icons (github.com/vscode-icons/
# vscode-icons). NOTE — unlike this project's own code, these icon *assets*
# are CC BY-SA 4.0 (share-alike), not MIT; some individual icons are also
# separately trademarked/branded assets under their own owners' terms on
# top of that. See static/file-icons/NOTICE.md for the full attribution
# this requires. Two tiers:
#   1. STATIC_ICON_DIR — bundled at build time via static/file-icons/<ext>.svg.
#      This is the common case: checked first, never touches the network.
#   2. ICON_CACHE_DIR — a live-fetch-and-cache fallback for the rare
#      extension genuinely outside that bundled set. Deliberately NOT
#      fetched directly by the browser (see the route's own comment) —
#      only the server ever talks to the external icon host, once per
#      never-before-seen extension, ever. Safe to delete by hand at any
#      time: recreated on demand, and every consumer already falls back
#      gracefully (404 -> frontend onerror -> the old generic emoji).
STATIC_ICON_DIR = VOID_ROOT / "static" / "file-icons"
# Void's own cache, not user storage — anchored at VOID_ROOT like the rest of
# Void's bookkeeping data above, NOT inside FILE_ROOT. FILE_ROOT can point
# anywhere (an external drive, a NAS mount); this shouldn't end up nested
# inside whatever that happens to be.
ICON_CACHE_DIR = VOID_ROOT / "icon-cache"
_ICON_EXT_RE = re.compile(r"^[a-z0-9]{1,12}$")  # allowlist: ext becomes part of both a
                                                  # filesystem path and an outbound URL below
def _load_icon_ext_map() -> dict:
    """ext -> vscode-icons icon name (without the 'file_type_' filename prefix),
    for the live-fetch fallback's URL only (the bundled set above was already
    resolved through this same mapping at build time, so this only matters for
    extensions that snapshot didn't have). Missing/unparseable file just means
    the fallback degrades to guessing icon name == ext, same as an extension
    with no entry in the map."""
    try:
        return json.loads((VOID_ROOT / "static" / "file-icon-ext-map.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
_ICON_EXT_MAP = _load_icon_ext_map()

CATALOG_UPDATED_ZIP = VOID_ROOT / "app_catalog_updated.zip"   # written by "Update app list"; never part of a program update

def _ensure_catalog_extracted():
    """The App Store catalog ships as ONE file, app_catalog.zip. Apps can also be refreshed without
    a program update (modules/catalog_update.py), which saves app_catalog_updated.zip beside it.
    Whichever of the two has the higher catalog version is the one in use (the bundled one wins a
    tie, so a program update that brings a newer list always takes over). It is extracted to
    app_catalog/, a disposable cache: whenever the chosen zip's contents change the folder is
    deleted and extracted fresh, so apps removed from the catalog really disappear instead of
    lingering. Unchanged zip -> nothing happens (compared by SHA-256, recorded in the marker file;
    timestamps aren't trusted). A missing zip just means an empty App Store, never a crash."""
    import hashlib
    from modules import catalog_pack
    zips = [z for z in (CATALOG_ZIP, CATALOG_UPDATED_ZIP) if z.exists()]
    if not zips:
        return
    use = max(zips, key=lambda z: (catalog_pack.zip_version(z), z == CATALOG_ZIP))
    try:
        digest = hashlib.sha256(use.read_bytes()).hexdigest()
        if _CATALOG_MARKER.exists() and _CATALOG_MARKER.read_text(encoding="utf-8").strip() == digest:
            return
        if CATALOG_DIR.exists():
            shutil.rmtree(CATALOG_DIR)
        with zipfile.ZipFile(use) as zf:
            root = CATALOG_DIR.parent.resolve()
            for member in zf.namelist():          # refuse any entry that would land outside app_catalog/
                target = (root / member).resolve()
                if not str(target).startswith(str(CATALOG_DIR.resolve())):
                    raise ValueError(f"unexpected path in catalog zip: {member}")
            zf.extractall(root)
        _CATALOG_MARKER.write_text(digest, encoding="utf-8")
        consolelog.say(f"Extracted the App Store catalog from {use.name}", "app-store")
    except Exception as e:
        consolelog.warn(f"Could not extract the App Store catalog from {use.name}: {e}", "app-store")

_ensure_catalog_extracted()
AUDIT_FILE = _void_path("VOID_AUDIT_FILE", "audit.log")
ERROR_LOG_FILE = _void_path("VOID_ERROR_LOG_FILE", "error_log.jsonl")
AUDIT_MAX_ENTRIES = int(os.environ.get("VOID_AUDIT_MAX_ENTRIES", "5000"))
# Single shared constant for every PIN in the app (share links, app-gate) — was two different
# ranges (4-8 for shares, 6-12 for gates) before this was standardized; both now require exactly
# this many digits. Applies to *new/edited* PINs only — see _validate_gate_fields and
# api_shares_create for the backward-compat note on already-issued PINs under the old ranges,
# whose stored hash/pin_length simply keep validating as they always have.
PIN_LEN = 6
GUEST_QUOTA_MB = int(os.environ.get("VOID_GUEST_QUOTA_MB", "0"))  # 0 = unlimited
SESSION_HRS = int(os.environ.get("VOID_SESSION_HOURS",    "24"))
COOKIE_SEC  = os.environ.get("VOID_COOKIE_SECURE",   "false").lower() == "true"
MAX_ATTS    = int(os.environ.get("VOID_MAX_ATTEMPTS",    "5"))
LOCKOUT_S   = int(os.environ.get("VOID_LOCKOUT_SECONDS", "60"))
MAX_UPLOAD  = int(os.environ.get("VOID_MAX_UPLOAD_MB",   "500"))
# Deliberately below any sane MAX_CONTENT_LENGTH (which still applies to every
# request, including /api/file/upload/chunk) — a single chunk always has to
# fit inside that cap, so this stays small regardless of how large the whole
# file ends up being. 24h TTL matches the existing zip-job janitor's spirit:
# generous enough to survive an overnight-paused transfer, not so long that
# an abandoned upload lingers on disk indefinitely.
UPLOAD_SESSION_TTL_S = int(os.environ.get("VOID_UPLOAD_SESSION_TTL_HOURS", "24")) * 3600
# One shared temp folder for every kind of "server-generated file that isn't
# a real upload/download yet" — in-progress upload .part files and zip-build
# temp files both live under here (in their own subfolders), instead of
# each picking a different location. Deliberately kept inside the project
# rather than falling through to the OS temp dir: on Windows, the system
# temp folder is one of the paths real-time antivirus scans hardest, and a
# zip actively being written there while a scan holds its handle can stall
# the writer thread — which, since the writer only releases its semaphore
# slot in a `finally` that runs after the write completes, can hold that
# slot (and add general disk contention) far longer than intended. One
# documented folder to exclude from AV scanning covers all of it.
TMP_DIR = _void_path("VOID_TMP_DIR", "tmp")
TMP_DIR.mkdir(parents=True, exist_ok=True)
# Every temporary file Python itself creates goes here too, not the OS temp
# folder. This matters more than it looks: Werkzeug spools any uploaded file
# over 500 KB into a tempfile *before* Void OS's own code sees it, so on
# Windows every regular upload and every chunk of a large upload used to
# pass through %TEMP% — where Windows Defender scans and throttles writes,
# which is the very slowdown TMP_DIR was introduced to avoid.
tempfile.tempdir = str(TMP_DIR)
external_storage.SCRATCH_DIR = TMP_DIR
cloud_sync.SCRATCH_DIR = TMP_DIR
# VOID_UPLOAD_TMP_DIR is kept as a backward-compatible override for just the
# uploads subfolder (pre-dates VOID_TMP_DIR) — anyone who already set it
# keeps working unchanged; everyone else gets the new consolidated layout
# for free. If you're upgrading and had in-flight uploads sitting in an old
# standalone upload-tmp/ folder, move its contents into the new location
# (or just let them finish/expire there and delete the old folder after).
# Same VOID_ROOT-anchoring as everything else above: unset -> lives under
# TMP_DIR as before; a relative override resolves against VOID_ROOT, not
# the process's cwd.
UPLOAD_TMP_DIR = _void_path("VOID_UPLOAD_TMP_DIR", str(TMP_DIR / "uploads"))
ZIP_TMP_DIR = TMP_DIR / "zips"
# Both deliberately outside FILE_ROOT — a half-finished upload's .part file
# or an in-progress zip build must never be listable/reachable through the
# file browser, same reasoning as APP_DATA_DIR being kept outside FILE_ROOT
# above.
UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
ZIP_TMP_DIR.mkdir(parents=True, exist_ok=True)
TERM_EN     = os.environ.get("VOID_TERMINAL_ENABLED", "true").lower() == "true"
# Concurrency caps for /terminal — see the DoS analysis in ws_terminal()
# below for why these exist. Gunicorn's thread count (README.md's
# Performance & limits section of README.md) is the real ceiling these are sized against;
# the global cap is deliberately well under it so ordinary HTTP requests
# always have threads free even if every terminal slot is in use.
MAX_TERMINAL_SESSIONS_PER_USER = int(os.environ.get("VOID_MAX_TERMINAL_SESSIONS_PER_USER", "6"))
MAX_TERMINAL_SESSIONS_GLOBAL   = int(os.environ.get("VOID_MAX_TERMINAL_SESSIONS_GLOBAL",   "16"))

def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """An integer setting from .env; a missing, blank or non-numeric value
    falls back to the default instead of stopping the server from starting."""
    try:
        return max(minimum, int(str(os.environ.get(name, "")).strip() or default))
    except ValueError:
        return default

# Fair-use limits: stop one account (or one address) from using up what everyone
# shares. All adjustable in .env; 0 turns a limit off where noted in .env.example.
# Threads that serve requests (Linux). Every open browser tab and every slow
# download holds one for as long as it lasts, so the caps below are sized against it.
SERVER_THREADS                 = _env_int("VOID_THREADS", 32, 8)
MAX_CONTROL_SOCKETS_PER_USER   = _env_int("VOID_MAX_CONTROL_SOCKETS_PER_USER", 5, 1)   # open browser tabs per account
MAX_CONTROL_SOCKETS_GLOBAL     = _env_int("VOID_MAX_CONTROL_SOCKETS_GLOBAL", 16, 1)
MAX_DOWNLOADS_PER_USER         = _env_int("VOID_MAX_DOWNLOADS_PER_USER", 6, 1)         # simultaneous downloads / media streams
MAX_SHARE_DOWNLOADS_PER_IP     = _env_int("VOID_MAX_SHARE_DOWNLOADS_PER_IP", 2, 1)     # public share links, per visitor address
MAX_DOWNLOADS_GLOBAL           = _env_int("VOID_MAX_DOWNLOADS_GLOBAL", 10, 1)
MAX_FILE_JOBS_PER_USER         = _env_int("VOID_MAX_FILE_JOBS_PER_USER", 5, 1)         # queued + running copy/move/delete jobs
MIN_FREE_MB                    = _env_int("VOID_MIN_FREE_MB", 1024, 0)                 # uploads and copies stop before the drive gets this full
SHARED_QUOTA_MB                = _env_int("VOID_SHARED_QUOTA_MB", 0, 0)                # Guests can't push the Shared folder past this (0 = no cap)
WEBDAV_EN   = os.environ.get("VOID_WEBDAV_ENABLED",   "true").lower() == "true"
CF_TUNNEL_EN= os.environ.get("VOID_CLOUDFLARE_TUNNEL", "false").lower() == "true"
CF_KV_NAMESPACE  = os.environ.get("CF_KV_NAMESPACE", "")
CF_WORKER_URL    = os.environ.get("CF_WORKER_URL", "")
if CF_WORKER_URL and not CF_WORKER_URL.startswith(("http://", "https://")):
    CF_WORKER_URL = f"https://{CF_WORKER_URL}"
CF_RESTART_DELAY = int(os.environ.get("CF_RESTART_DELAY", "5"))

_RAW_SECRET = _ENV_SECRET_KEY  # never empty here: the first-run block above generated one, or exited
SECRET_KEY = _RAW_SECRET
if not SECRET_KEY:
    raise SystemExit("Void OS: VOID_SECRET_KEY is empty, so it can't start safely. Add a long random value to .env.")


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global sock
    from modules.web import sock
