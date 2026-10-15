"""
modules/system.py — Server statistics (CPU, memory, drives) and Docker connection helpers.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from collections import deque
from modules.winplat import IS_WIN
from pathlib import Path
import os
import psutil
import threading
import time
from modules import state
from modules.config import FILE_ROOT
from modules.web import app


# ══════════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════════
state._np = None
def _temp():
    try:
        p = Path("/sys/class/thermal/thermal_zone0/temp")
        if p.exists(): return round(float(p.read_text().strip()) / 1000, 1)
    except: pass
    try:
        t = psutil.sensors_temperatures()
        if t:
            for v in t.values():
                if v: return round(v[0].current, 1)
    except: pass
    return None

# ── CPU load ──────────────────────────────────────────────────────────
# One background sampler measures the CPU once a second and the dashboard just
# reads the latest figure. Measuring inside each request (a 0.1 s window, done
# twice) made the number jump around, made every stats call wait ~200 ms, and
# gave the total and the per-core bars two different samples. The sampler keeps
# an average of the last few seconds (steady, but still follows a real change in
# about 3 s) and the total is the mean of the same per-core samples, so the two
# always agree. It only runs while someone is looking: it stops 30 s after the
# last request and starts again on the next one.
_CPU_WINDOW = 3            # one-second samples averaged together
_CPU_IDLE_STOP_S = 30
_cpu_lock = threading.Lock()
_cpu_hist: deque = deque(maxlen=_CPU_WINDOW)     # each item: [per-core percentages]
_cpu_last_request = 0.0
_cpu_thread: threading.Thread | None = None

def _cpu_sampler():
    global _cpu_thread
    try:
        psutil.cpu_percent(None, percpu=True)   # the first reading only starts the clock
        while time.time() - _cpu_last_request < _CPU_IDLE_STOP_S:
            time.sleep(1.0)
            cores = psutil.cpu_percent(None, percpu=True)
            with _cpu_lock:
                _cpu_hist.append(cores)
    except Exception:
        pass
    finally:
        with _cpu_lock:
            _cpu_thread = None

def _cpu_reading() -> tuple[float, list]:
    """(overall %, per-core %) averaged over the last few seconds."""
    global _cpu_last_request, _cpu_thread
    _cpu_last_request = time.time()
    with _cpu_lock:
        if _cpu_thread is None or not _cpu_thread.is_alive():
            _cpu_hist.clear()                       # anything left is stale
            _cpu_thread = threading.Thread(target=_cpu_sampler, name="cpu-sampler", daemon=True)
            _cpu_thread.start()
        hist = list(_cpu_hist)
    if not hist:
        # First request after (re)start: no sample yet, so take one short one now.
        cores = psutil.cpu_percent(interval=0.25, percpu=True)
        hist = [cores]
    n = len(hist[-1])
    cores = [round(sum(h[i] for h in hist if len(h) == n) / sum(1 for h in hist if len(h) == n), 1) for i in range(n)]
    return round(sum(cores) / n, 1), cores

# Drives change slowly, and reading each one can wake a sleeping disk or hang on an
# unreachable network share, so every dashboard tab sharing one recent reading
# beats each tab re-reading them all every 2 s.
_DISKS_TTL_S = 5
_disks_cache = {"t": 0.0, "v": []}
_disks_lock = threading.Lock()

def _disks_cached() -> list:
    with _disks_lock:
        if time.time() - _disks_cache["t"] > _DISKS_TTL_S:
            _disks_cache["v"] = _real_disks()
            _disks_cache["t"] = time.time()
        return _disks_cache["v"]

# ── Power: Raspberry Pi under-voltage, and laptop battery ─────────────────────────────
# A Pi that is not getting enough power throttles itself, drops USB devices and corrupts SD cards,
# and the usual first sign is nothing at all. The firmware keeps a bit-field of what it has seen:
#   bit 0 under-voltage now      bit 1 ARM frequency capped now   bit 2 throttled now
#   bit 3 soft temperature limit bit 16-19 the same four, "has happened since boot"
# A laptop acting as the server can run out of battery (or be unplugged by accident).
_POWER_TTL_S = 4
_power_cache = {"t": 0.0, "v": None}
_power_lock = threading.Lock()
_is_pi = None


def _pi_model():
    """The Raspberry Pi model string, or '' on anything else."""
    global _is_pi
    if _is_pi is None:
        _is_pi = ""
        try:
            p = Path("/proc/device-tree/model")
            if not IS_WIN and p.exists():
                m = p.read_bytes().decode("utf-8", "replace").strip("\x00 \n")
                if "raspberry pi" in m.lower():
                    _is_pi = m
        except Exception:
            pass
    return _is_pi


def _pi_throttled_bits():
    """The firmware's throttled bit-field, or None when it can't be read."""
    for path in ("/sys/devices/platform/soc/soc:firmware/get_throttled",
                 "/sys/devices/platform/soc/soc:firmware/raspberrypi-hwmon/get_throttled"):
        try:
            txt = Path(path).read_text().strip()
            if txt:
                return int(txt, 16)
        except Exception:
            continue
    try:
        import shutil
        import subprocess
        exe = shutil.which("vcgencmd")
        if exe:
            r = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=3)
            txt = r.stdout.strip()
            if "=" in txt:
                return int(txt.split("=", 1)[1].strip(), 16)
    except Exception:
        pass
    # newer kernels: the rpi_volt hwmon reports only "under-voltage right now"
    try:
        for hw in Path("/sys/class/hwmon").glob("hwmon*"):
            if (hw / "name").read_text().strip() == "rpi_volt":
                return 1 if (hw / "in0_lcrit_alarm").read_text().strip() == "1" else 0
    except Exception:
        pass
    return None


def _power_reading():
    out = {}
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is not None:
        secs = b.secsleft if isinstance(b.secsleft, int) and b.secsleft >= 0 else None
        out["battery"] = {"percent": round(b.percent), "plugged": b.power_plugged, "secs_left": secs}
    model = _pi_model()
    if model:
        bits = _pi_throttled_bits()
        if bits is not None:
            out["pi"] = {"model": model, "undervolt_now": bool(bits & 0x1), "undervolt_past": bool(bits & 0x10000),
                         "freq_capped": bool(bits & 0x2), "throttled_now": bool(bits & 0x4),
                         "throttled_past": bool(bits & 0x40000), "temp_limit": bool(bits & 0x8)}
    return out or None


def _power():
    with _power_lock:
        if time.time() - _power_cache["t"] > _POWER_TTL_S:
            try:
                _power_cache["v"] = _power_reading()
            except Exception:
                _power_cache["v"] = None
            _power_cache["t"] = time.time()
        return _power_cache["v"]

def _stats():
    now = time.time(); ram = psutil.virtual_memory()
    disks = _disks_cached()
    cpu, cores = _cpu_reading()
    net = psutil.net_io_counters(); up = dn = 0.
    if state._np:
        dt = now - state._np["t"]
        if dt > 0: up = max(0., (net.bytes_sent-state._np["s"])/dt); dn = max(0., (net.bytes_recv-state._np["r"])/dt)
    state._np = {"s": net.bytes_sent, "r": net.bytes_recv, "t": now}
    us = int(now - psutil.boot_time()); h, r = divmod(us, 3600); m, s = divmod(r, 60)
    return {"cpu": cpu, "cores": cores,
        "ram": {"total":ram.total,"used":ram.used,"available":ram.available,"percent":ram.percent},
        "disks": disks, "net": {"up":up,"down":dn}, "temp": _temp(), "power": _power(),
        "uptime": f"{h}h {m}m" if h else f"{m}m {s}s", "procs": len(psutil.pids())}

# ══════════════════════════════════════════════════════════════════════
# DOCKER HELPERS
# ══════════════════════════════════════════════════════════════════════
# Filesystems that are never "your drives": read-only app images (snap
# squashfs on Ubuntu shows up as a dozen 100%-full disks), overlays, RAM
# disks, FUSE helpers, discs. Listing them buried the real drive under a
# column of alarming red 100% bars.
_PSEUDO_FS = {"squashfs", "overlay", "overlayfs", "tmpfs", "devtmpfs", "ramfs", "iso9660", "udf",
              "autofs", "nsfs", "proc", "sysfs", "cgroup", "cgroup2", "efivarfs", "fuse.snapfuse",
              "fuse.lxcfs", "fuse.gvfsd-fuse", "fuse.portal"}
_PSEUDO_MOUNT_PREFIXES = ("/snap/", "/var/snap/", "/var/lib/docker/", "/run/", "/sys/", "/proc/",
                          "/dev/", "/boot/efi", "/boot/firmware")

def _real_disks() -> list:
    out, seen = [], {}
    try:
        files_real = os.path.realpath(FILE_ROOT)
    except OSError:
        files_real = str(FILE_ROOT)
    for p in psutil.disk_partitions(all=False):
        fs = (p.fstype or "").lower()
        mp = p.mountpoint
        if fs in _PSEUDO_FS or fs.startswith("fuse.") or (not IS_WIN and mp != "/" and mp.startswith(_PSEUDO_MOUNT_PREFIXES)):
            continue
        if IS_WIN and "cdrom" in (p.opts or ""):
            continue
        try:
            u = psutil.disk_usage(mp)
        except Exception:
            continue
        if u.total < 64 * 1024 * 1024:  # tiny boot/firmware partitions
            continue
        key = p.device or mp
        if key in seen and len(seen[key]["mount"]) <= len(mp):
            continue  # same device mounted twice (bind mounts) — keep the shortest path
        d = {"mount": mp, "device": p.device, "total": u.total, "used": u.used, "free": u.free,
             "percent": u.percent, "fstype": fs}
        if key in seen:
            out.remove(seen[key])
        seen[key] = d
        out.append(d)
    # Which drive holds the Files root: the longest mountpoint that contains it.
    best = None
    for d in out:
        m = d["mount"].rstrip("/\\") or "/"
        if files_real == m or files_real.startswith(m.rstrip("/\\") + os.sep):
            if best is None or len(m) > len(best["mount"]):
                best = d
    for d in out:
        d["files"] = d is best
        m = d["mount"]
        if IS_WIN:
            d["label"] = m.rstrip("\\")
        elif m == "/":
            d["label"] = "System drive"
        else:
            d["label"] = os.path.basename(m.rstrip("/")) or m
    out.sort(key=lambda d: (not d["files"], d["mount"] != "/", d["mount"]))
    return out

def _ci(c):
    try:
        ports = {k:[p["HostPort"] for p in v if "HostPort" in p] for k,v in (c.ports or {}).items() if v}
        return {"id":c.short_id,"name":c.name,
            "image":c.image.tags[0] if c.image.tags else c.image.short_id,
            "status":c.status,"ports":ports,"created":c.attrs.get("Created","")[:10],
            "restarts":int(c.attrs.get("RestartCount") or 0),
            "compose":bool(((c.attrs.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project.config_files")),
            "exit_code":(c.attrs.get("State") or {}).get("ExitCode")}
    except Exception as e:
        app.logger.exception(f"[_ci] {e}")
        return {"id":c.short_id,"name":c.name,"status":"unknown","error":"Could not read container details"}
