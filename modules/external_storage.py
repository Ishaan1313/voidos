"""
modules/external_storage.py — SMB/NFS network shares + USB auto-mount for
Void OS (disk/USB management, mounting network shares as local storage).

Dependency-free, host-CLI-tool pattern (same as modules/intercom.py and
modules/cloud_sync.py) — everything here shells out to standard Linux
tools rather than a pip package:
  - udisksctl (udisks2)      — USB mount/unmount, no root required
  - lsblk                    — USB device discovery
  - mount/umount (cifs-utils for SMB, nfs-common for NFS) — network shares

HONEST NOTE ON PRIVILEGES: USB mounting via udisksctl is designed to work
for an ordinary logged-in user (polkit handles the privilege boundary) and
needs no root. Mounting SMB/NFS onto an arbitrary directory is a real
kernel mount and does need root — there's no reliable, scriptable,
headless-server-friendly way around that (GVfs/`gio mount` avoids root but
depends on a D-Bus session and FUSE, which most headless server installs
simply don't have running, and would fail unpredictably rather than
working consistently). So: if this process runs as root, network shares
mount directly; if not, it tries passwordless `sudo -n mount`/`sudo -n
umount` and reports exactly what to add to sudoers if that's not set up
either — the same "detect precisely, explain precisely" philosophy this
app already uses for Bluetooth/PipeWire/rclone, rather than silently
failing or requiring root for the entire app.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCRATCH_DIR = None  # set by voidos.py to the app's own tmp/ folder — never the OS temp folder

MOUNT_TIMEOUT = 30


def _run(cmd: list, timeout=MOUNT_TIMEOUT, input_text: str | None = None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, f"{cmd[0]} not found"
    except Exception as e:
        return False, str(e)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _priv(cmd: list) -> list:
    """Prefix with non-interactive sudo if not already root — fails fast
    rather than hanging on a password prompt that will never come from a
    background thread."""
    return cmd if _is_root() else ["sudo", "-n"] + cmd


# ── Capability checks ────────────────────────────────────────────────────
def check_usb_capability() -> tuple[bool, str]:
    if not shutil.which("udisksctl"):
        return False, ("udisksctl not found — install udisks2 (sudo apt install udisks2) "
                        "for no-root USB mount/unmount support.")
    if not shutil.which("lsblk"):
        return False, "lsblk not found — install util-linux (sudo apt install util-linux)."
    return True, "udisksctl + lsblk available"


def check_mount_privilege() -> tuple[bool, str]:
    """Shared by SMB and NFS — both are real kernel mounts and need root
    one way or another."""
    if _is_root():
        return True, "running as root"
    ok, _ = _run(["sudo", "-n", "-l", "mount"], timeout=5)
    if ok:
        return True, "passwordless sudo available for mount/umount"
    return False, ("Network share mounting needs root — this process isn't running as root and "
                    "passwordless sudo isn't set up for it. Either run Void OS as root, or add a "
                    "sudoers rule scoped to just mount/umount, e.g.:\n"
                    "  echo 'voidos ALL=(root) NOPASSWD: /bin/mount, /bin/umount' | "
                    "sudo tee /etc/sudoers.d/voidos-mount\n"
                    "(replace 'voidos' with the actual user Void OS runs as)")


def check_smb_capability() -> tuple[bool, str]:
    ok, msg = check_mount_privilege()
    if not ok:
        return False, msg
    if not shutil.which("mount.cifs") and not os.path.exists("/sbin/mount.cifs"):
        return False, "cifs-utils not installed — sudo apt install cifs-utils"
    return True, "ready"


def check_nfs_capability() -> tuple[bool, str]:
    ok, msg = check_mount_privilege()
    if not ok:
        return False, msg
    if not shutil.which("mount.nfs") and not os.path.exists("/sbin/mount.nfs"):
        return False, "nfs-common not installed — sudo apt install nfs-common"
    return True, "ready"


# ── USB ──────────────────────────────────────────────────────────────────
def list_usb_devices() -> list[dict]:
    """Removable block devices (RM=1) or USB-transport devices, partitions
    only (a raw unpartitioned disk isn't mountable directly in the common
    case)."""
    ok, out = _run(["lsblk", "-J", "-b", "-o",
                     "NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT,RM,TRAN,UUID,TYPE"], timeout=10)
    if not ok:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    out_devices = []

    def walk(devices, parent_removable=False):
        for d in devices:
            removable = d.get("rm") in (True, "1", 1) or parent_removable
            is_usb = d.get("tran") == "usb"
            whole_fs = d.get("type") == "disk" and d.get("fstype") and not d.get("children")
            if (d.get("type") == "part" or whole_fs) and (removable or is_usb):
                out_devices.append({
                    "device": "/dev/" + d["name"],
                    "size": d.get("size"),
                    "fstype": d.get("fstype"),
                    "label": d.get("label") or d["name"],
                    "mountpoint": d.get("mountpoint"),
                    "uuid": d.get("uuid"),
                })
            if d.get("children"):
                walk(d["children"], removable or is_usb)

    walk(data.get("blockdevices", []))
    return out_devices


# ── Repair after an unclean shutdown ─────────────────────────────────
# A power cut leaves a drive "dirty": the file system was never closed properly, and mounting it fails
# with messages such as "structure needs cleaning" or "volume is dirty". Only the *safe, automatic*
# modes of each file system's own checker are used (the ones meant for unattended boot-time repair):
# nothing here ever answers "yes" to a risky question, deletes the journal or forces a rewrite. Anything
# the safe mode can't fix is left alone and reported, with the drive untouched.
_CORRUPT_HINTS = (
    "structure needs cleaning", "needs cleaning", "unclean", "dirty", "corrupt", "bad superblock",
    "can't read superblock", "cannot read superblock", "wrong fs type", "unknown filesystem type",
    "not cleanly unmounted", "was not properly unmounted", "inconsistent", "journal", "input/output error",
    "metadata", "hibernat", "fast restart",
)
REPAIR_TIMEOUT = 45 * 60          # a large drive can take a long time; this only stops a hung checker


def looks_corrupt(message: str) -> bool:
    m = (message or "").lower()
    return any(h in m for h in _CORRUPT_HINTS)


def repair_plan(fstype: str | None):
    """(tool name(s), safe arguments, exit codes that mean "fixed or clean") for a file system, or None
    when there is no safe unattended repair for it."""
    f = (fstype or "").lower()
    if f in ("ext2", "ext3", "ext4"):
        return (("e2fsck",), ["-p"], {0, 1, 2})          # -p: preen = only fixes that are always safe
    if f in ("vfat", "fat", "fat16", "fat32", "msdos"):
        return (("fsck.vfat", "dosfsck", "fsck.fat"), ["-a"], {0, 1})   # -a: automatic, safe repairs
    if f == "exfat":
        return (("fsck.exfat", "exfatfsck"), ["-p"], {0, 1})
    if f in ("ntfs", "ntfs3", "ntfs-3g", "fuseblk"):
        return (("ntfsfix",), ["-d"], {0})               # -d: clear the dirty flag and replay the log
    return None


def _is_mounted_device(device: str) -> bool:
    ok, out = _run(["lsblk", "-n", "-o", "MOUNTPOINT", device], timeout=10)
    return ok and any(line.strip() for line in out.splitlines())


def repair_usb(device: str, fstype: str | None) -> tuple[bool, str]:
    """Run the safe checker on an UNMOUNTED removable partition. Returns (fixed_or_clean, plain summary)."""
    if sys.platform == "win32":
        return _repair_usb_windows(device)
    plan = repair_plan(fstype)
    if not plan:
        return False, (f"This drive uses {fstype or 'an unknown file system'}, which can't be repaired "
                       "automatically and safely. Check it on a computer with that file system's own tools.")
    tools, args, good = plan
    exe = next((t for t in tools if shutil.which(t)), None)
    if not exe:
        return False, (f"The repair tool for {fstype} ({tools[0]}) isn't installed. "
                       "Install it (e2fsprogs / dosfstools / exfatprogs / ntfs-3g) and plug the drive in again.")
    if _is_mounted_device(device):
        return False, "The drive is mounted, so it can't be checked safely right now."
    cmd = _priv([exe] + args + [device])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=REPAIR_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "The repair took too long and was stopped. The drive was not changed further."
    except Exception as e:
        return False, f"The repair could not start: {e}"
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    tail = " ".join(text.split())[-240:]
    low = text.lower()
    # sudo's own refusal comes first: it also exits with 1, which e2fsck uses for "errors were fixed"
    if (low.startswith("sudo:") or "\nsudo:" in low) and any(k in low for k in (
            "password is required", "a terminal is required", "not in the sudoers", "may not run sudo",
            "is not allowed to execute", "no tty present")):
        return False, ("Repairing a drive needs administrator rights. Run the installer again with --with mount, "
                       "or run Void OS as root.")
    if r.returncode in good:
        return True, f"Repaired with {exe} ({fstype})." + (f" {tail}" if tail and r.returncode else "")
    return False, f"{exe} couldn't repair it automatically (exit {r.returncode}). {tail}".strip()


def _repair_usb_windows(device: str) -> tuple[bool, str]:
    m = re.fullmatch(r"([A-Za-z]):", device or "")
    if not m:
        return False, "Windows can only repair a drive that has a letter. Give it one in Disk Management, then try again."
    letter = m.group(1).upper()
    # chkdsk /f fixes file-system errors; /x dismounts the volume first so it never prompts.
    try:
        r = subprocess.run(["chkdsk", f"{letter}:", "/f", "/x"], capture_output=True, text=True,
                           timeout=REPAIR_TIMEOUT, input="n\n")
    except subprocess.TimeoutExpired:
        return False, "The check took too long and was stopped."
    except Exception as e:
        return False, f"The check could not start: {e}"
    text = " ".join(((r.stdout or "") + (r.stderr or "")).split())
    if "access denied" in text.lower() or "sufficient privileges" in text.lower():
        return False, "Windows needs Void OS to run as Administrator to repair a drive."
    if r.returncode in (0, 1):
        return True, f"Checked and repaired {letter}: with chkdsk."
    return False, f"chkdsk couldn't repair {letter}: automatically (exit {r.returncode}). {text[-200:]}"


def mount_usb(device: str) -> tuple[bool, str]:
    ok, out = _run(["udisksctl", "mount", "-b", device, "--no-user-interaction"])
    if ok:
        m = re.search(r"at (.+?)\.?\s*$", out.strip())
        return True, m.group(1) if m else out.strip()
    if "AlreadyMounted" in out:
        return True, "already mounted"
    return False, out.strip()


def unmount_usb(device: str) -> tuple[bool, str]:
    ok, out = _run(["udisksctl", "unmount", "-b", device, "--no-user-interaction"])
    return ok, out.strip()


# ── mounting straight into a folder we choose ───────────────────────────
# udisksctl decides the mount point itself (under /media/<user>/) and, for a background service that isn't
# in a desktop login session, polkit usually refuses it ("not authorized"), which is why automounting could
# silently do nothing. A plain `sudo mount` into a folder of our own needs only the mount/umount sudo rule
# the installer already sets up, works for a service, and lets the folder be configured.
_FORBIDDEN_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/root", "/sbin",
                   "/sys", "/usr", "/var")


def validate_mount_dir(path: str, protect: tuple = ()) -> tuple[bool, str, str]:
    """(ok, message, normalised path) for a folder drives may be mounted under."""
    p = (path or "").strip()
    if not p:
        return True, "", ""
    if "\x00" in p or not os.path.isabs(p):
        return False, "Use a full path that starts with / (for example /mnt/drives).", ""
    n = os.path.normpath(p)
    if n == "/" or any(n == f or n.startswith(f + "/") for f in _FORBIDDEN_DIRS):
        return False, f"{n} is a system folder; pick somewhere like /mnt/drives or /srv/drives.", ""
    if n == "/run" or (n.startswith("/run/") and not n.startswith("/run/media")):
        return False, "Pick a folder outside /run (it is cleared at every restart).", ""
    for q in protect:
        q = os.path.normpath(str(q))
        if n == q or n.startswith(q + os.sep):
            return False, f"Drives can't be mounted inside {q}.", ""
    try:
        os.makedirs(n, exist_ok=True)
    except OSError:
        return False, (f"Void OS can't create {n}. Create it once yourself and give it to the Void OS user: "
                       f"sudo mkdir -p {n} && sudo chown $USER {n}"), ""
    if not os.path.isdir(n) or not os.access(n, os.W_OK | os.X_OK):
        return False, f"{n} isn't a folder the Void OS user can write to (sudo chown $USER {n}).", ""
    return True, "", n


def _mount_opts(fstype: str | None):
    """(file system type or None to auto-detect, mount options or None) so the drive's files belong to the
    Void OS user. ext4, btrfs and xfs keep the permissions stored on the disk."""
    uid, gid = os.getuid(), os.getgid()
    ft = (fstype or "").lower()
    base = "nosuid,nodev"
    if ft in ("vfat", "fat", "msdos"):
        return "vfat", f"{base},uid={uid},gid={gid},umask=022,flush,utf8"
    if ft == "exfat":
        return "exfat", f"{base},uid={uid},gid={gid},umask=022"
    if ft in ("ntfs", "ntfs3"):
        t = "ntfs-3g" if (shutil.which("mount.ntfs-3g") or os.path.exists("/sbin/mount.ntfs-3g")) else "ntfs3"
        return t, f"{base},uid={uid},gid={gid},umask=022"
    return (ft or None), base


def mount_at(device: str, fstype: str | None, target: str) -> tuple[bool, str]:
    """Mount `device` at `target` with `sudo mount`. Returns (ok, message)."""
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return False, f"Couldn't create {target}: {e}"
    if os.path.ismount(target):
        return False, f"{target} already has something mounted on it."
    t, opts = _mount_opts(fstype)
    cmd = ["mount"] + (["-t", t] if t else []) + (["-o", opts] if opts else []) + [device, target]
    ok, out = _run(_priv(cmd))
    if not ok:
        try:
            os.rmdir(target)
        except OSError:
            pass
    return ok, out.strip()


def umount_at(target: str) -> tuple[bool, str]:
    ok, out = _run(_priv(["umount", target]))
    if ok:
        try:
            os.rmdir(target)
        except OSError:
            pass
    return ok, out.strip()


def bind_mount(real_path: str, target_path: str) -> tuple[bool, str]:
    """Makes an already-mounted location (USB's own OS-chosen mount point,
    which udisksctl controls and can't be redirected) additionally appear
    at `target_path` — specifically so it can live inside FILE_ROOT and
    the existing file browser (_safe(), upload, rename, everything) just
    works on it for free, with zero changes to that code. Needs the same
    mount privilege as SMB/NFS — see check_mount_privilege()."""
    os.makedirs(target_path, exist_ok=True)
    ok, out = _run(_priv(["mount", "--bind", real_path, target_path]))
    return ok, out.strip()


def unbind_mount(target_path: str) -> tuple[bool, str]:
    ok, out = _run(_priv(["umount", target_path]))
    return ok, out.strip()


_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])$")


def valid_host(server: str) -> bool:
    """A host name or IP address that can safely sit in a mount command: no leading '-', so it can
    never be read as an option by `mount`, and no spaces or separators."""
    return bool(_HOST_RE.fullmatch(server or ""))


# ── SMB ──────────────────────────────────────────────────────────────────
def mount_smb(server: str, share: str, mount_point: str, username: str = "",
              password: str = "", domain: str = "") -> tuple[bool, str]:
    if not valid_host(server):
        return False, "That isn't a valid server name or address."
    os.makedirs(mount_point, exist_ok=True)
    unc = f"//{server}/{share}"
    opts = ["iocharset=utf8", "vers=3.0"]
    cred_file = None
    if username:
        if any(c in (username + password + domain) for c in "\r\n\x00"):
            return False, "The username, password and domain can't contain line breaks."
        # Credentials go in a short-lived owner-only file instead of the command line:
        # they'd be visible to every local user in the process list, and a comma in a
        # password would split it into extra mount options.
        fd, cred_file = tempfile.mkstemp(prefix="smbcred-", dir=str(SCRATCH_DIR) if SCRATCH_DIR else None)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"username={username}\npassword={password}\n" + (f"domain={domain}\n" if domain else ""))
        os.chmod(cred_file, 0o600)
        opts.append(f"credentials={cred_file}")
    else:
        opts.append("guest")
    try:
        ok, out = _run(_priv(["mount", "-t", "cifs", unc, mount_point, "-o", ",".join(opts)]))
    finally:
        if cred_file:
            try: os.unlink(cred_file)
            except OSError: pass
    return ok, out.strip() if not ok else "mounted"


def unmount_smb(mount_point: str) -> tuple[bool, str]:
    ok, out = _run(_priv(["umount", mount_point]))
    return ok, out.strip()


# ── NFS ──────────────────────────────────────────────────────────────────
def mount_nfs(server: str, export: str, mount_point: str) -> tuple[bool, str]:
    if not valid_host(server):
        return False, "That isn't a valid server name or address."
    os.makedirs(mount_point, exist_ok=True)
    src = f"{server}:{export}"
    cmd = _priv(["mount", "-t", "nfs", "--", src, mount_point])
    ok, out = _run(cmd)
    return ok, out.strip() if not ok else "mounted"


def unmount_nfs(mount_point: str) -> tuple[bool, str]:
    ok, out = _run(_priv(["umount", mount_point]))
    return ok, out.strip()


# ── Shared helpers voidos.py uses on every OS ─────────────────────────────
_DEVICE_RE_POSIX = re.compile(r"^/dev/[a-zA-Z0-9]+$")


def valid_device(device: str) -> bool:
    return bool(_DEVICE_RE_POSIX.match(device or ""))


def is_mounted(path) -> bool:
    """`Path.is_mount()` without the platform landmines: it raises
    NotImplementedError on Windows before Python 3.12, and on Windows the
    "mount" is a junction/symlink anyway (see the Windows section below)."""
    try:
        return os.path.exists(path) and os.path.ismount(path)
    except (OSError, NotImplementedError):
        return False


def check_bind_privilege() -> tuple[bool, str]:
    """Whether a mounted USB drive can additionally be made browsable inside
    FILE_ROOT/external. Linux: a bind mount, same privilege as SMB/NFS."""
    return check_mount_privilege()


# ══════════════════════════════════════════════════════════════════════
# WINDOWS BACKEND
# Windows has no bind mounts, no udisks and no sudo, so the same public
# functions are re-implemented below on Windows primitives. The contract
# voidos.py relies on is unchanged: whatever is "mounted" ends up reachable
# at FILE_ROOT/external/<slug>, where the existing Files app, uploads,
# share links and WebDAV all just work on it.
#
#   USB   — Windows already auto-mounts USB volumes to a drive letter.
#           "Mount" = make sure it has a letter (assigning one if it has
#           none, which needs admin); "bind" = an NTFS *junction* from
#           external/<slug> to E:\ (junctions need no admin rights);
#           "unmount" = remove the junction, then the Shell "Eject" verb
#           (the same thing Explorer's Eject menu item does, no admin).
#   SMB   — `net use \\server\share` (no drive letter consumed) to
#           establish the credentials, then a *directory symlink* from
#           external/<slug> to the UNC path. Junctions can't point at
#           network paths, so this needs symlink rights: Developer Mode
#           (Settings → For developers) or running Void OS as admin.
#   NFS   — Windows' optional "Client for NFS" feature (Pro/Enterprise/
#           Server editions) provides mount.exe/umount.exe; same symlink
#           approach as SMB on top of it.
#
# _safe() in modules/file_access.py has a matching, deliberately narrow exception that
# lets paths under one of these links resolve outside FILE_ROOT (see
# _external_link_path there) — that's the only reason these links are
# usable from the Files app at all.
# ══════════════════════════════════════════════════════════════════════
if sys.platform == "win32":
    import tempfile
    from modules import winplat

    _DEVICE_RE_WIN = re.compile(r"^(?:[A-Za-z]:|disk\d{1,3}part\d{1,3})$")
    _symlink_ok_cache: list = []

    def valid_device(device: str) -> bool:  # noqa: F811 — intentional per-OS override
        return bool(_DEVICE_RE_WIN.match(device or ""))

    def is_mounted(path) -> bool:  # noqa: F811
        return winplat.is_link_dir(path) and os.path.exists(path)

    def _win_run(cmd: list, timeout=MOUNT_TIMEOUT, input_text: str | None = None):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, input=(input_text or "").encode(),
                               **winplat.no_window())
            out = (r.stdout or b"") + (r.stderr or b"")
            return r.returncode == 0, out.decode("oem", "replace")  # console tools print in the OEM code page
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except FileNotFoundError:
            return False, f"{cmd[0]} not found"
        except Exception as e:
            return False, str(e)

    def _can_symlink() -> bool:
        if _symlink_ok_cache:
            return _symlink_ok_cache[0]
        ok = False
        try:
            d = tempfile.mkdtemp(prefix="voidos-symtest-", dir=str(SCRATCH_DIR) if SCRATCH_DIR else None)
            try:
                os.symlink(d, os.path.join(d, "l"), target_is_directory=True)
                ok = True
            finally:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            ok = False
        _symlink_ok_cache.append(ok)
        return ok

    _SYMLINK_HELP = ("Mounting network shares on Windows uses a directory symlink, which needs either "
                     "Developer Mode (Settings → System → For developers → Developer Mode) or running "
                     "Void OS as Administrator. Restart Void OS after turning Developer Mode on.")

    def check_mount_privilege() -> tuple[bool, str]:  # noqa: F811
        if _can_symlink():
            return True, "symlink creation allowed"
        return False, _SYMLINK_HELP

    def check_bind_privilege() -> tuple[bool, str]:  # noqa: F811
        return True, "NTFS junctions need no extra privilege"

    def check_usb_capability() -> tuple[bool, str]:  # noqa: F811
        return True, "Windows auto-mounts USB volumes; Void OS links them into external/"

    def check_smb_capability() -> tuple[bool, str]:  # noqa: F811
        if not shutil.which("net"):
            return False, "net.exe not found on PATH"
        return check_mount_privilege()

    def _nfs_bin(name: str) -> str | None:
        p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name)
        return p if os.path.exists(p) else None

    def check_nfs_capability() -> tuple[bool, str]:  # noqa: F811
        if not _nfs_bin("mount.exe"):
            return False, ("Windows' Client for NFS isn't installed — enable it under "
                           "'Turn Windows features on or off' → Services for NFS → Client for NFS "
                           "(Windows Pro/Enterprise/Server only), or in an admin PowerShell: "
                           "Enable-WindowsOptionalFeature -Online -FeatureName ServicesForNFS-ClientOnly,ClientForNFS-Infrastructure")
        return check_mount_privilege()

    # ── USB ──
    _USB_PS = r"""
$out = @()
try {
  foreach ($d in (Get-Disk | Where-Object { $_.BusType -eq 'USB' })) {
    foreach ($p in ($d | Get-Partition -ErrorAction SilentlyContinue)) {
      if ($p.Type -eq 'Reserved') { continue }
      $v = $p | Get-Volume -ErrorAction SilentlyContinue
      $letter = ''
      if ($p.DriveLetter -and [int][char]$p.DriveLetter -ne 0) { $letter = [string]$p.DriveLetter + ':' }
      $out += [pscustomobject]@{ letter=$letter; disk=[int]$d.Number; part=[int]$p.PartitionNumber;
        size=[int64]$p.Size; fs=[string]$v.FileSystem; label=[string]$v.FileSystemLabel; uuid=[string]$v.UniqueId }
    }
  }
} catch { }
ConvertTo-Json -Compress -InputObject @($out)
"""

    def list_usb_devices() -> list[dict]:  # noqa: F811
        devices, seen = [], set()
        ok, out, _ = winplat.run_powershell(_USB_PS, timeout=20)
        if ok and out:
            try:
                rows = json.loads(out)
                rows = rows if isinstance(rows, list) else [rows]
            except ValueError:
                rows = []
            for r in rows:
                letter = (r.get("letter") or "").upper()
                if not letter and not r.get("fs"):
                    continue  # unformatted/unknown partition — nothing to mount
                dev = letter or f"disk{r.get('disk')}part{r.get('part')}"
                seen.add(letter)
                devices.append({"device": dev, "size": r.get("size"), "fstype": (r.get("fs") or "").lower() or None,
                                "label": r.get("label") or dev.rstrip(":"),
                                "mountpoint": (letter + "\\") if letter else None, "uuid": r.get("uuid") or None})
        # Fallback/extra: anything Windows itself flags as removable (SD card
        # readers, thumb drives on hosts where Get-Disk needs elevation).
        try:
            import psutil
            for p in psutil.disk_partitions(all=False):
                letter = p.device.rstrip("\\").upper()
                if "removable" in (p.opts or "") and letter not in seen and p.fstype:
                    seen.add(letter)
                    label = ""
                    try:
                        import ctypes
                        buf = ctypes.create_unicode_buffer(261)
                        if ctypes.windll.kernel32.GetVolumeInformationW(p.mountpoint, buf, 261, None, None, None, None, 0):
                            label = buf.value
                    except Exception:
                        pass
                    size = None
                    try:
                        size = psutil.disk_usage(p.mountpoint).total
                    except Exception:
                        pass
                    devices.append({"device": letter, "size": size, "fstype": p.fstype.lower(),
                                    "label": label or letter.rstrip(":"), "mountpoint": p.mountpoint, "uuid": None})
        except Exception:
            pass
        return devices

    def mount_usb(device: str) -> tuple[bool, str]:  # noqa: F811
        if not valid_device(device):
            return False, "Invalid device"
        if re.match(r"^[A-Za-z]:$", device):
            root = device.upper() + "\\"
            return (True, root) if os.path.exists(root) else (False, f"{device} isn't available")
        m = re.match(r"^disk(\d+)part(\d+)$", device)
        script = (f"Add-PartitionAccessPath -DiskNumber {int(m.group(1))} -PartitionNumber {int(m.group(2))} -AssignDriveLetter;"
                  f"$p = Get-Partition -DiskNumber {int(m.group(1))} -PartitionNumber {int(m.group(2))};"
                  "[string]$p.DriveLetter")
        ok, out, err = winplat.run_powershell(script, timeout=30)
        letter = (out.strip()[-1:] if ok and out.strip() else "")
        if ok and letter.isalpha():
            return True, letter.upper() + ":\\"
        return False, ("Couldn't assign a drive letter (this needs Void OS to run as Administrator): "
                       + (err or out or "unknown error")[:300])

    _EJECT_PS = r"""
$sh = New-Object -ComObject Shell.Application
$item = $sh.Namespace(17).ParseName(%s)
if ($null -eq $item) { 'missing' } else { $item.InvokeVerb('Eject'); 'ok' }
"""

    def unmount_usb(device: str) -> tuple[bool, str]:  # noqa: F811
        if not re.match(r"^[A-Za-z]:$", device or ""):
            return False, "Only drives with a letter can be ejected"
        letter = device.upper()
        ok, out, err = winplat.run_powershell(_EJECT_PS % winplat.ps_quote(letter), timeout=30)
        if not ok:
            return False, (err or out or "eject failed")[:300]
        for _ in range(20):  # the verb returns before Windows finishes flushing/removing the volume
            if not os.path.exists(letter + "\\"):
                return True, "ejected — safe to remove"
            time.sleep(0.25)
        return False, (f"Windows didn't release {letter} — a program (or Void OS's own Files view) "
                       "may still have a file open on it. Close it and try again.")

    def _make_junction(real_path: str, target_path: str) -> tuple[bool, str]:
        if os.path.lexists(target_path):
            if winplat.is_link_dir(target_path):
                winplat.remove_link_dir(target_path)
            else:
                try:
                    os.rmdir(target_path)  # only ever an empty placeholder dir — never rmtree here
                except OSError:
                    return False, f"{target_path} already exists and isn't empty"
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        try:
            import _winapi
            _winapi.CreateJunction(real_path, target_path)
            return True, "linked"
        except Exception:
            ok, out = _win_run(["cmd", "/c", "mklink", "/J", target_path, real_path], timeout=15)
            return ok, out.strip()

    def bind_mount(real_path: str, target_path: str) -> tuple[bool, str]:  # noqa: F811
        return _make_junction(real_path, target_path)

    def unbind_mount(target_path: str) -> tuple[bool, str]:  # noqa: F811
        if not winplat.is_link_dir(target_path):
            return True, "not linked"
        return (True, "unlinked") if winplat.remove_link_dir(target_path) else (False, "couldn't remove link")

    # ── SMB / NFS ──
    def _unc(server: str, share: str) -> str:
        server = server.strip().strip("\\/")
        share = share.strip().strip("\\/").replace("/", "\\")
        return f"\\\\{server}\\{share}"

    def _link_to(target_path: str, unc: str) -> tuple[bool, str]:
        if os.path.lexists(target_path):
            if winplat.is_link_dir(target_path):
                winplat.remove_link_dir(target_path)
            else:
                try:
                    os.rmdir(target_path)
                except OSError:
                    return False, f"{target_path} already exists and isn't empty"
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        try:
            os.symlink(unc, target_path, target_is_directory=True)
            return True, "mounted"
        except OSError as e:
            return False, f"{e}. {_SYMLINK_HELP}"

    def _link_unc(target_path: str) -> str | None:
        try:
            t = os.readlink(target_path)
        except OSError:
            return None
        if t.startswith("\\\\?\\UNC\\"):
            t = "\\\\" + t[8:]
        return t if t.startswith("\\\\") else None

    def _bad_arg(*vals) -> bool:
        # net.exe parses its own "/switch" syntax — a value starting with "/"
        # would be read as a switch rather than data. Reject rather than guess.
        return any(str(v).startswith("/") or "\x00" in str(v) for v in vals if v)

    def mount_smb(server: str, share: str, mount_point: str, username: str = "",  # noqa: F811
                  password: str = "", domain: str = "") -> tuple[bool, str]:
        if _bad_arg(server, share, username, domain):
            return False, "Server, share and username can't start with '/'"
        unc = _unc(server, share)
        cmd = ["net", "use", unc]
        if username:
            cmd += [password if password else "", f"/user:{domain}\\{username}" if domain else f"/user:{username}"]
        cmd += ["/persistent:no"]
        ok, out = _win_run(cmd, input_text="\n")  # stdin closed off so net.exe can never block on a prompt
        if not ok and "1219" not in out:  # 1219 = already connected with other credentials — usable as-is
            return False, out.strip() or "net use failed"
        return _link_to(mount_point, unc)

    def unmount_smb(mount_point: str) -> tuple[bool, str]:  # noqa: F811
        unc = _link_unc(mount_point)
        winplat.remove_link_dir(mount_point)
        if unc:
            _win_run(["net", "use", unc, "/delete", "/y"], timeout=15)
        return True, "unmounted"

    def mount_nfs(server: str, export: str, mount_point: str) -> tuple[bool, str]:  # noqa: F811
        mnt = _nfs_bin("mount.exe")
        if not mnt:
            return False, check_nfs_capability()[1]
        if _bad_arg(server):
            return False, "Invalid server"
        unc = _unc(server, export)
        ok, out = _win_run([mnt, "-o", "anon", unc, "*"])
        if not ok and "already" not in out.lower():
            return False, out.strip() or "mount.exe failed"
        return _link_to(mount_point, unc)

    def unmount_nfs(mount_point: str) -> tuple[bool, str]:  # noqa: F811
        unc = _link_unc(mount_point)
        winplat.remove_link_dir(mount_point)
        umnt = _nfs_bin("umount.exe")
        if unc and umnt:
            _win_run([umnt, "-f", unc], timeout=15)
        return True, "unmounted"
