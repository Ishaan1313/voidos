"""
modules/disk_monitor.py — Background disk-space monitor and alerts.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

import os
import psutil
import time
from modules.config import APP_DATA_DIR, FILE_ROOT
from modules.activity import _audit, _inapp_notify_admins
from modules.error_log import _log_exception


# ── Disk space monitor ────────────────────────────────────────────────
_DISK_WARN_PCT = float(os.environ.get("VOID_DISK_WARN_PERCENT", "90"))
_DISK_CRIT_PCT = float(os.environ.get("VOID_DISK_CRITICAL_PERCENT", "95"))
_disk_levels: dict = {}

def _disk_check_once() -> None:
    """Notifies when a watched drive crosses into warning/critical, and
    when it recovers — never repeatedly while it stays at the same level."""
    watched = {"files": FILE_ROOT, "app data": APP_DATA_DIR}
    seen_devs = set()
    for label, path in watched.items():
        try:
            dev = os.stat(path).st_dev
            if dev in seen_devs:
                continue
            seen_devs.add(dev)
            u = psutil.disk_usage(str(path))
        except (OSError, ValueError):
            continue
        level = 2 if u.percent >= _DISK_CRIT_PCT else 1 if u.percent >= _DISK_WARN_PCT else 0
        prev = _disk_levels.get(label, 0)
        _disk_levels[label] = level
        free = _fmt_bytes(u.free)
        if level > prev:
            _audit("disk_space_low", None, target=f"{label.capitalize()} drive",
                   detail=f"{u.percent:.0f}% full — {free} free" + (" (critical)" if level == 2 else ""))
        elif level == 0 and prev > 0:
            _inapp_notify_admins("Disk space back to normal", f"{label} drive: {free} free ({u.percent:.0f}% used)",
                                 level="success", kind="system", link="dashboard")

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024

def _disk_monitor_loop():
    time.sleep(20)
    while True:
        try:
            _disk_check_once()
        except Exception as e:
            _log_exception(e, source="job", context="disk monitor")
        time.sleep(300)
