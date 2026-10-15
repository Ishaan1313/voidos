"""
modules/errors.py — turns exceptions into messages a person can act on.

Before this, any unexpected failure reached the browser as "Something went
wrong processing that request" — true, but useless: a full disk, a
read-only USB stick, a file locked by another program and a name the
drive's filesystem can't store all looked identical. friendly() maps the
exception to:

    (http_status, code, message, hint)

  code     stable machine-readable id ("disk_full", "in_use", ...) the
           frontend can key off and admins can grep the error log for
  message  one sentence describing what went wrong, in plain language
  hint     what to do about it (may be "")

Only *categories* are described — never raw exception text, paths outside
the user's view, or library internals — so these are safe to show any
signed-in user. The raw exception and traceback still go to the server
log and the admin Error Log, keyed by the same reference code.
"""
from __future__ import annotations

import errno
import socket
import sys

# (status, code, message, hint)
Friendly = tuple

_ERRNO_MAP = {
    errno.ENOSPC: (507, "disk_full", "There isn't enough free space on the drive.",
                   "Free up some space (empty the Recycle Bin, delete large files) or choose another drive."),
    errno.EDQUOT if hasattr(errno, "EDQUOT") else -1001:
        (507, "quota_exceeded", "The drive's storage quota has been reached.",
         "Free up space or ask the server owner to raise the quota."),
    errno.EACCES: (403, "os_permission", "Void OS isn't allowed to access that location on the server.",
                   "The folder's permissions on the server don't allow it — the server owner needs to fix "
                   "ownership/permissions for the account Void OS runs as."),
    errno.EPERM: (403, "os_permission", "The operating system refused that operation.",
                  "The file may be protected, or Void OS lacks the privileges for it."),
    errno.ENOENT: (404, "not_found", "That file or folder no longer exists.",
                   "It may have been moved, renamed or deleted — refresh and try again."),
    errno.EEXIST: (409, "exists", "Something with that name already exists there.",
                   "Choose a different name, or delete/rename the existing item first."),
    errno.ENOTEMPTY: (409, "not_empty", "That folder isn't empty.", ""),
    errno.EISDIR: (409, "is_dir", "That's a folder, not a file.", ""),
    errno.ENOTDIR: (409, "not_dir", "Part of that path is a file, not a folder.", ""),
    errno.ENAMETOOLONG: (400, "name_too_long", "That name (or the full path) is too long for this drive.",
                         "Shorten the file name or move it to a less deeply nested folder."),
    errno.EROFS: (403, "read_only", "That drive is read-only.",
                  "It may be write-protected, mounted read-only, or the filesystem was remounted read-only "
                  "after an error."),
    errno.EBUSY: (423, "in_use", "That file or drive is in use.",
                  "Close any program using it (including other Void OS tabs) and try again."),
    errno.ETXTBSY: (423, "in_use", "That file is in use by a running program.", ""),
    errno.EIO: (500, "io_error", "The drive reported a read/write error.",
                "It may be failing or was unplugged — check the drive and its cable."),
    errno.ENXIO: (500, "device_gone", "The drive isn't available any more.",
                  "It may have been unplugged or gone to sleep."),
    errno.ENODEV: (500, "device_gone", "The drive isn't available any more.", ""),
    errno.EXDEV: (500, "cross_device", "That can't be done across different drives in one step.", ""),
    errno.EMFILE: (503, "busy", "The server is temporarily out of file handles.", "Try again in a moment."),
    errno.ENFILE: (503, "busy", "The server is temporarily out of file handles.", "Try again in a moment."),
    errno.ENOMEM: (503, "out_of_memory", "The server ran out of memory.", "Try again, or with fewer files."),
    errno.EFBIG: (413, "too_big", "That file is too large for the destination drive.",
                  "FAT32-formatted drives can't hold files over 4 GB — reformat as exFAT/NTFS or split the file."),
    errno.EINVAL: (400, "invalid_name", "The drive won't accept that name or operation.",
                   "Some drives (FAT/exFAT) don't allow characters like : * ? \" < > | in names."),
    errno.ELOOP: (400, "link_loop", "That path loops back on itself through links.", ""),
}

# Windows: OSError.winerror is more specific than the mapped errno.
_WINERROR_MAP = {
    5: _ERRNO_MAP[errno.EACCES],
    32: (423, "in_use", "That file is open in another program.",
         "Close it there (or in Explorer's preview pane) and try again."),
    33: (423, "in_use", "Part of that file is locked by another program.", ""),
    112: _ERRNO_MAP[errno.ENOSPC],
    123: (400, "invalid_name", "Windows doesn't allow that name.",
          "Names can't contain < > : \" / \\ | ? * or end with a dot or space."),
    206: _ERRNO_MAP[errno.ENAMETOOLONG],
    1224: (423, "in_use", "That file is mapped by another program.", ""),
    21: (500, "device_gone", "The drive isn't ready.", "It may have been ejected or gone to sleep."),
    1117: _ERRNO_MAP[errno.EIO],
    145: _ERRNO_MAP[errno.ENOTEMPTY],
    183: _ERRNO_MAP[errno.EEXIST],
}

_DOCKER_PATTERNS = [
    ("port is already allocated", 409, "port_in_use",
     "That port is already in use by another container or program.",
     "Pick a different host port, or stop whatever is using it."),
    ("address already in use", 409, "port_in_use",
     "That port is already in use by another container or program.",
     "Pick a different host port, or stop whatever is using it."),
    ("pull access denied", 404, "image_not_found",
     "Docker couldn't download that image — it doesn't exist or is private.",
     "Check the image name and tag."),
    ("manifest unknown", 404, "image_not_found",
     "That image tag doesn't exist in the registry.", "Check the tag, or use 'latest'."),
    ("no matching manifest", 400, "image_arch",
     "That image isn't published for this CPU architecture.",
     "On a Raspberry Pi look for an arm64/armv7 variant of the image."),
    ("toomanyrequests", 429, "registry_rate_limited",
     "Docker Hub's download limit was reached.", "Wait a while, or sign in to Docker Hub on the server."),
    ("no space left on device", 507, "disk_full",
     "Docker ran out of disk space.", "Remove unused images/containers (docker system prune) or free up space."),
    ("is already in use by container", 409, "name_in_use",
     "A container with that name already exists.", "Remove the old container or choose another name."),
    ("no such container", 404, "container_gone", "That container no longer exists.", "Refresh the list."),
    ("no such image", 404, "image_not_found", "That image isn't on this server.", ""),
    ("is not running", 409, "not_running", "That container isn't running.", ""),
    ("permission denied", 403, "docker_permission",
     "Void OS isn't allowed to talk to Docker.",
     "Add the Void OS user to the 'docker' group (Linux) or 'docker-users' (Windows), then restart Void OS."),
    ("timed out", 504, "timeout", "Docker didn't respond in time.", "Docker may be busy — try again shortly."),
    ("cannot connect to the docker daemon", 503, "docker_down",
     "Docker isn't running.", "Start Docker (or Docker Desktop on Windows/macOS) and try again."),
]


def _docker_friendly(e) -> Friendly | None:
    mod = type(e).__module__ or ""
    if not mod.startswith("docker"):
        return None
    text = (getattr(e, "explanation", None) or str(e) or "").lower()
    for needle, status, code, msg, hint in _DOCKER_PATTERNS:
        if needle in text:
            return status, code, msg, hint
    return 502, "docker_error", "Docker reported an error.", "An admin can see the details in the Error Log."


def friendly(e: BaseException) -> Friendly:
    """(status, code, message, hint) for any exception."""
    d = _docker_friendly(e)
    if d:
        return d
    if isinstance(e, OSError):
        we = getattr(e, "winerror", None)
        if we in _WINERROR_MAP:
            return _WINERROR_MAP[we]
        if e.errno in _ERRNO_MAP:
            return _ERRNO_MAP[e.errno]
        if isinstance(e, PermissionError):
            return _ERRNO_MAP[errno.EACCES]
        if isinstance(e, FileNotFoundError):
            return _ERRNO_MAP[errno.ENOENT]
        if isinstance(e, FileExistsError):
            return _ERRNO_MAP[errno.EEXIST]
        if isinstance(e, (socket.timeout, TimeoutError)):
            return 504, "timeout", "The operation timed out.", "Try again; if it keeps happening, check the network."
        if isinstance(e, ConnectionError):
            return 502, "connection_failed", "A network connection failed.", "Check the network and try again."
    if isinstance(e, MemoryError):
        return _ERRNO_MAP[errno.ENOMEM]
    if isinstance(e, UnicodeError):
        return 400, "bad_encoding", "A name or file contains characters that couldn't be read.", ""
    try:
        import shutil
        if isinstance(e, shutil.SameFileError):
            return 409, "same_file", "The source and destination are the same item.", ""
        if isinstance(e, shutil.Error):
            # copytree aggregates per-file failures into one Error(list)
            args = e.args[0] if e.args else None
            if isinstance(args, list) and args:
                return 500, "partial_copy", f"{len(args)} item(s) couldn't be copied.", \
                    "Some files may be in use, unreadable, or have names the destination drive doesn't allow."
            return 409, "copy_failed", "That copy or move couldn't be completed.", ""
    except Exception:
        pass
    return 500, "internal", "Something went wrong on the server.", \
        "Try again. If it keeps happening, give an admin the reference code so they can look it up."


def describe(e: BaseException) -> str:
    """'message — hint' in one line, for job errors and notifications."""
    _s, _c, msg, hint = friendly(e)
    return f"{msg} {hint}".strip()


def exc_summary(e: BaseException) -> str:
    """Short raw description for the admin-only error log."""
    return f"{type(e).__module__}.{type(e).__name__}: {e}"[:600]


def where(e: BaseException) -> str:
    """The innermost Void OS source line the exception passed through, as
    'modules/file_ops.py:412 in _file_op_worker' (empty if there is none)."""
    import os
    import traceback
    best = ""
    for fr in traceback.extract_tb(e.__traceback__ or None):
        fn = fr.filename.replace("\\", "/")
        if "/modules/" in fn or fn.endswith("/voidos.py"):
            best = f"{fn.split('/modules/')[-1] if '/modules/' in fn else os.path.basename(fn)}:{fr.lineno} in {fr.name}"
            if "/modules/" in fn:
                best = "modules/" + best
    return best


def reason(e: BaseException) -> str:
    """A plain-language cause for the admin Error Log: what failed, and the likely reason."""
    status, code, msg, hint = friendly(e)
    if code != "internal":
        return f"{msg} {hint}".strip()
    name = type(e).__name__
    detail = str(e).strip()[:160]
    known = {
        "KeyError": "The code looked up a field that wasn't there (a missing key in saved data or in the request)",
        "AttributeError": "The code used something that was None or the wrong kind of object",
        "TypeError": "The code received a value of an unexpected type (often a missing or malformed field)",
        "ValueError": "The code received a value it couldn't use (for example text where a number was expected)",
        "IndexError": "The code asked for an item that isn't in a list",
        "JSONDecodeError": "A saved or received file isn't valid JSON (damaged or half-written)",
        "ImportError": "A Python package or module it needs isn't available",
        "ModuleNotFoundError": "A Python package it needs isn't installed",
        "RuntimeError": "The code hit a state it didn't expect",
        "AssertionError": "An internal check failed",
    }.get(name, f"An unexpected {name} was raised")
    return f"{known}{': ' + detail if detail else ''}."


# ── errors reported by the browser ────────────────────────────────────────
_CLIENT_PATTERNS = [
    # (substring or regex-ish needle (lowercase), category, severity, reason)
    ("failed to fetch", "network", "info", "The browser couldn't reach the server (it was restarting, the network dropped, or a tunnel/proxy in between failed)."),
    ("networkerror", "network", "info", "The browser couldn't reach the server (it was restarting, the network dropped, or a tunnel/proxy in between failed)."),
    ("load failed", "network", "info", "The browser couldn't reach the server (it was restarting, the network dropped, or a tunnel/proxy in between failed)."),
    ("unexpected token '<'", "bad_response", "warning", "The server (or a proxy/tunnel in front of it) answered with a web page where data was expected: usually an error page or a sign-in redirect."),
    ("is not valid json", "bad_response", "warning", "A reply wasn't valid data: usually an error page from a proxy/tunnel, or a response cut off half-way."),
    ("unexpected end of json", "bad_response", "warning", "A reply was cut off half-way (the connection dropped while it was arriving)."),
    ("resizeobserver loop", "benign", "info", "A harmless browser notice about the page resizing while it was drawing. Nothing is broken."),
    ("quotaexceeded", "browser_storage", "warning", "The browser's local storage is full, so a preference couldn't be remembered."),
    ("notallowederror", "permission", "info", "The browser (or you) refused a permission the page asked for, such as the microphone or clipboard."),
    ("notfounderror", "device", "warning", "A device the page asked for (microphone/speaker) wasn't found."),
    ("is not defined", "stale_script", "warning", "A piece of the page's script is missing. This usually means the browser is running an old cached copy after an update: reload the page (Ctrl+F5)."),
    ("is not a function", "stale_script", "warning", "A piece of the page's script isn't what the page expects. This usually means an old cached copy after an update: reload the page (Ctrl+F5)."),
]


def explain_client_error(kind: str, message: str, stack: str, view: str, stale: bool = False) -> dict:
    """Turns a browser-reported error into {reason, category, severity, where}."""
    import re
    msg = (message or "")
    low = msg.lower()
    top = ""
    m = re.search(r"at (?:async )?([^\s(]+) \(([^)]*app\.js[^:]*):(\d+):(\d+)\)", stack or "")
    if m:
        top = f"app.js:{m.group(3)} in {m.group(1)}"
    else:
        m = re.search(r"at ([^\s(]*app\.js[^:]*):(\d+):(\d+)", stack or "")
        if m:
            top = f"app.js:{m.group(2)}"
    if "'dimensions'" in low and "vendor/terminal" in (stack or "").lower():
        return {"reason": "The terminal component (xterm) raised its known harmless error while it was closing or "
                          "being resized (you left the Terminal page just as it was starting). Nothing is broken.",
                "category": "terminal_closing", "severity": "info", "where": top or "static/vendor/terminal.js"}
    if stale:
        return {"reason": "A page that had already been left was still loading and tried to draw on elements that were gone "
                          "(you moved to another page before it finished). Harmless: nothing was lost.",
                "category": "stale_view", "severity": "info", "where": top}
    nm = re.search(r"cannot read propert(?:y|ies) of (null|undefined)(?: \(reading '([^']+)'\))?", msg, re.I)
    if nm:
        what = f" '{nm.group(2)}'" if nm.group(2) else ""
        return {"reason": f"The page tried to use{what} of something that wasn't there ({nm.group(1)}) while working on "
                          f"the {view or 'current'} view. That's a bug in the page, not in your data: the position below says where.",
                "category": "page_bug", "severity": "error", "where": top}
    for needle, cat, sev, why in _CLIENT_PATTERNS:
        if needle in low:
            return {"reason": why, "category": cat, "severity": sev, "where": top}
    if kind == "promise":
        why = "A background request or task in the page failed and nothing handled it."
    else:
        why = "A script error occurred in the page."
    return {"reason": f"{why} Open the technical details for the exact message and position.",
            "category": "page_error", "severity": "error", "where": top}


if __name__ == "__main__":  # quick self-check
    for ex in (OSError(errno.ENOSPC, "x"), PermissionError(13, "x"), FileNotFoundError(2, "x"), ValueError("x")):
        print(friendly(ex))
    sys.exit(0)
