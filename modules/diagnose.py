"""
modules/diagnose.py — "why is this container restarting / not starting?"

Reads what Docker already knows about a container (exit code, whether the kernel
killed it for memory, its last log lines) together with a few facts about this
machine (RAM, Docker's CPU architecture, the kernel's memory page size) and turns
that into a plain sentence plus the steps that usually fix it.

Nothing here changes anything. It is used by the App Store (an install that ends in
a crash loop explains why) and by the Containers page ("Why?" button).

Every function is best effort and never raises: a diagnosis is a convenience and
must not be able to break the page it is shown on.
"""
from __future__ import annotations

import os
import platform
import re
import time

from modules import state

try:
    import psutil
except Exception:  # pragma: no cover - psutil is a hard requirement elsewhere
    psutil = None

_DOCKER_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
                "armv7l": "arm", "armv6l": "arm", "armhf": "arm", "arm": "arm", "i386": "386", "i686": "386"}
_host_cache = (0.0, None)


# ── this machine ─────────────────────────────────────────────────────
def host_profile(max_age: float = 30.0) -> dict:
    """What matters when an app crashes on a small board. Cached for a few seconds because the
    install dialog and the Containers page may both ask."""
    global _host_cache
    ts, cached = _host_cache
    if cached is not None and time.time() - ts < max_age:
        return cached
    h = {"machine": platform.machine(), "model": "", "is_pi": False, "ram_mb": None, "swap_mb": None,
         "cores": os.cpu_count(), "page_size": None, "docker_arch": None, "os_bits": None}
    try:
        with open("/proc/device-tree/model", "rb") as f:
            h["model"] = f.read().decode("utf-8", "replace").strip("\x00 \n")
            h["is_pi"] = "raspberry pi" in h["model"].lower()
    except OSError:
        pass
    if psutil is not None:
        try:
            h["ram_mb"] = int(psutil.virtual_memory().total / 1048576)
            h["swap_mb"] = int(psutil.swap_memory().total / 1048576)
        except Exception:
            pass
    try:
        h["page_size"] = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, ValueError, OSError):
        pass
    arch = None
    if state.DOCKER_OK:
        try:
            arch = str(state._docker.info().get("Architecture", "") or "").lower()
        except Exception:
            arch = None
    arch = _DOCKER_ARCH.get(arch or platform.machine().lower())
    h["docker_arch"] = arch
    if arch in ("amd64", "arm64"):
        h["os_bits"] = 64
    elif arch in ("arm", "386"):
        h["os_bits"] = 32
    _host_cache = (time.time(), h)
    return h


# ── a diagnosis ──────────────────────────────────────────────────────
def _d(code, title, cause, fix, evidence=None, severity="error"):
    return {"code": code, "title": title, "cause": cause, "fix": fix,
            "evidence": evidence or [], "severity": severity}


def _grab(text: str, rx: str, n: int = 3) -> list:
    out = []
    for ln in text.splitlines():
        if re.search(rx, ln, re.I):
            out.append(ln.strip()[:240])
            if len(out) >= n:
                break
    return out


def _mb(v):
    return f"{v / 1024:.1f} GB" if v and v >= 1024 else (f"{v} MB" if v else "unknown")


def explain(attrs: dict, logs: str, host: dict | None = None) -> dict:
    """Diagnosis for one container. `attrs` is its Docker inspect data, `logs` its last output."""
    host = host or host_profile()
    st = (attrs or {}).get("State") or {}
    image = str(((attrs or {}).get("Config") or {}).get("Image") or "").lower()
    name = str((attrs or {}).get("Name") or "").lstrip("/")
    code = st.get("ExitCode")
    text = logs or ""
    low = text.lower()
    ram = host.get("ram_mb")
    is_ml = "machine-learning" in image or "machine-learning" in name or "machine_learning" in name
    ml_hint = ("If this is Immich, reinstall it with 'AI features' turned off in the install dialog: "
               "photos still sync and browse, only smart search and face grouping go away.") if (
        is_ml or "immich" in image or "immich" in name) else None

    # 1. the kernel killed it for memory
    if st.get("OOMKilled") or (code == 137 and ("killed" in low or not text)):
        fix = [f"This machine has {_mb(ram)} of RAM"
               + (f" and {_mb(host.get('swap_mb'))} of swap." if host.get("swap_mb") is not None else "."),
               "Close or stop other apps, or add swap (on Raspberry Pi OS: edit CONF_SWAPSIZE in /etc/dphys-swapfile, "
               "then `sudo systemctl restart dphys-swapfile`)."]
        if ml_hint:
            fix.insert(0, ml_hint)
        return _d("oom", "It ran out of memory",
                  "Linux stopped this container because the machine had no memory left"
                  + (" (the kernel reported an out-of-memory kill)." if st.get("OOMKilled") else " (exit code 137)."),
                  fix, _grab(text, r"killed|out of memory|oom"))

    # 2. 16 KB kernel pages (Raspberry Pi 5 default) with software built for 4 KB
    if "unsupported system page size" in low:
        return _d("page_size", "This kernel's memory page size isn't supported by the program",
                  "The program was built for 4 KB memory pages, but this system's kernel uses 16 KB pages "
                  "(the default on a Raspberry Pi 5)."
                  + (f" Detected page size: {host['page_size']} bytes." if host.get("page_size") else ""),
                  ["Boot the 4 KB-page kernel: add the line `kernel=kernel8.img` at the top of "
                   "/boot/firmware/config.txt (older systems: /boot/config.txt) and reboot.",
                   "Check afterwards with `getconf PAGESIZE`; it should print 4096.",
                   "Or update the app: newer builds of the database image (for Immich, VectorChord 0.4.3 or later) "
                   "support 16 KB pages."],
                  _grab(text, r"page size|jemalloc"))

    # 3. CPU instruction not available
    if code == 132 or "illegal instruction" in low:
        fix = ["This is a mismatch between the program and this CPU, not a setting. Try a different version of the app, "
               "or run it on a newer machine."]
        if ml_hint:
            fix.insert(0, ml_hint)
        return _d("sigill", "The CPU doesn't support an instruction this program uses",
                  "The container crashed with 'illegal instruction' (exit code 132): the program was built to use a "
                  "CPU feature that this processor doesn't have.", fix, _grab(text, r"illegal instruction"))

    # 4. wrong architecture / 32-bit system
    if re.search(r"exec format error|no matching manifest|does not match the specified platform|no match for platform", low):
        bits = host.get("os_bits")
        return _d("arch", "This app isn't built for this machine's architecture",
                  f"The image doesn't have a build for this system (Docker reports "
                  f"{host.get('docker_arch') or host.get('machine') or 'an unknown architecture'}"
                  + (f", {bits}-bit" if bits else "") + ").",
                  ["Many apps only publish 64-bit images. If this is a Raspberry Pi running a 32-bit OS, "
                   "reinstall with the 64-bit version of Raspberry Pi OS.",
                   "Or pick a different app/image that publishes your architecture."],
                  _grab(text, r"exec format|manifest|platform"))

    # 5. data folder on a filesystem without Unix permissions
    if (re.search(r"operation not permitted", low) and re.search(r"chown|chmod|data directory|initdb", low)) or \
       re.search(r"could not change permissions of directory|data directory .* has (wrong ownership|invalid permissions)|"
                 r"initdb: error: could not|permissions? .* (too open|must be)", low):
        return _d("fs_perm", "The app's data folder is on a drive that can't hold Linux permissions",
                  "The app tried to set ownership or permissions on its data folder and the drive refused. This is "
                  "typical for exFAT, FAT32, NTFS and some network shares, which databases can't use.",
                  ["Keep the app's data on an ext4 (Linux) disk. In Void OS that is the folder set by "
                   "VOID_APP_DATA_DIR / VOID_FILE_ROOT.",
                   "Format a USB drive as ext4 if you want to use one, and mount it before starting Void OS.",
                   "Never put a database on a network share."],
                  _grab(text, r"operation not permitted|permission|chown|chmod|initdb"))

    # 6. disk full
    if "no space left on device" in low:
        return _d("disk_full", "The disk is full",
                  "The container couldn't write because there is no space left on the drive it uses.",
                  ["Free some space (Docker images and old containers: `docker system prune`), or move the app's data "
                   "to a bigger drive.", "On a Raspberry Pi the SD card is often the one that fills up."],
                  _grab(text, r"no space left"))

    # 7. port already used
    if re.search(r"address already in use|port is already allocated|bind: .*in use", low):
        return _d("port", "A port it needs is already taken",
                  "Another program or container is using a port this app tries to open.",
                  ["Stop whatever uses that port, or reinstall the app so Void OS picks a free one."],
                  _grab(text, r"already in use|already allocated"))

    # 8. another container it needs is down
    if re.search(r"enotfound|econnrefused|connect etimedout|connect_timeout|could not translate host name|"
                 r"name or service not known|connection refused|can't reach database|no route to host", low):
        return _d("dependency", "It can't reach another container it depends on",
                  "The app is up but keeps failing to connect to a service it needs (usually its database or cache), "
                  "so that other container is probably the real problem.",
                  ["Open the app's other containers and find the one that is restarting or stopped. Fix that one first; "
                   "this one will then start by itself.",
                   "Use 'Why?' on that container."],
                  _grab(text, r"enotfound|econnrefused|etimedout|translate host|connection refused"), "warn")

    # 9. database from another major version
    if "database files are incompatible with server" in low:
        return _d("db_version", "The database folder was made by a different database version",
                  "The stored database files are from a different major PostgreSQL version than the one now running.",
                  ["Use the image tag the data was created with, or export and re-import the data."],
                  _grab(text, r"incompatible"))

    # 10. a folder it can't write to
    if "permission denied" in low:
        return _d("perm", "It can't write to a folder it was given",
                  "A folder mounted into the container isn't writable by the user the app runs as.",
                  ["Check the owner of the app's data folder (`ls -ld <folder>`), or fix it with `sudo chown -R 1000:1000 <folder>`."],
                  _grab(text, r"permission denied"))

    # nothing recognised
    names = {1: "an error", 2: "a usage error", 125: "Docker couldn't start it", 126: "a command that can't run",
             127: "a command that wasn't found", 133: "a crash (SIGTRAP/abort)", 139: "a crash (segmentation fault)",
             143: "being asked to stop (SIGTERM)"}
    if code in (None, 0) and not text:
        return _d("unknown", "No problem found",
                  "Docker doesn't report an error for this container and it has printed nothing.",
                  ["If it keeps restarting, open its Logs for the full output."], [], "info")
    return _d("unknown", "It stopped and the cause isn't one Void OS recognises",
              f"The container exited with code {code}" + (f", {names[code]}" if code in names else "") + ".",
              ["Read the last log lines below. The first error line is usually the real reason; "
               "later ones are often consequences."], _grab(text, r"error|fatal|panic|fail|exception", 5))


def diagnose_container(c) -> dict:
    """Diagnosis for a docker-py container object, including how it is behaving right now."""
    try:
        c.reload()
    except Exception:
        pass
    attrs = getattr(c, "attrs", {}) or {}
    st = attrs.get("State") or {}
    try:
        logs = c.logs(tail=80, timestamps=False).decode("utf-8", errors="replace")
    except Exception:
        logs = ""
    out = explain(attrs, logs)
    out.update({
        "name": c.name, "status": st.get("Status") or getattr(c, "status", ""),
        "restarting": bool(st.get("Restarting")), "restart_count": int(attrs.get("RestartCount") or 0),
        "exit_code": st.get("ExitCode"), "oom_killed": bool(st.get("OOMKilled")),
        "log_tail": "\n".join(logs.splitlines()[-25:]),
    })
    return out
