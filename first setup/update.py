#!/usr/bin/env python3
"""
Void OS: update an install from the command line, without the in-app updater.

For installs whose own updater is broken (1.0.0 to 1.0.3 fail with "HTTP Error 415: Unsupported Media
Type" when you press Install). Safe to run on any version; it only touches Void OS's program files and
never your .env, accounts, files or any other data.

  Linux / macOS / Raspberry Pi:
    curl -fsSL https://raw.githubusercontent.com/Ishaan1313/voidos/main/first%20setup/update.py | ~/voidos/.venv/bin/python -

  Windows (PowerShell):
    irm https://raw.githubusercontent.com/Ishaan1313/voidos/main/first%20setup/update.py | & "$env:USERPROFILE\\voidos\\.venv\\Scripts\\python.exe" -

  Another folder:  set VOIDOS_DIR first (the folder that contains voidos.py).
  Options (after "-"):  --check  only say what is available      --yes  don't ask

What it does: finds the install, downloads the newest GitHub release, checks its SHA-256 and that the
VERSION inside matches the tag, backs up the current program files, swaps in the new ones, installs the
new Python packages, and tells you how to restart. If anything fails it puts the old files back.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = "Ishaan1313/voidos"                       # the same fixed repository the in-app updater uses
# What a release may replace. Everything else (your .env, data, .venv, tmp...) is never touched.
PROGRAM_ENTRIES = {"voidos.py", "VERSION", "requirements.txt", "README.md", "modules", "templates",
                   "static", "first setup", "app_catalog.zip", ".gitignore", ".gitattributes"}
MAX_BYTES = 300 * 1024 * 1024
UA = {"User-Agent": "VoidOS-update-script", "X-GitHub-Api-Version": "2022-11-28"}


def say(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    print(f"\n  x {msg}\n", file=sys.stderr, flush=True)
    sys.exit(code)


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise ValueError("GitHub redirected the download to a non-https address; stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnly)


def get(url, accept="application/vnd.github+json", timeout=30):
    return _opener.open(urllib.request.Request(url, headers={**UA, "Accept": accept}), timeout=timeout)


def find_root() -> Path:
    cands = []
    if os.environ.get("VOIDOS_DIR"):
        cands.append(Path(os.environ["VOIDOS_DIR"]).expanduser())
    exe = Path(sys.executable).resolve()
    for p in exe.parents:                                  # <app>/.venv/bin/python or <app>/.venv/Scripts/python.exe
        if p.name == ".venv":
            cands.append(p.parent)
    cands += [Path.cwd(), Path.cwd().parent, Path.home() / "voidos"]
    for c in cands:
        if (c / "voidos.py").is_file() and (c / "modules").is_dir():
            return c
    die("Couldn't find the Void OS folder. Set VOIDOS_DIR to the folder that contains voidos.py, then run this again.")


def vtuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", str(v).split("-")[0])[:4]) or (0,)


def latest_release():
    try:
        with get(f"https://api.github.com/repos/{REPO}/releases/latest") as r:
            data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        die("GitHub has no published release yet." if e.code == 404 else f"GitHub answered HTTP {e.code}.")
    except Exception as e:
        die(f"Couldn't reach GitHub: {e}")
    tag = str(data.get("tag_name") or "")
    version = tag.lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
        die(f"The newest release's tag ({tag or 'none'}) isn't a version number.")
    url, sha, kind = f"https://api.github.com/repos/{REPO}/zipball/{tag}", None, "source"
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        dl = str(a.get("browser_download_url") or "")
        u = urllib.parse.urlparse(dl)
        if (re.fullmatch(r"voidos[-_.\w]*\.zip", name, re.I) and u.scheme == "https" and u.netloc == "github.com"
                and u.path.startswith(f"/{REPO}/releases/download/")):
            m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", str(a.get("digest") or ""))
            url, sha, kind = dl, (m.group(1).lower() if m else None), "asset"
            if sha:
                break
    return {"version": version, "tag": tag, "url": url, "sha256": sha, "kind": kind}


def main():
    args = sys.argv[1:]
    check_only, assume_yes = "--check" in args, "--yes" in args or "-y" in args
    root = find_root()
    cur = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else "0"
    say(f"\n  Void OS folder:  {root}")
    say(f"  Installed:       {cur}")
    rel = latest_release()
    say(f"  Newest release:  {rel['version']}  ({'checksummed zip' if rel['sha256'] else rel['kind'] + ' zip'})")
    if vtuple(rel["version"]) <= vtuple(cur):
        say("\n  Already up to date. Nothing to do.\n")
        return
    if check_only:
        say(f"\n  {rel['version']} is available. Run again without --check to install it.\n")
        return
    if not assume_yes and sys.stdin and sys.stdin.isatty():
        if input(f"\n  Update {cur} -> {rel['version']}? Your data and settings are not touched. [Y/n] ").strip().lower() in ("n", "no"):
            return

    work = Path(tempfile.mkdtemp(prefix="voidos-update-"))
    backup = root / "tmp" / "update" / f"backup-{cur}"
    swapped = []
    try:
        # 1. download (the right Accept header for each kind: assets want octet-stream, source zips do not)
        say("\n  Downloading...")
        zpath = work / "release.zip"
        accept = "application/octet-stream" if rel["kind"] == "asset" else "application/vnd.github+json"
        h, got = hashlib.sha256(), 0
        with get(rel["url"], accept=accept, timeout=60) as r, open(zpath, "wb") as out:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_BYTES:
                    raise RuntimeError("The download is far larger than any Void OS release; stopped.")
                h.update(chunk)
                out.write(chunk)
        say(f"  Downloaded {got // 1024} KB")
        # 2. verify
        if rel["sha256"] and h.hexdigest().lower() != rel["sha256"]:
            raise RuntimeError("The download doesn't match the release's SHA-256. Nothing was changed.")
        say("  Checksum OK" if rel["sha256"] else "  (GitHub gave no checksum for this zip; relying on HTTPS)")
        # 3. unpack safely
        dest = work / "new"
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                try:
                    (dest / name).resolve().relative_to(dest.resolve())
                except ValueError:
                    raise RuntimeError(f"The release contains an unsafe path ({name}). Nothing was changed.")
            zf.extractall(dest)
        roots = sorted((p.parent for p in dest.rglob("voidos.py") if len(p.relative_to(dest).parts) <= 2),
                       key=lambda q: len(q.relative_to(dest).parts))
        if not roots:
            raise RuntimeError("That download isn't a Void OS release (no voidos.py). Nothing was changed.")
        new = roots[0]
        new_ver = (new / "VERSION").read_text(encoding="utf-8").strip() if (new / "VERSION").exists() else ""
        if new_ver != rel["version"]:
            raise RuntimeError(f"The release is tagged {rel['version']} but its files say {new_ver or '?'}. Nothing was changed.")
        for must in ("modules", "templates", "requirements.txt"):
            if not (new / must).exists():
                raise RuntimeError(f"The release is incomplete (no {must}). Nothing was changed.")
        # 4. packages first: if this fails nothing has been touched
        say("  Installing Python packages...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                            "-r", str(new / "requirements.txt")], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            tail = " ".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])[:300]
            raise RuntimeError(f"Couldn't install the new Python packages, so nothing was changed. {tail}")
        # 5. back up, then swap
        say("  Installing files...")
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir(parents=True)
        for src in [p for p in new.iterdir() if p.name in PROGRAM_ENTRIES]:
            live = root / src.name
            if live.exists():
                shutil.move(str(live), str(backup / src.name))
            swapped.append(src.name)
            shutil.move(str(src), str(live))
        # keep the owner's optional local clip (static/rick.mp4): an update replaces the whole static folder
        try:
            kept = backup / "static" / "rick.mp4"
            if kept.is_file() and not (root / "static" / "rick.mp4").exists():
                shutil.copy2(str(kept), str(root / "static" / "rick.mp4"))
        except OSError:
            pass
        (root / "tmp").mkdir(exist_ok=True)
        try:
            (root / "tmp" / ".requirements.sha256").write_text(
                hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest(), encoding="utf-8")
        except OSError:
            pass
    except Exception as e:
        for name in reversed(swapped):                      # put the old files back
            live, saved = root / name, backup / name
            try:
                if live.is_dir():
                    shutil.rmtree(live)
                elif live.exists():
                    live.unlink()
                if saved.exists():
                    shutil.move(str(saved), str(live))
            except Exception:
                pass
        die(f"{e}\n  Your install was left as it was.")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    say(f"\n  Updated {cur} -> {rel['version']}.  (Old files are kept in {backup})")
    say("  Restart Void OS to use it:")
    say("    Linux/macOS:  voidos restart        (or: sudo systemctl restart voidos)")
    say("    Windows:      close the Void OS window, then Start menu > Void OS > Start Void OS")
    say("                  (or use Restart in Settings > Updates once it shows the new version)\n")


if __name__ == "__main__":
    main()
