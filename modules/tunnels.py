"""
modules/tunnels.py — Cloudflare tunnels (main + per app), Access Gate supervisor, Workers KV sync.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from collections import deque
from flask import jsonify
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import warnings
from modules import consolelog, domains, state, winplat
from modules.config import (
    CF_KV_NAMESPACE, CF_RESTART_DELAY, CF_TUNNEL_EN, CF_WORKER_URL, PORT, SECRET_KEY, VOID_ROOT,
    _void_path,
)
from modules.file_helpers import _aload
from modules.trash_zip_routes import _zip_cleanup_stale
from modules.web import app
from modules.decorators import require_admin


# ══════════════════════════════════════════════════════════════════════
# CLOUDFLARE QUICK TUNNEL — optional, opt-in via VOID_CLOUDFLARE_TUNNEL=true
# Spawns `cloudflared tunnel --url http://localhost:PORT` and parses the
# ephemeral *.trycloudflare.com URL from its log output. No account or
# config file required — this is Cloudflare's free anonymous "Quick Tunnel".
#
# Self-healing: if cloudflared crashes or the tunnel drops, it is
# automatically restarted after CF_RESTART_DELAY seconds, indefinitely,
# for as long as VOID_CLOUDFLARE_TUNNEL=true.
#
# Workers KV sync: whenever a NEW tunnel URL is obtained (fresh start or
# reconnect after a drop), it is automatically pushed to a Cloudflare
# Workers KV namespace via `wrangler kv key put current "<url>" --namespace-id ...`.
# A Cloudflare Worker (CF_WORKER_URL) can then read that "current" key and
# proxy/redirect to whatever the live ephemeral tunnel happens to be —
# giving you one permanent, stable URL to bookmark even though the
# underlying Quick Tunnel address changes on every restart.
# ══════════════════════════════════════════════════════════════════════
state.TUNNEL_URL    = None        # the live https://xxxx.trycloudflare.com address, once known
state.TUNNEL_STATUS = "disabled"  # disabled | not_installed | starting | connected | reconnecting | error
state.TUNNEL_PROC   = None
_TUNNEL_URL_RE = re.compile(r"https://(?!api\.)[a-zA-Z0-9\-]+\.trycloudflare\.com")
# (The negative lookahead matters: when cloudflared can't register a tunnel it logs
# `Post "https://api.trycloudflare.com/tunnel": ...`. Without it that API address was
# taken for the live tunnel - shown as "connected" and even pushed to Workers KV.)
# Rolling tail of cloudflared's own raw output — every line it prints, not
# just the one containing the connect URL. Before this, a cloudflared that
# never printed a matching URL line was a total black box: the read loop
# silently discarded everything that wasn't the URL, so an admin watching
# the tunnel sit at "starting" (even through repeated watchdog-triggered
# restarts) had no way to see what cloudflared itself was actually saying —
# an auth/token error, a blocked outbound connection, a DNS failure, or
# just a slow handshake. Exposed via /api/admin/tunnel so it's visible from
# the Settings UI instead of only the server's own console.
_TUNNEL_LOG_LOCK = threading.Lock()
_TUNNEL_LOG_TAIL = deque(maxlen=40)

def _tunnel_log_line(line: str):
    line = line.rstrip("\n")
    if not line.strip():
        return
    with _TUNNEL_LOG_LOCK:
        _TUNNEL_LOG_TAIL.append(line)

def _tunnel_log_snapshot() -> list:
    with _TUNNEL_LOG_LOCK:
        return list(_TUNNEL_LOG_TAIL)

state.TUNNEL_PROBE  = None        # {"ok": bool, "at": epoch, "error": str} - last check that the public link answers

state.KV_SYNC_STATUS      = "not_configured"  # not_configured | idle | syncing | synced | error
state.KV_SYNC_ERROR       = ""
state.KV_LAST_SYNCED_URL  = None
state.KV_LAST_SYNCED_AT   = None

def _tunnel_print(msg: str):
    """Every '[tunnel] ...' status line: plain, indented, flushed (see modules/consolelog.py)."""
    consolelog.say(msg, "tunnel")

def _kv_print(key: str, msg: str):
    """Same, for '[kv-sync:<key>] ...' lines."""
    consolelog.say(msg, f"kv-sync:{key}")

def _sync_cloudflare_kv(url: str, key: str = "current") -> bool:
    """Push a tunnel URL into Cloudflare Workers KV via `npx wrangler`, under
    the given key. Runs in its own thread so it never blocks the tunnel
    log-watcher. Prints its own result for the same reason _tunnel_supervisor
    does: this runs asynchronously, well after any startup banner has already
    printed and exited, so it's the only place that can report the
    real outcome.

    `key` defaults to "current" (the main dashboard tunnel, unchanged from
    before). Per-app tunnels (see APP_TUNNELS below) each pass their own key
    (f"app_{app_id}") so multiple tunnels can share one KV namespace without
    clobbering each other — each app gets its own Worker deployment reading
    its own key, all bound to the same namespace.

    Uses `npx wrangler` rather than a bare `wrangler` binary so this works
    whether or not wrangler happens to be globally installed/on PATH — npx
    resolves a project-local install, a cached one, or transparently
    downloads it on first run. Only Node.js/npm need to be present."""
    pass  # (shared value now lives in modules/state.py)
    is_main = (key == "current")
    if not CF_KV_NAMESPACE:
        if is_main: state.KV_SYNC_STATUS = "not_configured"
        return False
    npx = shutil.which("npx")
    if not npx:
        if is_main:
            state.KV_SYNC_STATUS = "error"
            state.KV_SYNC_ERROR = "npx not found on PATH — install Node.js/npm"
        _kv_print(key, "npx not found on PATH — install Node.js/npm")
        return False
    if is_main: state.KV_SYNC_STATUS = "syncing"
    try:
        result = subprocess.run(
            [npx, "wrangler", "kv", "key", "put", key, url,
             "--namespace-id", CF_KV_NAMESPACE, "--remote"],
            # --remote is required: without it, `wrangler kv key put` writes to a
            # local emulated cache under .wrangler/state/ instead of the actual
            # live Cloudflare namespace. The command still exits 0 and prints a
            # "Writing the value..." success message either way, so this failure
            # mode is silent — nothing shows up in the Cloudflare dashboard, and
            # nothing here would have caught it without the --remote flag.
            capture_output=True, text=True, timeout=60,
            # 60s not 30s: if wrangler isn't cached yet, npx downloads it on this
            # first run, which can take a while on something like a Pi's connection.
            encoding="utf-8", errors="replace", **winplat.no_window(),
            # wrangler (a Node CLI) always writes UTF-8. Without an explicit
            # encoding, text=True falls back to the OS locale's default codec —
            # cp1252 on Windows — which chokes on bytes like 0x8f and crashes
            # subprocess.run's internal _readerthread with a UnicodeDecodeError.
            # Same root cause as the cloudflared fix above.
        )
        if result.returncode == 0:
            if is_main:
                state.KV_SYNC_STATUS = "synced"; state.KV_SYNC_ERROR = ""
                state.KV_LAST_SYNCED_URL = url; state.KV_LAST_SYNCED_AT = time.time()
            _kv_print(key, f"Synced {key} -> {url}")
            return True
        err = (result.stderr or result.stdout or "wrangler exited non-zero")[:300]
        if is_main: state.KV_SYNC_STATUS = "error"; state.KV_SYNC_ERROR = err
        _kv_print(key, f"Failed: {err}")
        return False
    except subprocess.TimeoutExpired:
        if is_main: state.KV_SYNC_STATUS = "error"; state.KV_SYNC_ERROR = "npx wrangler timed out after 60s"
        _kv_print(key, "npx wrangler timed out after 60s")
        return False
    except Exception as e:
        if is_main: state.KV_SYNC_STATUS = "error"; state.KV_SYNC_ERROR = str(e)[:300]
        _kv_print(key, str(e))
        return False

def _tunnel_supervisor():
    """Runs for the lifetime of the process in a daemon thread. Starts
    cloudflared, watches its output for the tunnel URL, syncs that URL to
    Workers KV, and — if the process ever exits — waits CF_RESTART_DELAY
    seconds and starts it again. Loops forever while CF_TUNNEL_EN is true.

    Prints its own status transitions to the console as they happen, since
    this thread is the only place that actually knows when the tunnel
    connects, drops, or fails — the startup banner in __main__ runs and
    exits in milliseconds, long before cloudflared has even been spawned,
    let alone connected, so it can never report a real status itself."""
    pass  # (shared value now lives in modules/state.py)
    binary = shutil.which("cloudflared")
    if not binary:
        state.TUNNEL_STATUS = "not_installed"
        _tunnel_print("cloudflared not found on PATH — Cloudflare Tunnel will stay disabled.")
        return
    first_attempt = True
    while True:
        state.TUNNEL_URL = None
        state.TUNNEL_STATUS = "starting"
        if first_attempt:
            _tunnel_print("Starting cloudflared in the background — this can take a few seconds...")
        else:
            _tunnel_log_line("── restarting cloudflared ──")
        try:
            state.TUNNEL_PROC = subprocess.Popen(
                [binary, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, universal_newlines=True,
                encoding="utf-8", errors="replace", **winplat.no_window(),
                # cloudflared always writes UTF-8 (its startup banner uses box-drawing
                # characters like ╭╮╯╰). Without an explicit encoding, Python falls back
                # to the OS locale's default codec — cp1252 on Windows — which can't
                # decode those bytes and crashes the subprocess reader thread with a
                # UnicodeDecodeError. errors="replace" is a second safety net: even if
                # some other unexpected byte sequence shows up, we substitute the
                # replacement character instead of crashing the tunnel thread.
            )
        except Exception as e:
            state.TUNNEL_STATUS = "error"
            _tunnel_print(f"Could not start cloudflared: {e}")
            warnings.warn(f"[Void OS] Could not start cloudflared: {e}")
            time.sleep(max(1, CF_RESTART_DELAY))
            first_attempt = False
            continue

        was_connected = False
        # Read cloudflared's merged stdout/stderr line by line. The old code
        # wrapped the WHOLE `for line in TUNNEL_PROC.stdout:` loop in one
        # try/except that silently gave up on ANY read error — even a
        # transient one — and fell straight through to `.wait()`. If that
        # happened before the URL line was ever seen, TUNNEL_STATUS stayed
        # wedged at "starting" indefinitely while cloudflared kept running
        # (and tunneling traffic) completely fine in the background —
        # `.wait()` doesn't return until the process actually exits, so
        # nothing here would ever notice or recover. This is consistent
        # with a report of the tunnel showing "stuck on starting" on a
        # Raspberry Pi (but not Windows) despite the tunnel itself working:
        # POSIX pipe reads are more exposed to a transient interrupted-
        # system-call error than Windows' named-pipe-based implementation.
        # Fix: only treat a read failure as "the process is gone" if it
        # actually is; otherwise it's a hiccup, not cloudflared dying, so
        # retry instead of abandoning status tracking.
        try:
            while True:
                try:
                    line = state.TUNNEL_PROC.stdout.readline()
                except Exception as e:
                    if state.TUNNEL_PROC.poll() is None:
                        _tunnel_print(f"Transient error reading cloudflared's output ({e}) — retrying...")
                        time.sleep(0.2)
                        continue
                    break  # process is actually gone — fall through to .wait()/reconnect below
                if not line:
                    break  # EOF: process is exiting
                _tunnel_log_line(line)
                if state.TUNNEL_URL is None:
                    m = _TUNNEL_URL_RE.search(line)
                    if m:
                        state.TUNNEL_URL = m.group(0)
                        state.TUNNEL_STATUS = "connected"
                        was_connected = True
                        _tunnel_print(f"Connected: {state.TUNNEL_URL}")
                        threading.Thread(target=_sync_cloudflare_kv, args=(state.TUNNEL_URL,), daemon=True).start()
        except Exception:
            pass

        state.TUNNEL_PROC.wait()
        state.TUNNEL_STATUS = "reconnecting" if was_connected else "error"
        _tunnel_print(f"{'Dropped, reconnecting' if was_connected else 'Failed to connect'} "
                      f"— retrying in {CF_RESTART_DELAY}s...")
        time.sleep(max(1, CF_RESTART_DELAY))
        first_attempt = False
        # loop restarts cloudflared automatically

TUNNEL_CONNECT_TIMEOUT_S = 25  # a real cloudflared Quick Tunnel handshake
# normally completes in a few seconds. If TUNNEL_STATUS is still "starting"
# after this long AND the process is confirmed still alive, something kept
# us from ever seeing the connected-URL log line — could be the read-loop
# issue described above, could be a genuine difference in cloudflared's log
# output on a given platform/version we haven't seen. Either way, don't
# stay silently wedged: force a restart so the normal reconnect path (which
# already works) gets a chance to recover instead.

def _tunnel_watchdog():
    """Safety net for _tunnel_supervisor: if the tunnel looks stuck at
    'starting' for far longer than a handshake should ever take, while
    cloudflared is still running, kill it so the supervisor's own
    reconnect loop restarts it cleanly. This is deliberately independent
    of *why* it got stuck — it's a backstop, not a diagnosis."""
    stuck_since = None
    while True:
        time.sleep(5)
        if state.TUNNEL_STATUS == "starting":
            if stuck_since is None:
                stuck_since = time.time()
            elif time.time() - stuck_since > TUNNEL_CONNECT_TIMEOUT_S:
                proc = state.TUNNEL_PROC
                if proc and proc.poll() is None:
                    _tunnel_print(f"Stuck at 'starting' for over {TUNNEL_CONNECT_TIMEOUT_S}s while "
                                  "cloudflared is still running — restarting it to recover.")
                    try: proc.terminate()
                    except Exception: pass
                stuck_since = None
        else:
            stuck_since = None

def _tunnel_probe_loop():
    """Every minute while the tunnel says "connected", ask the public address for this
    server's own /api/health. Informational only (Settings shows the result): it never
    restarts anything, because a failure can also mean this machine's DNS hasn't caught up
    with a brand-new trycloudflare.com name yet, or that outbound traffic to it is filtered."""
    last_url, seen_at = None, 0.0
    while True:
        time.sleep(15)
        url = state.TUNNEL_URL
        if state.TUNNEL_STATUS != "connected" or not url:
            state.TUNNEL_PROBE = None
            last_url = None
            continue
        if url != last_url:
            last_url, seen_at = url, time.time()
            state.TUNNEL_PROBE = None
        if time.time() - seen_at < 15:
            continue  # give a fresh tunnel a moment before the first check
        if state.TUNNEL_PROBE and time.time() - state.TUNNEL_PROBE["at"] < 60:
            continue
        try:
            req = urllib.request.Request(url + "/api/health", headers={"User-Agent": "VoidOS-tunnel-check"})
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = (r.status == 200 and b'"ok"' in r.read(2048))
            err = "" if ok else "The public address answered, but not with Void OS's health reply."
        except Exception as e:
            ok, err = False, str(getattr(e, "reason", None) or e)[:160]
        state.TUNNEL_PROBE = {"ok": ok, "at": time.time(), "error": err}

if CF_TUNNEL_EN:
    threading.Thread(target=_tunnel_supervisor, daemon=True).start()
    threading.Thread(target=_tunnel_watchdog, daemon=True).start()
    threading.Thread(target=_tunnel_probe_loop, daemon=True).start()
    if not CF_KV_NAMESPACE:
        state.KV_SYNC_STATUS = "not_configured"

# ══════════════════════════════════════════════════════════════════════
# MULTI-APP TUNNEL ROUTING — each app tile can opt into its own independent
# Cloudflare Quick Tunnel, so self-hosted apps (VS Code/code-server, Immich,
# Portainer, etc.) get a real public link without hand-editing nginx.
#
# Deliberately NOT done via one shared tunnel + nginx path prefixes —
# several apps (Portainer, Immich, code-server) don't tolerate being
# served from a subpath, since their own internal links assume they own the
# whole origin. Giving each app tile its OWN cloudflared process and its own
# full https://xxxx.trycloudflare.com origin sidesteps that entirely: no
# path is ever stripped or rewritten, so nothing about the app needs to know
# it's being tunneled.
#
# This is independent of CF_TUNNEL_EN/the main dashboard tunnel — an app tile
# can be tunneled whether or not the dashboard itself is. It only requires
# the `cloudflared` binary on PATH, checked per-start below.
#
# Each entry in apps.json may carry:
#   "tunnel_enabled": true/false
#   "local_port": int              — parsed from the tile's URL at save time
# Live state (never persisted) lives in APP_TUNNELS, keyed by app id:
#   {"status", "url", "port", "proc", "stop", "kv_key"}
# ══════════════════════════════════════════════════════════════════════
MAX_APP_TUNNELS = int(os.environ.get("VOID_MAX_APP_TUNNELS", "6"))
# A handful of long-running Quick Tunnels is exactly what they're built for;
# this cap exists to stop a runaway loop or a heavy-handed admin from spawning
# an unbounded number of cloudflared processes on something like a Pi. Raise
# via VOID_MAX_APP_TUNNELS in .env if you deliberately want more than 6.

GATE_PASSWORD_MIN_LEN = 6
# Only applies to the gate's Password mode now — PIN mode uses the shared PIN_LEN constant
# (defined near the top of this file) and is exactly 6 digits, no range, same as share links.
# This minimum is deliberately higher than nothing at all: a gate is the only thing standing
# between the public internet and something as high-blast-radius as Portainer (root-equivalent
# Docker control), whereas a share-link password protects one file.
# See FEATURE_REQUEST_tunnel_access_gate.md §4.
GATE_STARTUP_TIMEOUT_S = 8  # how long _start_app_tunnel will wait for a gate proxy to report its port before giving up
# Anchored to this file's own directory rather than left cwd-relative — same class of bug
# APP_DATA_DIR's old default had (see its definition near the top of this file), fixed the
# same way. In the normal spawn path this barely matters: voidos.py always resolves this constant
# itself and passes the resolved value explicitly to the gate_proxy.py subprocess via
# VOID_GATE_AUDIT_FILE, so the subprocess never falls back to its own default. It only matters
# if this constant's own resolution (right here, in voidos.py) ever ran from an unexpected cwd.
GATE_AUDIT_FILE = _void_path("VOID_GATE_AUDIT_FILE", "gate_audit.ndjson")
state._gate_audit_pos = 0  # in-memory read offset into GATE_AUDIT_FILE — resets on restart, that's fine (best-effort bridge)

_app_tunnels_lock = threading.RLock()
APP_TUNNELS = {}  # app_id -> dict, see docstring above


def _app_tunnel_supervisor(app_id: str, port: int):
    """Same shape as _tunnel_supervisor, generalized to one specific local
    port and one specific app id. Loops (with the same self-healing restart
    behavior) until APP_TUNNELS[app_id]["stop"] is set to True — set by
    _stop_app_tunnel when the tile is deleted or its tunnel is turned off."""
    binary = shutil.which("cloudflared")
    with _app_tunnels_lock:
        st = APP_TUNNELS.get(app_id)
        if st is None:
            return  # tile was removed before this thread even got scheduled
        if not binary:
            st["status"] = "not_installed"
            return
    first_attempt = True
    while True:
        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                return
            st["url"] = None
            st["status"] = "starting"
            named = st.get("named")  # {"hostname", "tunnel_id"} when the tile has its own domain
        cmd, env = [binary, "tunnel", "--url", f"http://127.0.0.1:{port}"], None
        if named:
            # Point the tunnel's ingress at the right port and fetch its run token (Cloudflare API).
            # Done at every start so a gate proxy's port, a changed port or a rotated token is
            # always current. The token goes in the environment, not on the command line.
            token, derr = domains.start_info(named["tunnel_id"], named["hostname"], port)
            if derr:
                with _app_tunnels_lock:
                    st = APP_TUNNELS.get(app_id)
                    if st is None or st.get("stop"): return
                    st["status"] = "error"; st["error"] = derr[:300]
                consolelog.warn(f"Custom domain not ready: {derr}", f"tunnel:{app_id}")
                time.sleep(max(5, CF_RESTART_DELAY))
                first_attempt = False
                continue
            with _app_tunnels_lock:
                st = APP_TUNNELS.get(app_id)
                if st is not None:
                    st.pop("error", None)
            cmd = [binary, "tunnel", "--no-autoupdate", "run"]
            env = {**os.environ, "TUNNEL_TOKEN": token}
        if first_attempt:
            consolelog.say(f"Starting cloudflared for localhost:{port}"
                           + (f" as {named['hostname']}" if named else "") + "...", f"tunnel:{app_id}")
        try:
            proc = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, universal_newlines=True,
                encoding="utf-8", errors="replace", **winplat.no_window(),  # same UTF-8-on-Windows fix as the main tunnel
            )
        except Exception as e:
            with _app_tunnels_lock:
                st = APP_TUNNELS.get(app_id)
                if st is None or st.get("stop"): return
                st["status"] = "error"; st["error"] = str(e)[:200]
            consolelog.say(f"Could not start cloudflared: {e}", f"tunnel:{app_id}")
            time.sleep(max(1, CF_RESTART_DELAY))
            first_attempt = False
            continue

        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                proc.terminate()
                return
            st["proc"] = proc

        was_connected = False
        # Same fix as _tunnel_supervisor's read loop: don't wrap the WHOLE
        # `for line in proc.stdout:` loop in one try/except. That pattern
        # treats any transient pipe-read error as "the process is gone" and
        # falls straight through to .wait() — which never returns for a
        # live process — silently abandoning status tracking while
        # cloudflared keeps tunneling fine in the background. This is the
        # per-app twin of the main-tunnel bug fixed in that function; it
        # was left unfixed here, which is why per-app tunnel sliders could
        # still get stuck on "starting" even after the main dashboard
        # tunnel's fix shipped. Read line-by-line instead and only treat a
        # read failure as real process death if the process has actually
        # exited; otherwise retry.
        try:
            while True:
                try:
                    line = proc.stdout.readline()
                except Exception as e:
                    if proc.poll() is None:
                        consolelog.say(f"Transient error reading cloudflared's output ({e}) — retrying...", f"tunnel:{app_id}")
                        time.sleep(0.2)
                        continue
                    break  # process is actually gone — fall through to .wait()/reconnect below
                if not line:
                    break  # EOF: process is exiting
                with _app_tunnels_lock:
                    st = APP_TUNNELS.get(app_id)
                    if st is None or st.get("stop"):
                        proc.terminate(); return
                    if st.get("url") is None:
                        if named:
                            m = _TUNNEL_REGISTERED_RE.search(line)
                            url = f"https://{named['hostname']}" if m else None
                        else:
                            m = _TUNNEL_URL_RE_APP.search(line)
                            url = m.group(0) if m else None
                        if m:
                            st["url"] = url; st["status"] = "connected"
                            was_connected = True
                            kv_key = None if named else st.get("kv_key")  # a stable domain needs no Workers KV hop
                            consolelog.say(f"Connected: {url}", f"tunnel:{app_id}")
                            if kv_key:
                                threading.Thread(target=_sync_cloudflare_kv,
                                                  args=(url, kv_key), daemon=True).start()
        except Exception:
            pass

        proc.wait()
        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                return
            st["status"] = "reconnecting" if was_connected else "error"
        consolelog.say(f"{'Dropped, reconnecting' if was_connected else 'Failed to connect'} "
                       f"— retrying in {CF_RESTART_DELAY}s...", f"tunnel:{app_id}")
        time.sleep(max(1, CF_RESTART_DELAY))
        first_attempt = False

_TUNNEL_URL_RE_APP = _TUNNEL_URL_RE  # same pattern, alias for readability in the function above
# What a named tunnel (custom domain) prints once it is up; it has no *.trycloudflare.com address to sniff.
_TUNNEL_REGISTERED_RE = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]{36} registered")


def _app_tunnel_watchdog():
    """Per-app twin of _tunnel_watchdog. The main dashboard tunnel's
    "stuck at starting" backstop was never generalized to per-app tunnels
    (each app tile's own tunnel slider), which is how they could still
    show "Connecting…" forever even after the main tunnel's fix, and even
    after the read-loop fix above — a real cloudflared version/platform
    quirk could still stop the URL line from ever appearing. Watches every
    entry in APP_TUNNELS at once (there's no fixed set of app ids up
    front — tunnels come and go as tiles are toggled) and force-restarts
    any one that's been wedged at "starting" too long while its process is
    still alive, exactly like the main watchdog does. Runs unconditionally
    at import time, independent of CF_TUNNEL_EN, since per-app tunnels are
    an opt-in per tile, not gated by the main dashboard tunnel setting."""
    stuck_since = {}  # app_id -> time first seen stuck at "starting"
    while True:
        time.sleep(5)
        with _app_tunnels_lock:
            snapshot = {app_id: (st.get("status"), st.get("proc"))
                        for app_id, st in APP_TUNNELS.items()}
        for app_id, (status, proc) in snapshot.items():
            if status == "starting":
                if app_id not in stuck_since:
                    stuck_since[app_id] = time.time()
                elif time.time() - stuck_since[app_id] > TUNNEL_CONNECT_TIMEOUT_S:
                    if proc and proc.poll() is None:
                        consolelog.say(f"Stuck at 'starting' for over {TUNNEL_CONNECT_TIMEOUT_S}s while "
                                       "cloudflared is still running — restarting it to recover.", f"tunnel:{app_id}")
                        try: proc.terminate()
                        except Exception: pass
                    # reset rather than delete: avoids re-terminating the same
                    # process again every 5s while the supervisor thread is
                    # still in the middle of noticing .wait() returned
                    stuck_since[app_id] = time.time()
            else:
                stuck_since.pop(app_id, None)
        # drop bookkeeping for tunnels that no longer exist (tile removed / tunnel turned off)
        for app_id in list(stuck_since):
            if app_id not in snapshot:
                stuck_since.pop(app_id, None)

threading.Thread(target=_app_tunnel_watchdog, daemon=True).start()


def _gate_proxy_supervisor(app_id: str, backend_port: int, pin_hash: str, ttl_hours: float, app_name: str,
                            is_pin: bool = True, pin_length: int = 6):
    """Same self-healing shape as _app_tunnel_supervisor: (re)spawns
    gate_proxy.py as its own subprocess, reads the listen port it announces
    on stdout (mirrors how cloudflared's own connected URL is sniffed from
    its stdout), and restarts it if it ever dies. Loops until
    APP_TUNNELS[app_id]["stop"] is set — the same unified kill-signal used
    for the tunnel side, since both are torn down together."""
    script = str(VOID_ROOT / "modules" / "gate_proxy.py")
    first_attempt = True
    last_port = None  # the port the gate announced the first time; reused after every restart
    while True:
        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                return
            st["gate_status"] = "starting"
            if last_port is None:
                st["gate_port"] = None
        if first_attempt:
            consolelog.say(f"Starting gate proxy in front of localhost:{backend_port}...", f"gate:{app_id}")
        env = os.environ.copy()
        env.update({
            "VOID_GATE_BACKEND_PORT": str(backend_port),
            # 0 = pick any free port the first time. After a crash, take the same port again:
            # cloudflared keeps forwarding to the one it was started with, so a gate that came
            # back on a new port left the tunnel pointing at nothing.
            "VOID_GATE_LISTEN_PORT": str(last_port or 0),
            "VOID_GATE_PIN_HASH": pin_hash,
            "VOID_GATE_SECRET_KEY": SECRET_KEY,  # same secret Void OS already uses — no second secret to manage
            "VOID_GATE_COOKIE_TTL_HOURS": str(ttl_hours),
            "VOID_GATE_APP_NAME": app_name,
            "VOID_GATE_APP_ID": app_id,
            "VOID_GATE_AUDIT_FILE": str(GATE_AUDIT_FILE),
            "VOID_GATE_IS_PIN": "true" if is_pin else "false",
            "VOID_GATE_PIN_LENGTH": str(pin_length or 6),
            "PYTHONUTF8": "1",  # child prints/writes UTF-8 regardless of the Windows ANSI code page
        })
        try:
            proc = subprocess.Popen([sys.executable, script], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     bufsize=1, universal_newlines=True,
                                     encoding="utf-8", errors="replace", **winplat.no_window())
        except Exception as e:
            with _app_tunnels_lock:
                st = APP_TUNNELS.get(app_id)
                if st is None or st.get("stop"): return
                st["gate_status"] = "error"; st["gate_error"] = str(e)[:200]
            consolelog.say(f"Could not start gate proxy: {e}", f"gate:{app_id}")
            time.sleep(max(1, CF_RESTART_DELAY)); first_attempt = False; continue

        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                proc.terminate(); return
            st["gate_proc"] = proc

        try:
            line = proc.stdout.readline()
        except Exception:
            line = ""
        m = re.match(r"GATE_LISTENING:(\d+)", (line or "").strip())
        if not m:
            with _app_tunnels_lock:
                st = APP_TUNNELS.get(app_id)
                if st is None or st.get("stop"): proc.terminate(); return
                st["gate_status"] = "error"
                st["gate_error"] = "Gate proxy exited before reporting a listen port" if not line else f"Unexpected output: {line.strip()[:150]}"
            proc.terminate()
            consolelog.say(f"Failed to start: {st.get('gate_error')}", f"gate:{app_id}")
            last_port = None  # if the old port was the problem, let the next try pick a fresh one
            time.sleep(max(1, CF_RESTART_DELAY)); first_attempt = False; continue

        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                proc.terminate(); return
            if last_port is None:
                st["gate_port"] = int(m.group(1))
            st["gate_status"] = "connected"
            last_port = int(m.group(1))
        consolelog.say(f"Guarding localhost:{backend_port} via 127.0.0.1:{m.group(1)}", f"gate:{app_id}")

        try:
            for _ in proc.stdout:  # drain audit-line prints until the process exits
                pass
        except Exception:
            pass
        proc.wait()
        with _app_tunnels_lock:
            st = APP_TUNNELS.get(app_id)
            if st is None or st.get("stop"):
                return
            st["gate_status"] = "reconnecting"
        consolelog.say(f"Gate proxy dropped, restarting in {CF_RESTART_DELAY}s...", f"gate:{app_id}")
        time.sleep(max(1, CF_RESTART_DELAY))
        first_attempt = False


def _start_app_tunnel(app_id: str, port: int, gate_cfg: dict | None = None, named: dict | None = None) -> tuple:
    """Start (or restart) a per-app tunnel. `gate_cfg`, when given, is
    {"pin_hash", "ttl_hours", "name"} — when present, a gate proxy is started
    FIRST and cloudflared is pointed at the gate's own port instead of the
    app's real port directly. This fails closed: if the gate can't be
    confirmed listening within GATE_STARTUP_TIMEOUT_S, the tunnel is never
    started at all, rather than falling back to exposing the raw port.
    `named`, when given, is {"hostname", "tunnel_id"}: the tile has its own domain, so a Cloudflare
    named tunnel is run instead of a Quick Tunnel (see modules/domains.py).
    Returns (ok, error_message)."""
    with _app_tunnels_lock:
        existing = APP_TUNNELS.get(app_id)
        same_cfg = (existing and not existing.get("stop") and existing.get("real_port") == port
                    and (existing.get("named") or None) == (named or None)
                    and bool(existing.get("gate_enabled")) == bool(gate_cfg)
                    and (not gate_cfg or existing.get("gate_pin_hash") == gate_cfg.get("pin_hash")))
        if same_cfg:
            return True, None  # already running with this exact config, nothing to do
        if existing:
            _stop_app_tunnel_locked(app_id)
        if sum(1 for s in APP_TUNNELS.values() if not s.get("stop")) >= MAX_APP_TUNNELS:
            err = f"Tunnel limit reached ({MAX_APP_TUNNELS}). Raise VOID_MAX_APP_TUNNELS in .env to allow more."
            APP_TUNNELS[app_id] = {"status": "at_cap", "url": None, "port": port, "real_port": port,
                                    "proc": None, "stop": True, "kv_key": None, "error": err,
                                    "gate_enabled": bool(gate_cfg), "gate_port": None, "gate_status": None}
            return False, err
        if not shutil.which("cloudflared"):
            err = "cloudflared not found on PATH — install it on this machine first."
            APP_TUNNELS[app_id] = {"status": "not_installed", "url": None, "port": port, "real_port": port,
                                    "proc": None, "stop": True, "kv_key": None, "error": err,
                                    "gate_enabled": bool(gate_cfg), "gate_port": None, "gate_status": None}
            return False, err
        APP_TUNNELS[app_id] = {"status": "starting", "url": None, "port": port, "real_port": port,
                                "proc": None, "stop": False,
                                "kv_key": f"app_{app_id}" if CF_KV_NAMESPACE else None,
                                "gate_enabled": bool(gate_cfg), "gate_proc": None, "gate_port": None,
                                "gate_status": "starting" if gate_cfg else None,
                                "gate_pin_hash": gate_cfg.get("pin_hash") if gate_cfg else None,
                                "named": dict(named) if named else None}

    cf_target_port = port
    if gate_cfg:
        threading.Thread(target=_gate_proxy_supervisor,
                          args=(app_id, port, gate_cfg["pin_hash"], gate_cfg.get("ttl_hours", 12),
                                gate_cfg.get("name") or "this app"),
                          kwargs={"is_pin": gate_cfg.get("is_pin", True), "pin_length": gate_cfg.get("pin_length") or 6},
                          daemon=True).start()
        deadline = time.time() + GATE_STARTUP_TIMEOUT_S
        while time.time() < deadline:
            with _app_tunnels_lock:
                st = APP_TUNNELS.get(app_id)
                if st is None:
                    return False, "Tunnel was removed while starting"
                if st.get("gate_port"):
                    cf_target_port = st["gate_port"]
                    break
                if st.get("gate_status") == "error":
                    err = st.get("gate_error", "Gate proxy failed to start")
                    _stop_app_tunnel(app_id)
                    return False, err
            time.sleep(0.15)
        else:
            _stop_app_tunnel(app_id)
            return False, "Gate proxy did not report a listen port in time — tunnel was not started"

    threading.Thread(target=_app_tunnel_supervisor, args=(app_id, cf_target_port), daemon=True).start()
    return True, None


def _stop_app_tunnel_locked(app_id: str):
    """Caller must already hold _app_tunnels_lock."""
    st = APP_TUNNELS.pop(app_id, None)
    if st:
        st["stop"] = True  # single unified kill-signal checked by both the tunnel and gate supervisor loops
        proc = st.get("proc")
        if proc:
            try: proc.terminate()
            except Exception: pass
        gate_proc = st.get("gate_proc")
        if gate_proc:
            try: gate_proc.terminate()
            except Exception: pass


def _stop_app_tunnel(app_id: str):
    with _app_tunnels_lock:
        _stop_app_tunnel_locked(app_id)


def named_cfg(tile: dict) -> dict | None:
    """The `named` argument for _start_app_tunnel, from a saved tile (None = use a Quick Tunnel)."""
    if tile.get("custom_hostname") and tile.get("cf_tunnel_id"):
        return {"hostname": tile["custom_hostname"], "tunnel_id": tile["cf_tunnel_id"]}
    return None


def _start_tunnels_for_saved_apps():
    """Called once at startup: re-launch tunnels (and any gate proxy) for
    every app tile that was left with tunnel_enabled=True from a previous
    run (mirrors the main dashboard tunnel's own auto-start behavior)."""
    for a in _aload():
        if a.get("tunnel_enabled") and a.get("local_port"):
            gate_cfg = None
            if a.get("gate_enabled") and a.get("gate_pin_hash"):
                gate_cfg = {"pin_hash": a["gate_pin_hash"],
                            "ttl_hours": a.get("gate_cookie_ttl_hours", 12),
                            "name": a.get("name"),
                            "is_pin": a.get("gate_is_pin", True),
                            "pin_length": a.get("gate_pin_length")}
            ok, err = _start_app_tunnel(a["id"], int(a["local_port"]), gate_cfg, named_cfg(a))
            if not ok:
                consolelog.say(f"Not started at boot: {err}", f"tunnel:{a['id']}")

def _cleanup_all_tunnels():
    """Best-effort: terminate every cloudflared process we spawned (main +
    per-app) on interpreter exit, so restarting Void OS doesn't leave orphan
    tunnels running as unmanaged, undiscoverable background processes."""
    try:
        if state.TUNNEL_PROC: state.TUNNEL_PROC.terminate()
    except Exception:
        pass
    with _app_tunnels_lock:
        for st in APP_TUNNELS.values():
            proc = st.get("proc")
            if proc:
                try: proc.terminate()
                except Exception: pass
            gate_proc = st.get("gate_proc")
            if gate_proc:
                try: gate_proc.terminate()
                except Exception: pass

def _zip_janitor():
    """Periodically sweep abandoned zip jobs. Without this, a job's temp
    file only gets cleaned up as an opportunistic side effect of someone
    else starting a NEW zip — if nobody ever does, a closed-tab job's temp
    file sits on disk indefinitely."""
    while True:
        time.sleep(300)
        try: _zip_cleanup_stale()
        except Exception: pass

@app.route("/api/admin/tunnel")
@require_admin
def api_admin_tunnel():
    return jsonify({
        "enabled": CF_TUNNEL_EN,
        "status": state.TUNNEL_STATUS,
        "url": state.TUNNEL_URL,
        "worker_url": CF_WORKER_URL or None,
        "recent_output": _tunnel_log_snapshot(),
        "probe": state.TUNNEL_PROBE,
        "kv": {
            "configured": bool(CF_KV_NAMESPACE),
            "status": state.KV_SYNC_STATUS,
            "error": state.KV_SYNC_ERROR or None,
            "last_synced_url": state.KV_LAST_SYNCED_URL,
            "last_synced_at": state.KV_LAST_SYNCED_AT,
        },
    })

@app.route("/api/admin/tunnel/sync-kv", methods=["POST"])
@require_admin
def api_admin_tunnel_sync_kv():
    if not state.TUNNEL_URL:
        return jsonify({"error": "No active tunnel URL to sync yet"}), 400
    if not CF_KV_NAMESPACE:
        return jsonify({"error": "CF_KV_NAMESPACE not set in .env"}), 400
    ok = _sync_cloudflare_kv(state.TUNNEL_URL)
    return jsonify({"ok": ok, "status": state.KV_SYNC_STATUS, "error": state.KV_SYNC_ERROR or None})
