#!/usr/bin/env python3
"""
Void OS — self-hosted home server dashboard. Start it with:

    python voidos.py

This file is only the entry point. The server is split by feature into
modules/ (see modules/__init__.py for the map); importing them below, in
order, registers every route on the Flask app.
"""
import atexit
import logging
import os
import sys
import threading
import importlib

# ── Python packages: install anything an update added ───────────────────
# Runs before ANY feature module is imported, so (a) a package an update
# newly requires is installed before something tries to import it, and
# (b) nothing is loaded yet — on Windows a loaded package's files are
# locked and pip can't replace them. Keyed on a hash of requirements.txt
# recorded after each successful install (by this check or the installer),
# so ordinary restarts do no work at all.
#   VOID_AUTO_INSTALL_PACKAGES = auto (default: only inside Void OS's .venv)
#                              | always (also into a system Python) | never
def _ensure_requirements():
    import hashlib, subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    req = os.path.join(here, "requirements.txt")
    marker = os.path.join(here, "tmp", ".requirements.sha256")
    if not os.path.exists(req):
        return
    with open(req, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    try:
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == digest:
                return
    except OSError:
        pass
    mode = os.environ.get("VOID_AUTO_INSTALL_PACKAGES", "auto").strip().lower()
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if mode == "never" or (mode == "auto" and not in_venv):
        print("  Void OS: requirements.txt has changed. Install the new packages with the installer in")
        print("  'first setup' (it updates Void OS's .venv), or set VOID_AUTO_INSTALL_PACKAGES=always.")
        return
    print("  Void OS: requirements.txt changed — installing/updating Python packages…", flush=True)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", req])
    if r.returncode == 0:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(digest)
        print("  Void OS: packages are up to date.", flush=True)
    else:
        print("  ⚠ Void OS: couldn't install the updated packages (see pip's message above).")
        print("    Check the internet connection, then restart Void OS or re-run the installer.")


if __name__ == "__main__" and "--skip-package-check" not in sys.argv:
    _ensure_requirements()

_MODULES = ['config', 'web', 'users', 'twofactor', 'limits', 'auth', 'activity', 'error_log', 'decorators', 'file_access', 'security', 'system', 'file_helpers', 'account_routes', 'cloud_routes', 'storage_routes', 'admin_routes', 'twofa_routes', 'diagnose', 'container_routes', 'file_routes', 'file_preview', 'file_ops', 'trash_zip_routes', 'shares', 'webdav', 'intercom_routes', 'domains', 'app_tiles', 'app_store', 'disk_monitor', 'app_store_routes', 'live_sockets', 'tunnels', 'updater', 'catalog_update']
for _m in _MODULES:
    try:
        importlib.import_module(f"modules.{_m}")
    except ModuleNotFoundError as _e:
        if _e.name and not _e.name.startswith("modules"):
            # A third-party package is missing: say which and how to fix it,
            # rather than a traceback from deep inside a feature module.
            print(f"\n  Void OS can't start: the Python package '{_e.name}' isn't installed.")
            print("  Fix: run the installer in the 'first setup' folder again (it installs everything")
            print("  in requirements.txt), or: <python> -m pip install -r requirements.txt\n")
            raise SystemExit(1)
        raise
# Every module is loaded; now give each one the names it uses from modules
# that loaded after it (see _bind_later_modules in each module).
for _m in _MODULES:
    _bind = getattr(importlib.import_module(f"modules.{_m}"), "_bind_later_modules", None)
    if _bind:
        _bind()

from modules import consolelog, state, winplat
from modules.config import (
    CF_KV_NAMESPACE, CF_RESTART_DELAY, CF_TUNNEL_EN, CF_WORKER_URL, FILE_ROOT, OS_NAME, PORT,
    SERVER_THREADS, TERM_EN,
)
from modules.tunnels import _cleanup_all_tunnels, _start_tunnels_for_saved_apps, _zip_janitor
from modules.disk_monitor import _disk_monitor_loop
from modules.storage_routes import _remount_network_shares_on_boot, _usb_watcher_loop
from modules.cloud_routes import _restore_sync_pairs_on_boot
from modules.file_ops import _staging_sweep_on_start
from modules.file_routes import _upload_janitor
from modules.web import app

def __getattr__(name):
    """`voidos.<name>` keeps working for tools and scripts written against the
    single-file layout: look the name up in the feature modules."""
    import sys
    for _m in _MODULES:
        mod = sys.modules.get(f"modules.{_m}")
        if mod is not None and name in vars(mod):
            return vars(mod)[name]
    raise AttributeError(name)


# ══════════════════════════════════════════════════════════════════════
if __name__=="__main__" and "--reset-2fa" in sys.argv:
    # Last resort for a Super User who lost both their phone and their backup
    # codes: `python voidos.py --reset-2fa <username>` turns that account's
    # two-factor sign-in off. Needs access to this machine, so it's no way in
    # for an attacker.
    from modules import twofactor
    from modules.users import _get_user_by_username
    _i = sys.argv.index("--reset-2fa")
    _name = (sys.argv[_i + 1] if len(sys.argv) > _i + 1 else "").strip().lower()
    _u = _get_user_by_username(_name) if _name else None
    if not _name or not _u:
        print(f"\n  Usage: python voidos.py --reset-2fa <username>   (no account named '{_name}')\n")
        raise SystemExit(1)
    if not twofactor.is_enabled(_u):
        print(f"\n  {_name} doesn't have two-factor sign-in turned on.\n")
        raise SystemExit(0)
    twofactor.remove(_u["id"])
    from modules.activity import _audit
    _audit("2fa_reset", None, target=_name, detail="from the command line")
    print(f"\n  Two-factor sign-in turned off for {_name}. They can sign in with just their password now.\n")
    raise SystemExit(0)

if __name__=="__main__":
    # Windows: if /api/admin/restart relaunched us, wait for the old process to
    # release the port; then tie every helper process's lifetime to ours.
    winplat.wait_for_previous_instance()
    winplat.kill_children_with_us()
    # What the console shows: one plain summary, then ordinary log lines. Nothing interactive,
    # and the same on every OS. (Flushed: when stdout is a pipe -- systemd, docker logs, nohup --
    # Python would otherwise hold a short banner in its buffer indefinitely.)
    banner_rows = [("Address", f"http://localhost:{PORT}"),
                   ("Files", str(FILE_ROOT)),
                   ("Terminal", "in the browser, at /terminal (" + ("enabled" if TERM_EN else "disabled") + ")"),
                   ("Docker", "connected" if state.DOCKER_OK
                              else "not available" + (f": {state.DOCKER_ERROR}" if state.DOCKER_ERROR else ""))]
    if CF_TUNNEL_EN:
        banner_rows.append(("Tunnel", f"Cloudflare Quick Tunnel starting in the background "
                                      f"(restarts every {CF_RESTART_DELAY}s if it drops)"))
        banner_rows.append(("Workers KV", f"configured, namespace {CF_KV_NAMESPACE}" if CF_KV_NAMESPACE
                                          else "not configured (CF_KV_NAMESPACE unset)"))
        if CF_WORKER_URL:
            banner_rows.append(("Worker URL", CF_WORKER_URL))
    if sys.platform == "win32":
        # Gunicorn needs os.fork()/fcntl, so Windows uses Werkzeug's threaded server; flask-sock
        # supports it directly, so the in-browser terminal works there too.
        banner_rows.append(("Server", "Werkzeug (Windows: Gunicorn is POSIX-only)"))
    else:
        banner_rows.append(("Server", f"Gunicorn, 1 worker, {SERVER_THREADS} threads (gthread: the terminal's WebSocket needs it)"))
    consolelog.banner(OS_NAME, banner_rows)

    # Werkzeug's own lines (start-up notes, one per request) come through logging, sometimes several
    # lines in one record and with colour codes. Print them like everything else: plain, indented,
    # without the "development server" warning (the Server row above already says so).
    import re as _re
    _ansi = _re.compile(r"\x1b\[[0-9;]*m")

    class _TidyLogs(logging.Filter):
        def filter(self, rec):
            lines = []
            for ln in _ansi.sub("", rec.getMessage()).splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("WARNING: This is a development server"):
                    continue
                if ln.startswith("*"):
                    ln = ln.lstrip("* ").strip()
                lines.append(consolelog.INDENT + ln)
            if not lines:
                return False
            rec.msg, rec.args = "\n".join(lines), ()
            return True
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _wl = logging.getLogger("werkzeug")
    _wl.handlers[:] = [_h]
    _wl.addFilter(_TidyLogs())
    _wl.propagate = False
    _wl.setLevel(logging.INFO)
    try:
        import flask.cli as _flask_cli
        _flask_cli.show_server_banner = lambda *a, **k: None   # "Serving Flask app ... Debug mode: off"
    except Exception:
        pass

    def _start_deferred_background_work():
        """The tunnel restorer, zip janitor, and cleanup registration used
        to run unconditionally at module import time — a landmine under
        any production WSGI server: every worker process that imports
        voidos.py independently would spin up its own duplicate tunnel
        threads, fighting over the same ports for the same app tiles.

        Moved here, called explicitly once the real server is up, in
        whichever process is actually going to serve requests — under
        Gunicorn that's the post_worker_init hook below (which runs AFTER
        the fork, inside the worker), not the arbiter process. Starting
        these threads BEFORE that fork instead (e.g. directly in this
        __main__ block) would leave them alive only in the arbiter,
        since fork() doesn't replicate a parent's other running threads
        into the child — Flask's request handlers, which run in the
        worker, would then see none of this state (no tunnels ever
        marked connected, etc.), a subtler version of the exact
        multi-process landmine this whole change is meant to close."""
        threading.Thread(target=_start_tunnels_for_saved_apps, daemon=True).start()
        threading.Thread(target=_zip_janitor, daemon=True).start()
        threading.Thread(target=_upload_janitor, daemon=True).start()
        threading.Thread(target=_disk_monitor_loop, daemon=True).start()
        from modules.updater import _auto_check_loop
        threading.Thread(target=_auto_check_loop, daemon=True).start()
        threading.Thread(target=_staging_sweep_on_start, daemon=True).start()
        _restore_sync_pairs_on_boot()  # each pair spins up its own daemon thread inside this call
        _remount_network_shares_on_boot()
        threading.Thread(target=_usb_watcher_loop, daemon=True).start()
        atexit.register(_cleanup_all_tunnels)

    if sys.platform == "win32":
        _start_deferred_background_work()
        # use_reloader=False explicitly: the reloader re-executes the script in a
        # child process, which would double every tunnel/sync/USB-watcher thread.
        app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False, debug=False)
    else:
        from gunicorn.app.base import BaseApplication
        # Workers inherit this, so a restart can stop the whole server rather
        # than just the worker (see winplat.restart_server).
        os.environ["VOID_GUNICORN_MASTER_PID"] = str(os.getpid())

        class _VoidGunicornApp(BaseApplication):
            def __init__(self, wsgi_app, options):
                self.application = wsgi_app
                self.options = options
                super().__init__()
            def load_config(self):
                for k, v in self.options.items():
                    if k in self.cfg.settings:
                        self.cfg.set(k, v)
            def load(self):
                return self.application

        def _gunicorn_logging():
            """Gunicorn's log lines with the same indent as everything else (falls back to its default)."""
            try:
                import copy
                from gunicorn.glogging import CONFIG_DEFAULTS
                cfg = copy.deepcopy(CONFIG_DEFAULTS)
                cfg["formatters"]["generic"]["format"] = consolelog.INDENT + "%(asctime)s [%(levelname)s] %(message)s"
                cfg["formatters"]["generic"]["datefmt"] = "%H:%M:%S"
                return cfg
            except Exception:
                return None

        _VoidGunicornApp(app, {
            "bind": f"0.0.0.0:{PORT}",
            "logconfig_dict": _gunicorn_logging() or {},
            "workers": 1,       # never raise this without first externalizing APP_TUNNELS/_fails/
                                 # _zip_jobs/session+user JSON stores to shared storage — see README.md
            "threads": SERVER_THREADS,  # VOID_THREADS (default 32). Every open browser tab, terminal and slow download holds
                                 # one thread; the VOID_MAX_* caps in .env keep them from using them all up
            "worker_class": "gthread",  # required for flask-sock's WebSocket support under Gunicorn
            "post_worker_init": lambda worker: _start_deferred_background_work(),
            "timeout": 0,       # Gunicorn's worker-hang watchdog defaults to killing a worker that hasn't
                                 # responded in 30s — but a gthread worker legitimately holds long-lived
                                 # connections open for their entire duration (an idle terminal session where
                                 # nobody's typing, a gated Portainer/Immich tunnel left open) with no way for
                                 # Gunicorn to tell "idle but fine" apart from "actually hung." With workers=1
                                 # there's no redundant worker to fail over to anyway, so killing the sole
                                 # worker over this has no safety upside — it would just silently end whatever
                                 # long-lived session happened to be open around the 30s mark, undoing the
                                 # whole point of the flask-sock rewrite. Disabled entirely; see graceful_timeout
                                 # below for clean-shutdown behavior, which this does NOT affect.
            "graceful_timeout": 30,  # unrelated to the worker-hang watchdog above — this only governs how long
                                     # a restart/reload waits for in-flight requests to finish before forcing
                                     # them closed, and stays unchanged so restarts still behave predictably.
        }).run()
