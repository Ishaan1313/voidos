"""
first setup/helper.py — the parts of installing Void OS that are the same on
every OS, used by both install.sh (Linux/macOS) and install.ps1 (Windows).

Doing these in Python instead of twice in two shell languages avoids the
classic installer bugs: sed mangling a path with slashes or backslashes,
PowerShell rewriting a file's encoding, a regex matching a commented-out
line. Standard library only — runs on the Python the installer just
found, before any packages are installed.

    python "first setup/helper.py" env-init                 create .env from .env.example if missing
    python "first setup/helper.py" env-sync                 add settings new to .env.example to an existing .env (commented)
    python "first setup/helper.py" env-get KEY              print a value ('' if unset)
    python "first setup/helper.py" env-set KEY VALUE        set a value, keeping comments/layout
    python "first setup/helper.py" ensure-secret            generate VOID_SECRET_KEY if empty
    python "first setup/helper.py" port-free PORT           exit 0 if the TCP port is free
    python "first setup/helper.py" lan-ips                  this machine's LAN IPv4 addresses
    python "first setup/helper.py" wait-health PORT SECS    wait until Void OS answers /api/health
    python "first setup/helper.py" py-ok                    exit 0 if this Python can run Void OS
    python "first setup/helper.py" mark-requirements        record that requirements.txt is installed

Not needed for Void OS to run — only the installer uses it (first install,
re-runs, --uninstall). Editing .env goes through modules/envfile.py, shared
with Void OS itself, so both always follow the same rules.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))          # <app>/first setup
ROOT = os.path.dirname(HERE)                                # <app>
ENV = os.path.join(ROOT, ".env")
EXAMPLE = os.path.join(HERE, ".env.example")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")
REQ_MARKER = os.path.join(ROOT, "tmp", ".requirements.sha256")
sys.path.insert(0, ROOT)
from modules import envfile  # the same .env editor Void OS itself uses (stdlib only)
MIN_PY = (3, 10)


def env_init() -> str:
    return "created" if envfile.create_from_example(ENV, EXAMPLE) else "kept"


def env_sync() -> str:
    if not os.path.exists(ENV):
        return "0"
    return str(envfile.sync_missing(ENV, EXAMPLE))


def env_get(key: str) -> str:
    return envfile.get(ENV, key)


def env_set(key: str, value: str) -> None:
    env_init()
    envfile.update(ENV, {key: value})


def mark_requirements() -> None:
    """Record which requirements.txt was just installed, so Void OS's own
    startup check (see voidos.py) doesn't reinstall the same packages."""
    import hashlib
    os.makedirs(os.path.dirname(REQ_MARKER), exist_ok=True)
    with open(REQUIREMENTS, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(REQ_MARKER, "w", encoding="utf-8") as f:
        f.write(digest)


def ensure_secret() -> str:
    if env_get("VOID_SECRET_KEY"):
        return "kept"
    env_set("VOID_SECRET_KEY", secrets.token_hex(32))
    return "generated"


def port_free(port: int) -> bool:
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue  # this address family isn't available on this host (e.g. no IPv6)
        try:
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError as e:
            if fam == socket.AF_INET6 and e.errno in (97, 10047, 99):  # no IPv6 on this host
                continue
            return False
        finally:
            s.close()
    return True


def lan_ips() -> list[str]:
    ips = []
    try:  # the address the OS would use to reach the internet (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def wait_health(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if json.loads(r.read().decode() or "{}").get("ok"):
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "py-ok":
        ok = sys.version_info >= MIN_PY
        try:
            import venv  # noqa: F401
            import ensurepip  # noqa: F401  (Debian/Ubuntu ship this separately as python3-venv)
        except ImportError:
            print("no-venv")
            return 3
        print(".".join(map(str, sys.version_info[:3])))
        return 0 if ok else 1
    if cmd == "env-sync":
        print(env_sync())
        return 0
    if cmd == "env-init":
        print(env_init()); return 0
    if cmd == "env-get":
        print(env_get(args[0])); return 0
    if cmd == "env-set":
        env_set(args[0], args[1] if len(args) > 1 else ""); return 0
    if cmd == "ensure-secret":
        print(ensure_secret()); return 0
    if cmd == "port-free":
        return 0 if port_free(int(args[0])) else 1
    if cmd == "lan-ips":
        print(" ".join(lan_ips())); return 0
    if cmd == "mark-requirements":
        mark_requirements(); return 0
    if cmd == "wait-health":
        return 0 if wait_health(int(args[0]), float(args[1] if len(args) > 1 else 60)) else 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
