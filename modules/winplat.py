"""
modules/winplat.py — small Windows-compatibility helpers shared by
voidos.py and the other modules. Every function here is safe to call on
any OS: on Linux/macOS they're either no-ops or return the obvious POSIX
answer, so callers never need their own `if sys.platform == "win32"`
around them.

What lives here (and why it isn't inline in voidos.py):
  - no_window()            — Popen kwargs that stop a console window
                             flashing up for every helper process
                             (cloudflared, rclone, docker, PowerShell)
                             when Void OS runs without a console
                             (pythonw.exe, a service wrapper, Task Scheduler).
  - run_powershell()       — runs a script under *Windows PowerShell 5.1*
                             via -EncodedCommand (no quoting bugs), which is
                             the only PowerShell that can call WinRT APIs
                             (Bluetooth scan/pair, USB eject, radios).
  - win_name_problem() / sanitize_win_name()
                           — Windows filename rules. Without these, a
                             rename to "C:evil" turns into a drive-relative
                             path OUTSIDE FILE_ROOT, "a.txt:x" writes a
                             hidden alternate data stream, and "CON"/"NUL"
                             address devices instead of files.
  - kill_children_with_us()
                           — puts this process in a Job Object with
                             KILL_ON_JOB_CLOSE, so cloudflared/gate proxies/
                             rclone/winpty shells die with Void OS instead
                             of being orphaned (Windows never reaps children
                             when the parent exits, unlike a POSIX session).
  - respawn_self()         — the Windows path for /api/admin/restart, since
                             there's usually no systemd-style supervisor.
  - is_link_dir()          — symlink-or-junction test (Path.is_junction()
                             only exists from Python 3.12).
  - replace_with_retry()   — os.replace() that survives the brief sharing
                             violations antivirus/indexers cause on Windows.
"""
from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
import time

IS_WIN = sys.platform == "win32"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C


def no_window() -> dict:
    """Extra subprocess kwargs: hide the console window on Windows, nothing elsewhere."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


# ── PowerShell ───────────────────────────────────────────────────────────
def powershell_exe() -> str | None:
    """Windows PowerShell 5.1 specifically — PowerShell 7 (pwsh) dropped the
    `[Type,Assembly,ContentType=WindowsRuntime]` WinRT projection that the
    Bluetooth/radio scripts depend on, so pwsh is deliberately NOT used."""
    if not IS_WIN:
        return None
    root = os.environ.get("SystemRoot", r"C:\Windows")
    p = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.exists(p):
        return p
    return shutil.which("powershell.exe") or shutil.which("powershell")


_PS_PRELUDE = (
    "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;\n"
)


def run_powershell(script: str, timeout: float = 30) -> tuple[bool, str, str]:
    """(ok, stdout, stderr). Passed as -EncodedCommand (UTF-16LE base64) so no
    argument can ever be re-parsed as PowerShell syntax by the command line.
    Callers that interpolate values must still escape them with ps_quote()."""
    exe = powershell_exe()
    if not exe:
        return False, "", "Windows PowerShell (powershell.exe) not found"
    enc = base64.b64encode((_PS_PRELUDE + script).encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", enc],
                           capture_output=True, timeout=timeout, **no_window())
    except subprocess.TimeoutExpired:
        return False, "", f"PowerShell timed out after {int(timeout)}s"
    except Exception as e:
        return False, "", str(e)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    return r.returncode == 0, out, err


def ps_quote(value: str) -> str:
    """Single-quoted PowerShell string literal (only ' needs escaping, as '')."""
    return "'" + str(value).replace("'", "''") + "'"


# ── Filenames ────────────────────────────────────────────────────────────
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_WIN_BAD_CHARS = set('<>:"/\\|?*') | {chr(i) for i in range(32)}


def win_reserved(name: str) -> bool:
    return name.split(".", 1)[0].strip().upper() in _WIN_RESERVED


def win_name_problem(name: str) -> str | None:
    """Human-readable reason `name` can't be a single Windows path component,
    or None if it's fine. Only enforced on Windows (Linux allows all of these)."""
    if not IS_WIN:
        return None
    bad = sorted({c for c in name if c in _WIN_BAD_CHARS})
    if bad:
        shown = " ".join(c if c.isprintable() else "control characters" for c in bad)
        return f"Names can't contain {shown} on Windows"
    if name != name.rstrip(" ."):
        return "Names can't end with a space or a dot on Windows"
    if win_reserved(name):
        return f"'{name}' is a reserved device name on Windows"
    if len(name) > 255:
        return "Name is too long"
    return None


def sanitize_win_name(name: str) -> str:
    """Best-effort repair for names we don't get to reject (browser uploads):
    invalid characters become '_', trailing dots/spaces are trimmed, and
    reserved device names get a leading '_'. No-op off Windows."""
    if not IS_WIN:
        return name
    out = "".join("_" if c in _WIN_BAD_CHARS else c for c in name).rstrip(" .")
    if not out:
        out = "_"
    if win_reserved(out):
        out = "_" + out
    return out[:255]


# ── Links / junctions ────────────────────────────────────────────────────
def is_link_dir(path) -> bool:
    """True for a symlink or an NTFS junction (mount point reparse point)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, "st_reparse_tag", 0)
    return tag in (_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK)


def remove_link_dir(path) -> bool:
    """Removes ONLY the link itself, never what it points at. os.rmdir on a
    junction/directory-symlink deletes the reparse point and leaves the
    target untouched — the opposite of shutil.rmtree, which must never be
    pointed at one of these."""
    if not is_link_dir(path):
        return False
    try:
        os.rmdir(path)
        return True
    except OSError:
        try:
            os.unlink(path)
            return True
        except OSError:
            return False


# ── Atomic replace ───────────────────────────────────────────────────────
def restrict_to_owner(path) -> None:
    """Make a secret file readable by the current user only. POSIX: mode 600.
    Windows: drop inherited permissions and grant just the current user
    (icacls). Best effort — a failure here never blocks saving."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=15, **no_window())
    except Exception:
        pass


def replace_with_retry(src, dst, attempts: int = 12):
    """os.replace, retried on PermissionError. On Windows, Defender or the
    search indexer can hold a just-written file open for a few ms; a single
    failure there used to surface as a 500 on whatever was being saved."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WIN or i == attempts - 1:
                raise
            time.sleep(0.03 * (i + 1))


# ── Process lifetime ─────────────────────────────────────────────────────
_job_handle = None


def kill_children_with_us() -> bool:
    """Windows only: assigns this process to a Job Object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (+ BREAKAWAY_OK so respawn_self()'s
    replacement can leave it). Every child process inherits the job, so when
    Void OS exits — normally, by crash, by closing the console window, or via
    os._exit() in the restart route — Windows terminates cloudflared, gate
    proxies, rclone, winpty shells, etc. along with it. Returns True if the
    job was set up. Best-effort: failure just means the old behavior."""
    global _job_handle
    if not IS_WIN or _job_handle is not None:
        return _job_handle is not None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            return False  # e.g. already inside a job that forbids nesting (pre-Windows 8)
        _job_handle = job  # deliberately never closed — closing it is what kills the children
        return True
    except Exception:
        return False


def restart_mode() -> str:
    """How Void OS gets back up after a restart or update:
    'service'  something supervises it and starts it again (systemd's
               Restart=always, launchd KeepAlive, Docker, NSSM...) — just stop;
    'respawn'  nothing does — start a fresh copy ourselves, then stop.
    VOID_RESTART_MODE=exit|service or =respawn overrides the detection."""
    forced = os.environ.get("VOID_RESTART_MODE", "").strip().lower()
    if forced in ("exit", "service"):
        return "service"
    if forced == "respawn" or IS_WIN:
        return "respawn"
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM") or os.environ.get("XPC_SERVICE_NAME"):
        return "service"
    return "respawn"


def respawn_self(script_path: str, cwd: str, wait_pid: int | None = None) -> bool:
    """Starts a fresh copy of Void OS (new process: re-reads .env, loads the
    current code) that waits for `wait_pid` (default: this process) to exit
    before binding the port. See restart_server()."""
    env = os.environ.copy()
    env["VOID_RESPAWN_WAIT_PID"] = str(wait_pid or os.getpid())
    env.pop("VOID_GUNICORN_MASTER_PID", None)
    args = [sys.executable, script_path, *sys.argv[1:]]
    if not IS_WIN:
        try:
            subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True, close_fds=True)
            return True
        except OSError:
            return False
    flags = CREATE_BREAKAWAY_FROM_JOB
    flags |= CREATE_NEW_CONSOLE if (sys.stdout and sys.stdout.isatty()) else CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, cwd=cwd, env=env, creationflags=flags, close_fds=True)
        return True
    except OSError:
        try:  # job may not allow breakaway (nested job) — retry without it
            subprocess.Popen(args, cwd=cwd, env=env, creationflags=flags & ~CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
            return True
        except OSError:
            return False


def restart_server(script_path: str, cwd: str) -> None:
    """Restart the WHOLE server so new code and a changed .env take effect.
    Never returns. Call after stopping helper processes.

    Under Gunicorn (Linux/macOS) requests run in a worker forked from a
    master process that loaded the code at startup. Exiting only the worker
    makes the master fork a new one — from the same old in-memory code — so
    this stops the master itself (VOID_GUNICORN_MASTER_PID, set by voidos.py).
    In 'respawn' mode a fresh copy is started first and waits for the old
    master to release the port."""
    master = 0
    try:
        master = int(os.environ.get("VOID_GUNICORN_MASTER_PID") or 0)
    except ValueError:
        master = 0
    whole = master if master and master != os.getpid() else os.getpid()
    if restart_mode() == "respawn":
        respawn_self(script_path, cwd, wait_pid=whole)
    if whole != os.getpid():
        import signal
        try:
            os.kill(whole, signal.SIGTERM)   # Gunicorn: graceful stop of master and workers
        except OSError:
            pass
    os._exit(0)


def wait_for_previous_instance(timeout: float = 60) -> None:
    """Counterpart of respawn_self(), called at the top of __main__."""
    pid = os.environ.pop("VOID_RESPAWN_WAIT_PID", "")
    if not pid.isdigit():
        return
    try:
        import psutil
        psutil.Process(int(pid)).wait(timeout=timeout)
    except Exception:
        pass
