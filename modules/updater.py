"""
modules/updater.py — in-app updates (Settings → Updates, Super User only).

How releases flow (GitHub Releases — nothing to build by hand)
  1. Bump the VERSION file, commit, then publish a GitHub release whose tag is
     that version (v1.2.0 or 1.2.0). Write what changed in the release notes.
  2. Installs check https://api.github.com/repos/<owner>/<repo>/releases/latest
     once a day (VOID_UPDATE_CHECK=false to disable) and tell the Super User
     through the bell when the tag is newer than their VERSION. Nothing is ever
     installed without the Super User pressing Install.
  3. The repository is UPDATE_REPO below, fixed in the code (it cannot be
     redirected from .env or the web interface). It must be public.
     VOID_UPDATES=off in .env turns updates off entirely.

What gets downloaded
  - If the release has an attached zip named like voidos-1.2.0.zip, that is used
    and, when GitHub shows a SHA-256 for it, the download must match it.
  - Otherwise GitHub's automatic "Source code (zip)" for the tag. That one has no
    published checksum, so only HTTPS to github.com protects it (the page says
    so).
  Either way the zip's own VERSION file must equal the release tag, so tagging
  without bumping VERSION is caught before anything is installed.

What "Install" does (a background job; progress is polled by the page)
  download (into tmp/, never the OS temp folder) -> verify the SHA-256 when there
  is one -> unpack (refusing paths that escape the folder) -> sanity-check it is a
  Void OS release -> install its requirements.txt into the running Python FIRST
  (nothing has changed yet if that fails) -> back up every program file it will
  replace into tmp/update/backup-<old version>/ -> swap them in (rolled back
  automatically if any step fails) -> restart. Only PROGRAM_ENTRIES are replaced;
  never .env, user/session/share data, the files folder, app data, logs, .venv.
  A new app_catalog.zip makes config.py rebuild the App Store catalog on the
  next start; packages added later are also covered by the startup check in
  voidos.py.

Trust model: you are trusting whoever controls the release repository. HTTPS to
github.com and Super-User-only installs are the safeguards; an attached,
checksummed zip adds protection against a corrupted download, but not against a
compromised repository. Signed releases would be the next step for wide use.
"""

import hashlib, json, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid, zipfile

from flask import jsonify
from modules import errors, winplat
from modules.activity import _audit
from modules.cloud_routes import _sync_stop_supervisor, _sync_supervisors
from modules.config import TMP_DIR, VOID_ROOT
from modules.decorators import require_superuser
from modules.error_log import _api_error, _current_user, _log_exception
from modules.tunnels import _cleanup_all_tunnels
from modules.web import app

# The GitHub repository updates come from ("owner/repo", public). Deliberately
# fixed here, not configurable from .env: an install can't be pointed at some
# other source of "updates".
UPDATE_REPO = "Ishaan1313/voidos"
# The only setting: VOID_UPDATES=off disables updates entirely (no checks, no installs).
UPDATES_ENABLED = os.environ.get("VOID_UPDATES", "on").strip().lower() not in ("off", "false", "no", "0")
UPDATE_CHECK = os.environ.get("VOID_UPDATE_CHECK", "true").strip().lower() != "false"
UPDATE_DIR = TMP_DIR / "update"
MAX_RELEASE_BYTES = 300 * 1024 * 1024
MIN_PYTHON = "3.10"
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_ASSET_RE = re.compile(r"voidos[-_.\w]*\.zip", re.I)
_GITHUB_HOSTS = {"api.github.com", "github.com"}

# What a release may replace. Anything else in the zip is ignored, and these
# names can never be overwritten whatever a release contains.
PROGRAM_ENTRIES = {"voidos.py", "VERSION", "requirements.txt", "README.md",
                   "modules", "templates", "static", "first setup",
                   "app_catalog.zip", ".gitignore", ".gitattributes"}
NEVER_TOUCH = {".env", ".venv", ".tools", "tmp", "app_catalog", "app_catalog_updated.zip", "wallpapers", "rclone_conf", "icon-cache"}

_check = {"checked_at": None, "latest": None, "error": None, "notified": None}
_job = {"state": "idle"}           # idle | running | done | error ; plus step/progress/message
_lock = threading.Lock()


def current_version() -> str:
    try:
        return (VOID_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


def _vtuple(v: str):
    """'1.10.2' → (1, 10, 2); tolerant of a leading 'v' and suffixes like '-beta'."""
    nums = re.findall(r"\d+", str(v).split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub redirects downloads to its own storage hosts; follow that, but never
    to anything that isn't https."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise ValueError("GitHub redirected the download to a non-https address, so it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirect)


def _github_get(url: str, accept: str = "application/vnd.github+json", timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": f"VoidOS/{current_version()}", "Accept": accept,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    return _opener.open(req, timeout=timeout)


def _fetch_release() -> dict:
    """The newest published (non-draft, non-pre-release) GitHub release, as
    {version, tag, url, sha256 | None, verified, source, notes}."""
    if not UPDATES_ENABLED:
        raise ValueError("Updates are turned off on this server (VOID_UPDATES=off in .env).")
    if not UPDATE_REPO:
        raise ValueError("Updates aren't available in this copy of Void OS (no release repository is set in the code).")
    if not _REPO_RE.fullmatch(UPDATE_REPO):
        raise ValueError("The release repository set in the code isn't a valid owner/repo name.")
    try:
        with _github_get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest") as r:
            data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"No releases found for {UPDATE_REPO}. Either it hasn't published one yet, "
                             "or the repository is private or misspelled.")
        if e.code in (403, 429):
            raise ValueError("GitHub is limiting requests from this address right now. Try again in a while.")
        raise
    tag = str(data.get("tag_name") or "")
    version = tag.lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
        raise ValueError(f"The newest release's tag ({tag or 'none'}) isn't a version number like v1.2.0.")
    notes = str(data.get("body") or "").strip()[:1500]

    # Prefer an attached zip (it may carry a checksum); else GitHub's source zip.
    url, sha, source = f"https://api.github.com/repos/{UPDATE_REPO}/zipball/{tag}", None, "source"
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        dl = str(a.get("browser_download_url") or "")
        if not _ASSET_RE.fullmatch(name):
            continue
        u = urllib.parse.urlparse(dl)
        if u.scheme != "https" or u.netloc not in _GITHUB_HOSTS or not u.path.startswith(f"/{UPDATE_REPO}/releases/download/"):
            continue
        m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", str(a.get("digest") or ""))
        url, sha, source = dl, (m.group(1).lower() if m else None), "asset"
        if sha:
            break  # a checksummed zip beats an unchecksummed one
    return {"version": version, "tag": tag, "url": url, "sha256": sha, "verified": bool(sha),
            "source": source, "notes": notes, "min_python": MIN_PYTHON}


def check_for_update() -> dict:
    with _lock:
        try:
            m = _fetch_release()
            _check.update(latest=m, error=None)
        except Exception as e:
            _check.update(latest=None, error=str(e) if isinstance(e, ValueError) else errors.describe(e))
        _check["checked_at"] = time.time()
        return dict(_check)


def _status() -> dict:
    cur = current_version()
    latest = _check.get("latest")
    available = bool(latest) and _vtuple(latest["version"]) > _vtuple(cur)
    backups = sorted(p.name.replace("backup-", "") for p in UPDATE_DIR.glob("backup-*") if p.is_dir()) if UPDATE_DIR.exists() else []
    return {"current": cur, "configured": UPDATES_ENABLED and bool(UPDATE_REPO), "disabled": not UPDATES_ENABLED,
            "repo": UPDATE_REPO or None, "auto_check": UPDATE_CHECK,
            "checked_at": _check["checked_at"], "error": _check["error"],
            "latest": ({k: latest.get(k) for k in ("version", "notes", "min_python", "verified", "source")} if latest else None),
            "available": available, "job": {k: v for k, v in _job.items() if not k.startswith("_")},
            "backups": backups, "restart_mode": _restart_mode()}


def _restart_mode() -> str:
    return winplat.restart_mode()


def _restart_soon():
    def go():
        time.sleep(1.0)  # let the final status reach the browser
        try:
            _cleanup_all_tunnels()
        except Exception:
            pass
        try:
            for pid in list(_sync_supervisors.keys()):
                _sync_stop_supervisor(pid)
        except Exception:
            pass
        winplat.restart_server(str(VOID_ROOT / "voidos.py"), str(VOID_ROOT))
    threading.Thread(target=go, daemon=True).start()


def _step(msg, pct=None):
    _job["step"] = msg
    if pct is not None:
        _job["progress"] = pct


def _install_worker(manifest: dict, user: dict):
    old = current_version()
    work = UPDATE_DIR / f"job-{uuid.uuid4().hex[:8]}"
    swapped, backup = [], UPDATE_DIR / f"backup-{old}"
    try:
        work.mkdir(parents=True, exist_ok=True)
        # 1. download
        _step("Downloading", 2)
        zpath = work / "release.zip"
        h = hashlib.sha256()
        with _github_get(manifest["url"], accept="application/octet-stream", timeout=30) as r, open(zpath, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_RELEASE_BYTES:
                    raise ValueError("The download is far larger than any Void OS release — stopped.")
                h.update(chunk); out.write(chunk)
                if total:
                    _step("Downloading", 2 + int(48 * got / total))
        # 2. verify
        _step("Verifying the download", 52)
        if manifest.get("sha256") and h.hexdigest().lower() != manifest["sha256"].lower():
            raise ValueError("The download doesn't match the release's SHA-256 — it may be corrupted or tampered with. Nothing was changed.")
        # 3. unpack safely
        _step("Unpacking", 56)
        dest = work / "new"
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                target = (dest / name).resolve()
                try:
                    target.relative_to(dest.resolve())       # a real containment test, not a string prefix
                except ValueError:
                    raise ValueError(f"The release contains an unsafe path ({name}). Nothing was changed.")
            zf.extractall(dest)
        roots = sorted((p.parent for p in dest.rglob("voidos.py") if len(p.relative_to(dest).parts) <= 2),
                       key=lambda q: len(q.relative_to(dest).parts))
        if not roots:
            raise ValueError("That download isn't a Void OS release (no voidos.py). Nothing was changed.")
        new_root = roots[0]
        # 4. sanity checks
        new_ver = (new_root / "VERSION").read_text(encoding="utf-8").strip() if (new_root / "VERSION").exists() else ""
        if new_ver != manifest["version"]:
            raise ValueError(f"The release is tagged {manifest['version']}, but the files inside say version {new_ver or '?'} "
                             "(the VERSION file wasn't bumped before tagging). Nothing was changed.")
        minpy = _vtuple(manifest.get("min_python") or MIN_PYTHON)
        if sys.version_info[:len(minpy)] < minpy:
            raise ValueError(f"This version needs Python {manifest.get('min_python')} or newer. Nothing was changed.")
        for must in ("modules", "templates", "requirements.txt"):
            if not (new_root / must).exists():
                raise ValueError(f"The release is incomplete (no {must}). Nothing was changed.")
        # 5. packages first — if this fails, nothing has been touched
        _step("Installing Python packages", 62)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                            "-r", str(new_root / "requirements.txt")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=1800, **winplat.no_window())
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            raise ValueError("Couldn't install the new version's Python packages, so nothing was changed. "
                             + " ".join(tail)[:300])
        (TMP_DIR / ".requirements.sha256").write_text(
            hashlib.sha256((new_root / "requirements.txt").read_bytes()).hexdigest(), encoding="utf-8")
        # 6. back up, then swap
        _step("Installing files", 82)
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir(parents=True)
        entries = [p for p in new_root.iterdir() if p.name in PROGRAM_ENTRIES and p.name not in NEVER_TOUCH]
        for src in entries:
            live = VOID_ROOT / src.name
            if live.exists():
                shutil.move(str(live), str(backup / src.name))
            swapped.append(src.name)
            shutil.move(str(src), str(live))
        _step("Restarting", 98)
        _audit("update_installed", user, detail=f"{old} → {manifest['version']}")
        _job.update(state="done", progress=100, version=manifest["version"],
                    restart=_restart_mode(), step="Restarting", message=None)
        _restart_soon()
    except Exception as e:
        # roll back anything already swapped
        for name in reversed(swapped):
            live, saved = VOID_ROOT / name, backup / name
            try:
                if live.is_dir():
                    shutil.rmtree(live)
                elif live.exists():
                    live.unlink()
                if saved.exists():
                    shutil.move(str(saved), str(live))
            except Exception as re_err:
                _log_exception(re_err, source="job", context="update rollback")
        msg = str(e) if isinstance(e, ValueError) else errors.describe(e)
        ref = None if isinstance(e, ValueError) else _log_exception(e, source="job", context="update")
        _job.update(state="error", message=msg, ref=ref, step=None)
        _audit("update_failed", user, detail=msg[:200])
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route("/api/admin/update")
@require_superuser
def api_update_status():
    return jsonify(_status())


@app.route("/api/admin/update/check", methods=["POST"])
@require_superuser
def api_update_check():
    check_for_update()
    st = _status()
    if st["error"]:
        return jsonify({**st, "ok": False}), 200
    return jsonify({**st, "ok": True})


@app.route("/api/admin/update/install", methods=["POST"])
@require_superuser
def api_update_install():
    if not UPDATES_ENABLED:
        return _api_error("Updates are turned off on this server.", 403, "updates_off")
    with _lock:
        if _job.get("state") == "running":
            return _api_error("An update is already being installed.", 409, "busy")
        latest = _check.get("latest")
        if not latest:
            return _api_error("Check for updates first.", 400, "no_check")
        if _vtuple(latest["version"]) <= _vtuple(current_version()):
            return _api_error("You're already on the newest version.", 400, "up_to_date")
        _job.clear(); _job.update(state="running", progress=0, step="Starting", version=latest["version"])
    user = _current_user()
    threading.Thread(target=_install_worker, args=(dict(latest), user), daemon=True).start()
    return jsonify({"ok": True})


def _auto_check_loop():
    """Daily check; tells every Super User once per new version."""
    time.sleep(90)
    while True:
        if UPDATES_ENABLED and UPDATE_REPO and UPDATE_CHECK:
            st = check_for_update()
            latest = st.get("latest")
            if latest and _vtuple(latest["version"]) > _vtuple(current_version()) and _check.get("notified") != latest["version"]:
                _check["notified"] = latest["version"]
                from modules.users import _users_load
                from modules.activity import _inapp_notify
                for u in _users_load():
                    if u.get("role") == "superuser":
                        _inapp_notify(u["id"], f"Void OS {latest['version']} is available",
                                      (latest.get("notes") or "Open Settings → Updates to install it.")[:300],
                                      level="info", kind="update", link="settings")
        time.sleep(24 * 3600)
