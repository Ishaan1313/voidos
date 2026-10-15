"""
modules/cloud_sync.py — continuous, bidirectional cloud sync for Void OS,
built on rclone, the standard tool for this. This module shells out to the
`rclone` binary on PATH, the same "host CLI tool, not a pip package" pattern
modules/intercom.py already uses for bluetoothctl/pw-record.

Why rclone and not a hand-rolled sync loop: two-way sync is a genuinely
hard problem (renames, deletes, conflicting edits on both sides) that
rclone's `bisync` command already solves correctly, with its own state
tracking and conflict handling — reimplementing that with plain
`rclone copy`/`sync` calls would either be one-directional (rclone sync
*deletes* destination files not present in source, which is exactly
wrong for a two-way "keep folders in sync" feature) or would silently
reinvent bisync's own safety logic worse. This module is a thin,
supervised wrapper around it, not a replacement for it.

Each connected cloud account can have one or more "sync pairs": a local
Void OS folder kept continuously in sync with a folder on the remote.
voidos.py owns persistence/permissions; this module owns the actual
rclone process lifecycle (the first --resync run, the repeating interval
loop, and reporting status back).

Login tokens never sit in an rclone config file: each run gets its remote
definition through environment variables on the rclone child process only
(RCLONE_CONFIG_<REMOTE>_<OPTION>), plus a throwaway --config path that is
deleted when the run ends (so rclone can't fall back to, or write into, the
user's own rclone.conf).
"""
from __future__ import annotations
import os
import re
import json
import uuid
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from modules.winplat import no_window

# rclone writes UTF-8 on every OS; without an explicit encoding, text=True
# decodes with the Windows ANSI code page (cp1252) and any non-ASCII file
# name in its log output crashes the reader thread with UnicodeDecodeError.
_TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}

SCRATCH_DIR = None  # set by voidos.py to the app's own tmp/ folder

def _rclone_env() -> dict:
    """Base environment for rclone. Its own temporary files go under the app's
    tmp/ folder instead of the OS temp folder (see TMP_DIR in modules/config.py).
    RCLONE_TEMP_DIR is read by rclone >= 1.58 and harmlessly ignored by older
    versions."""
    env = os.environ.copy()
    if SCRATCH_DIR:
        d = os.path.join(str(SCRATCH_DIR), "rclone")
        os.makedirs(d, exist_ok=True)
        env.update(RCLONE_TEMP_DIR=d, TMPDIR=d, TEMP=d, TMP=d)
    return env

RCLONE_TYPE = {"dropbox": "dropbox", "gdrive": "drive"}


def check_rclone() -> tuple[bool, str]:
    """(ok, message) — same shape as intercom.py's own host-capability checks,
    so the frontend can show 'here's exactly what to install' instead of a
    generic failure."""
    path = shutil.which("rclone")
    if not path:
        if sys.platform == "win32":
            return False, ("rclone isn't installed — continuous cloud sync needs it on PATH. "
                            "Install with: winget install Rclone.Rclone (then restart Void OS so "
                            "the new PATH is picked up) — see https://rclone.org/downloads/")
        return False, ("rclone isn't installed — continuous cloud sync needs it on PATH. "
                        "Install with: curl https://rclone.org/install.sh | sudo bash "
                        "(or your package manager's rclone) — see https://rclone.org/downloads/")
    try:
        r = subprocess.run([path, "version"], capture_output=True, timeout=10, **_TEXT, **no_window())
        first_line = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else "unknown version"
        return True, first_line
    except Exception as e:
        return False, f"rclone found but failed to run: {e}"


def _rfc3339(expires_at: float) -> str:
    return datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def remote_env(remote_name: str, provider: str, client_id: str, client_secret: str,
               token_record: dict) -> dict:
    """The environment variables that define one rclone remote, using the same
    OAuth tokens Void OS's own cloud_storage.py already obtained — one Connect
    flow powers both the browse/import UI and this sync engine. Nothing is
    written to disk. rclone refreshes the access token itself during a run
    (it has client_id/secret + refresh_token); the next run starts again from
    Void OS's own (encrypted) token record."""
    prefix = "RCLONE_CONFIG_" + re.sub(r"[^A-Za-z0-9]", "_", remote_name).upper() + "_"
    env = {
        prefix + "TYPE": RCLONE_TYPE[provider],
        prefix + "CLIENT_ID": client_id,
        prefix + "CLIENT_SECRET": client_secret,
        prefix + "TOKEN": json.dumps({
            "access_token": token_record.get("access_token", ""),
            "token_type": "bearer",
            "refresh_token": token_record.get("refresh_token", ""),
            "expiry": _rfc3339(token_record.get("expires_at", time.time())),
        }),
    }
    if provider == "gdrive":
        env[prefix + "SCOPE"] = "drive"
    return env


def rclone_version() -> tuple:
    """(major, minor) of the installed rclone, or (0, 0)."""
    ok, line = check_rclone()
    m = re.search(r"v?(\d+)\.(\d+)", line or "") if ok else None
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def run_bisync(remote_name: str, remote_vars: dict, remote_path: str, local_path: Path,
                resync: bool = False, timeout_s: int = 0, provider: str = "") -> tuple[bool, str]:
    if not timeout_s:                  # the first full download of an account may take hours
        timeout_s = 12 * 3600 if resync else 1800
    local_path.mkdir(parents=True, exist_ok=True)
    # A path that rclone may create/write during the run (it saves refreshed
    # tokens there) — deleted afterwards so no token is ever left at rest.
    scratch = os.path.join(str(SCRATCH_DIR or os.path.dirname(os.path.abspath(__file__))), "rclone")
    os.makedirs(scratch, exist_ok=True)
    conf_path = Path(scratch) / f"run-{uuid.uuid4().hex}.conf"
    remote_spec = f"{remote_name}:{remote_path}".rstrip(":") if remote_path else f"{remote_name}:"
    cmd = [shutil.which("rclone") or "rclone", "bisync", remote_spec, str(local_path),
           "--config", str(conf_path),
           "--conflict-resolve", "newer",
           "--conflict-loser", "num",  # keep both sides of a genuine conflict rather than silently dropping one
           # a whole account is a lot of listing; the first download gets more parallel transfers
           # (Dropbox's pacer slows rclone down by itself if it is asked to go too fast)
           "--fast-list", "--checkers", "16" if resync else "8", "--transfers", "8" if resync else "4",
           "-v"]
    if provider == "dropbox":
        cmd += ["--dropbox-batch-mode", "sync"]       # many small files go up in batches
    if rclone_version() >= (1, 66):
        cmd += ["--resilient", "--recover"]           # carry on by itself after an interrupted run
    if resync:
        cmd.append("--resync")
    try:
        env = _rclone_env()
        env.update(remote_vars)
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s, env=env, **_TEXT, **no_window())
        ok = r.returncode == 0
        tail = "\n".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-40:])  # last 40 lines is enough to diagnose
        return ok, tail
    except subprocess.TimeoutExpired:
        return False, f"Sync timed out after {timeout_s}s — a very large first sync can need a longer window " \
                       f"(this pair's next attempt will resume, since bisync is resumable by design)."
    except Exception as e:
        return False, str(e)
    finally:
        try:
            conf_path.unlink()
        except OSError:
            pass


# ── Dropbox change notifications ───────────────────────────────────────
# Dropbox can tell us the moment anything in the account changes: a "cursor" marks a point in
# time and a long-poll request returns as soon as something is different (or after ~90 s).
# That is how the desktop app feels instant; here it just tells the sync loop to run now.
import urllib.request as _urlreq


def _dbx_post(url: str, payload: dict, token: str = "", timeout: int = 30) -> dict:
    if payload is None:                      # a few Dropbox calls take no body at all
        req = _urlreq.Request(url, method="POST")
    else:
        req = _urlreq.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def dbx_latest_cursor(token: str) -> str:
    d = _dbx_post("https://api.dropboxapi.com/2/files/list_folder/get_latest_cursor",
                  {"path": "", "recursive": True, "include_deleted": True}, token)
    return d["cursor"]


def dbx_longpoll(cursor: str, timeout: int = 90) -> dict:
    return _dbx_post("https://notify.dropboxapi.com/2/files/list_folder/longpoll",
                     {"cursor": cursor, "timeout": timeout}, "", timeout + 20)


def dbx_space(token: str) -> dict:
    """{used, allocated} in bytes for the connected account."""
    d = _dbx_post("https://api.dropboxapi.com/2/users/get_space_usage", None, token)
    alloc = d.get("allocation", {}) or {}
    total = (alloc.get("allocated") or (alloc.get("individual") or {}).get("allocated") or 0)
    return {"used": int(d.get("used", 0)), "allocated": int(total)}


class SyncSupervisor:
    """One background thread per sync pair — mirrors voidos.py's existing
    tunnel-supervisor pattern (self-healing loop + a stop event) rather than
    introducing a new concurrency style into the app. Owns nothing about
    *why* a pair exists (that's voidos.py's CLOUD_SYNC_FILE); only runs it.

    Plain pairs sync on a timer. A *live* pair also syncs as soon as something changes: a file
    watcher on the local folder and (Dropbox) a change notification from the account both
    "poke" the loop, which waits for a quiet moment so a burst of changes becomes one sync.
    The timer stays as a safety net."""
    QUIET_S = 3          # no new change for this long = the burst is over
    MAX_WAIT_S = 30      # ...but never hold a sync back longer than this
    MIN_GAP_S = 8        # at least this long between two runs

    def __init__(self, pair_id: str, get_remote, remote_name: str, remote_path: str,
                 local_path: Path, interval_s: int, first_resync_done: bool,
                 on_result, on_resync_done, live: bool = False, provider: str = "",
                 get_access_token=None, on_state=None):
        self.pair_id = pair_id
        self._get_remote = get_remote        # () -> (env_vars | None, error_message | None), called before every run
        self._remote_name = remote_name
        self._remote_path = remote_path
        self._local_path = local_path
        self._interval_s = max(60, interval_s)  # 60s floor — this hits a real API and a real disk, not free
        self._resync_needed = not first_resync_done
        self._on_result = on_result          # callback(pair_id, ok, message, timestamp)
        self._on_resync_done = on_resync_done  # callback(pair_id) — first successful --resync completed
        self._on_state = on_state            # callback(pair_id, "syncing" | "idle")
        self._live = live
        self._provider = provider
        self._get_token = get_access_token   # () -> a fresh Dropbox access token
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_event = 0.0
        self._observer = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"cloudsync-{pair_id}")

    def start(self):
        self._thread.start()
        if self._live:
            self._start_local_watcher()
            if self._provider == "dropbox" and self._get_token:
                threading.Thread(target=self._remote_watch, daemon=True, name=f"cloudsync-watch-{self.pair_id}").start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        obs, self._observer = self._observer, None
        if obs is not None:
            try:
                obs.stop()
            except Exception:
                pass

    @property
    def watching_local(self) -> bool:
        return self._observer is not None

    def poke(self):
        """Something changed: sync soon."""
        self._last_event = time.time()
        self._wake.set()

    # ---- change sources
    def _start_local_watcher(self):
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            return                           # no watcher available: the timer still syncs
        sup = self

        class _H(FileSystemEventHandler):
            def on_any_event(self, event):
                p = getattr(event, "src_path", "") or ""
                if p.endswith(".partial") or "/.rclone" in p.replace("\\", "/"):
                    return                   # rclone's own half-written files
                sup.poke()
        try:
            self._local_path.mkdir(parents=True, exist_ok=True)
            obs = Observer()
            obs.schedule(_H(), str(self._local_path), recursive=True)
            obs.daemon = True
            obs.start()
            self._observer = obs
        except Exception:
            self._observer = None            # e.g. out of inotify watches: fall back to the timer

    def _remote_watch(self):
        cursor = None
        while not self._stop.is_set():
            try:
                if cursor is None:
                    cursor = dbx_latest_cursor(self._get_token())
                res = dbx_longpoll(cursor, 90)
                if self._stop.is_set():
                    return
                if res.get("changes"):
                    cursor = dbx_latest_cursor(self._get_token())    # move past it BEFORE syncing
                    self.poke()
                backoff = res.get("backoff")
                if backoff:
                    self._stop.wait(float(backoff))
            except Exception:
                cursor = None
                self._stop.wait(30)          # network or token trouble: try again shortly

    # ---- the loop
    def _state(self, state: str):
        if self._on_state:
            try:
                self._on_state(self.pair_id, state)
            except Exception:
                pass

    def _run_once(self):
        self._state("syncing")
        try:
            remote_vars, problem = self._get_remote()
        except Exception as e:
            remote_vars, problem = None, f"Couldn't prepare the cloud login: {e}"
        if remote_vars is None:
            ok, msg = False, problem or "Couldn't prepare the cloud login."
        else:
            ok, msg = run_bisync(self._remote_name, remote_vars, self._remote_path,
                                  self._local_path, resync=self._resync_needed, provider=self._provider)
        if ok and self._resync_needed:
            self._resync_needed = False
            try:
                self._on_resync_done(self.pair_id)
            except Exception:
                pass
        try:
            self._on_result(self.pair_id, ok, msg, time.time())
        except Exception:
            pass
        self._state("idle")

    def _loop(self):
        last_end = 0.0
        while not self._stop.is_set():
            self._wake.clear()
            self._run_once()
            last_end = time.time()
            # wait for the next reason to run
            deadline = last_end + self._interval_s
            while not self._stop.is_set():
                left = deadline - time.time()
                if left <= 0:
                    break
                if self._wake.wait(min(left, 5)):
                    # a change: let the burst finish, then go (never hold it back for long)
                    began = time.time()
                    while (not self._stop.is_set() and time.time() - self._last_event < self.QUIET_S
                           and time.time() - began < self.MAX_WAIT_S):
                        self._stop.wait(0.5)
                    gap = self.MIN_GAP_S - (time.time() - last_end)
                    if gap > 0:
                        self._stop.wait(gap)
                    break
