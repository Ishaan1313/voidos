"""
modules/catalog_update.py — refreshing the App Store list without updating Void OS itself.

The list of apps (app_catalog.zip) ships with every Void OS release, but apps are added and
fixed far more often than the program changes. So the project's GitHub repository (the same one
updates come from, UPDATE_REPO in modules/updater.py, fixed in code) also carries a release
tagged `catalog` with two assets:

    catalog.json      version, app count, per-app hashes, and the zip's sha256 and size
    app_catalog.zip   the package itself (format described in modules/catalog_pack.py)

What happens here:
  check   ->  fetch catalog.json, compare its version with the one in use, work out which apps
              are new / changed / removed. Runs once a day when updates are on (the same
              VOID_UPDATES / VOID_UPDATE_CHECK switches as program updates), and on demand.
              Admins are told once per new version through the bell.
  update  ->  admin presses the button. Download the zip, require its SHA-256 to equal the one in
              catalog.json (and GitHub's own digest for the asset when it shows one), unpack it
              into a scratch folder refusing unsafe paths and oversized files, validate every
              app (manifest, compose YAML, icon is a real small image), and only then save it as
              app_catalog_updated.zip and re-extract. Anything wrong -> nothing changes.

Installed apps are never touched: they run from the compose file saved when they were installed.
A removed app only disappears from the list. A package older than or equal to the one in use is
refused, so the list can't be rolled back by a replayed download.

Trust: you are trusting whoever controls the repository, exactly as for program updates. The
checksum protects against a corrupted or truncated download, not against a compromised
repository. Every install is still previewed first and anything asking for elevated host access
is flagged before it is confirmed.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse

from flask import jsonify

from modules import catalog_pack, errors
from modules.activity import _audit, _inapp_notify
from modules.config import CATALOG_DIR, CATALOG_UPDATED_ZIP, TMP_DIR
from modules.decorators import require_admin, require_docker_access
from modules.error_log import _current_user, _log_exception
from modules.web import app

_lock = threading.Lock()
_check = {"checked_at": None, "remote": None, "error": None, "notified": None}
_MAX_INDEX = 2 * 1024 * 1024


def _updater():
    from modules import updater  # loads after this module's imports; only needed at call time
    return updater


# ── what is in use ───────────────────────────────────────────────────
def local_index() -> dict:
    """Index of the catalog currently extracted (computed when the package carries none)."""
    try:
        idx = json.loads((CATALOG_DIR / "catalog.json").read_text(encoding="utf-8"))
        if isinstance(idx, dict) and isinstance(idx.get("apps"), dict):
            return idx
    except Exception:
        pass
    try:
        return catalog_pack.index_for_dir(CATALOG_DIR, "0")
    except Exception:
        return {"version": "0", "count": 0, "apps": {}}


def _names(ids, *indexes) -> list:
    out = []
    for i in ids:
        for idx in indexes:
            n = ((idx or {}).get("apps") or {}).get(i, {}).get("name")
            if n:
                out.append(n)
                break
        else:
            out.append(i)
    return sorted(out, key=str.lower)


def status() -> dict:
    up = _updater()
    loc = local_index()
    remote = _check.get("remote")
    return {
        "version": loc.get("version"), "count": loc.get("count") or len(loc.get("apps") or {}),
        "built_at": loc.get("built_at"),
        "updates_enabled": bool(up.UPDATES_ENABLED), "repo_configured": bool(up.UPDATE_REPO),
        "checked_at": _check.get("checked_at"), "error": _check.get("error"),
        "remote": remote, "update_available": bool(remote and remote.get("newer")),
    }


# ── talking to GitHub ────────────────────────────────────────────────
def _release_assets() -> dict:
    up = _updater()
    if not up.UPDATES_ENABLED:
        raise ValueError("Updates are turned off on this server (VOID_UPDATES=off in .env).")
    if not up.UPDATE_REPO:
        raise ValueError("App list updates aren't available in this copy of Void OS "
                         "(no release repository is set in the code).")
    if not up._REPO_RE.fullmatch(up.UPDATE_REPO):
        raise ValueError("The release repository set in the code isn't a valid owner/repo name.")
    try:
        with up._github_get(f"https://api.github.com/repos/{up.UPDATE_REPO}/releases/tags/catalog") as r:
            data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"{up.UPDATE_REPO} hasn't published an app list yet "
                             "(a release tagged 'catalog' with catalog.json and app_catalog.zip).")
        if e.code in (403, 429):
            raise ValueError("GitHub is limiting requests from this address right now. Try again in a while.")
        raise
    out = {}
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        if name not in ("catalog.json", "app_catalog.zip"):
            continue
        u = urllib.parse.urlparse(str(a.get("browser_download_url") or ""))
        if u.scheme != "https" or u.netloc not in up._GITHUB_HOSTS or \
                not u.path.startswith(f"/{up.UPDATE_REPO}/releases/download/"):
            continue
        m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", str(a.get("digest") or ""))
        out[name] = {"url": u.geturl(), "digest": m.group(1).lower() if m else None}
    return out


def _download(url: str, limit: int, timeout: int = 60) -> bytes:
    up = _updater()
    with up._github_get(url, accept="application/octet-stream", timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The download is larger than allowed, so it was stopped.")
    return data


def check() -> dict:
    """Looks at the published list now. Never raises; the result is also kept in `_check`."""
    with _lock:
        try:
            assets = _release_assets()
            if "catalog.json" not in assets or "app_catalog.zip" not in assets:
                raise ValueError("The 'catalog' release is missing catalog.json or app_catalog.zip.")
            idx = json.loads(_download(assets["catalog.json"]["url"], _MAX_INDEX, 20).decode("utf-8"))
            if not isinstance(idx, dict) or idx.get("format") != catalog_pack.FORMAT or \
                    not isinstance(idx.get("apps"), dict) or not re.fullmatch(r"[0-9a-f]{64}", str(idx.get("sha256", ""))):
                raise ValueError("The published app list isn't in a format this version understands. "
                                 "Update Void OS itself first.")
            loc = local_index()
            d = catalog_pack.diff(loc.get("apps"), idx["apps"])
            newer = catalog_pack.version_key(idx.get("version")) > catalog_pack.version_key(loc.get("version"))
            _check["remote"] = {
                "version": idx.get("version"), "count": idx.get("count"), "newer": newer,
                "new": _names(d["new"], idx) if newer else [],
                "changed": _names(d["changed"], idx) if newer else [],
                "removed": _names(d["removed"], loc) if newer else [],
                "size": idx.get("size"),
            }
            _check["error"] = None
        except Exception as e:
            _check["remote"] = None
            _check["error"] = (str(e) if isinstance(e, ValueError) else errors.describe(e))[:300]
        _check["checked_at"] = int(time.time())
    return status()


def apply() -> dict:
    """Downloads, verifies and installs the published list. Raises ValueError with a reason."""
    with _lock:
        assets = _release_assets()
        if "catalog.json" not in assets or "app_catalog.zip" not in assets:
            raise ValueError("The 'catalog' release is missing catalog.json or app_catalog.zip.")
        idx = json.loads(_download(assets["catalog.json"]["url"], _MAX_INDEX, 20).decode("utf-8"))
        want = str(idx.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", want) or idx.get("format") != catalog_pack.FORMAT:
            raise ValueError("The published app list has no usable checksum, so it was not installed.")
        loc = local_index()
        if catalog_pack.version_key(idx.get("version")) <= catalog_pack.version_key(loc.get("version")):
            raise ValueError("The list in use is already as new as the published one.")
        gh = assets["app_catalog.zip"].get("digest")
        if gh and gh != want:
            raise ValueError("GitHub's checksum for the download doesn't match the published index; "
                             "nothing was installed.")
        data = _download(assets["app_catalog.zip"]["url"], catalog_pack.MAX_ZIP_BYTES, 120)
        if hashlib.sha256(data).hexdigest() != want:
            raise ValueError("The downloaded app list doesn't match its checksum, so it was not installed.")

        work = TMP_DIR / "catalog-update"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        try:
            zp = work / "app_catalog.zip"
            zp.write_bytes(data)
            root = catalog_pack.extract_checked(zp, work)
            problems = catalog_pack.validate_dir(root)
            if problems:
                raise ValueError("The downloaded app list failed its checks: " + "; ".join(problems[:3]))
            inner = catalog_pack.zip_index(zp) or {}
            if inner.get("version") != idx.get("version"):
                raise ValueError("The package's own version doesn't match the published index.")
            tmp_target = CATALOG_UPDATED_ZIP.with_suffix(".zip.new")
            shutil.copyfile(zp, tmp_target)
            tmp_target.replace(CATALOG_UPDATED_ZIP)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        from modules import config
        config._ensure_catalog_extracted()
        try:
            from modules import file_helpers
            file_helpers._known_icons_cache["t"] = 0.0
        except Exception:
            pass
        d = catalog_pack.diff(loc.get("apps"), idx["apps"])
        _check["remote"] = {**(_check.get("remote") or {}), "newer": False, "version": idx.get("version"),
                            "new": [], "changed": [], "removed": []}
        return {"version": idx.get("version"), "count": idx.get("count"),
                "new": _names(d["new"], idx), "changed": _names(d["changed"], idx),
                "removed": _names(d["removed"], loc)}


# ── routes ───────────────────────────────────────────────────────────
@app.route("/api/appstore/catalog/status")
@require_docker_access
def api_catalog_status():
    return jsonify(status())


@app.route("/api/appstore/catalog/check", methods=["POST"])
@require_admin
def api_catalog_check():
    return jsonify(check())


@app.route("/api/appstore/catalog/update", methods=["POST"])
@require_admin
def api_catalog_update():
    user = _current_user()
    try:
        res = apply()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        ref = _log_exception(e, source="catalog", context="update app list")
        return jsonify({"error": f"{errors.describe(e)} (ref {ref})"}), 502
    _audit("catalog_updated", user, target=res["version"],
           detail=f"{len(res['new'])} new, {len(res['changed'])} changed, {len(res['removed'])} removed")
    return jsonify({**res, **status()})


# ── daily check ──────────────────────────────────────────────────────
def _auto_check_loop():
    time.sleep(150)
    while True:
        up = _updater()
        if up.UPDATES_ENABLED and up.UPDATE_REPO and up.UPDATE_CHECK:
            st = check()
            r = st.get("remote")
            if r and r.get("newer") and _check.get("notified") != r.get("version"):
                _check["notified"] = r.get("version")
                from modules.users import _users_load
                bits = []
                if r.get("new"):
                    bits.append(f"{len(r['new'])} new app{'s' if len(r['new']) != 1 else ''}")
                if r.get("changed"):
                    bits.append(f"{len(r['changed'])} updated")
                for u in _users_load():
                    if u.get("role") in ("superuser", "admin"):
                        _inapp_notify(u["id"], "The App Store has new apps",
                                      (", ".join(bits) or "The app list changed") + ". Open the App Store to update the list.",
                                      level="info", kind="appstore", link="appstore")
        time.sleep(24 * 3600)


threading.Thread(target=_auto_check_loop, daemon=True, name="catalog-check").start()
