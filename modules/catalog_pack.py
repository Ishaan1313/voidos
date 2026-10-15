"""
modules/catalog_pack.py — the App Store list as a package. Pure Python (no Flask, no app
state) so the server, the updater and the maintainer's catalog tooling all use the same code.

A catalog package is a zip with one top-level folder, `app_catalog/`:

    app_catalog/catalog.json              index: format, version, per-app content hashes
    app_catalog/<app-id>/manifest.json    name, description, ports, options, ...
    app_catalog/<app-id>/docker-compose.yml.tmpl
    app_catalog/<app-id>/icon.<svg|png|jpg|webp|gif|ico>      optional

The version is a date string such as 2026.10.13.1; a package with a higher version replaces a
lower one. `catalog.json` published next to the zip (the "release index") is the same index plus
the zip's `sha256` and `size`, so a download can be checked before anything is unpacked.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

FORMAT = 1
ROOT = "app_catalog"
ICON_EXTS = ("svg", "png", "jpg", "jpeg", "webp", "gif", "ico")
MAX_ZIP_BYTES = 80 * 1024 * 1024
MAX_FILES = 3000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ICON_BYTES = 512 * 1024
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_VER_RE = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}\.\d{1,3}")


def version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:4]) or (0,)


def new_version(previous: str = "") -> str:
    """Today's date plus a counter that goes up if `previous` is from the same day."""
    t = time.gmtime()
    base = f"{t.tm_year}.{t.tm_mon}.{t.tm_mday}"
    pv = version_key(previous)
    if len(pv) == 4 and pv[:3] == (t.tm_year, t.tm_mon, t.tm_mday):
        return f"{base}.{pv[3] + 1}"
    return f"{base}.1"


def icon_file(app_dir: Path) -> Path | None:
    for ext in ICON_EXTS:
        p = app_dir / f"icon.{ext}"
        if p.is_file():
            return p
    return None


def app_hash(app_dir: Path) -> str:
    h = hashlib.sha256()
    for name in ("manifest.json", "docker-compose.yml.tmpl"):
        p = app_dir / name
        h.update(name.encode() + b"\0")
        if p.is_file():
            h.update(p.read_bytes())
    ic = icon_file(app_dir)
    if ic:
        h.update(ic.name.encode() + b"\0" + ic.read_bytes())
    return h.hexdigest()


def index_for_dir(root: Path, version: str | None = None) -> dict:
    """The index of an unpacked catalog folder (`root` is the folder holding the app folders)."""
    apps = {}
    for d in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        mp = d / "manifest.json"
        if not mp.is_file() or not (d / "docker-compose.yml.tmpl").is_file():
            continue
        try:
            name = json.loads(mp.read_text(encoding="utf-8")).get("name") or d.name
        except Exception:
            name = d.name
        apps[d.name] = {"name": str(name), "hash": app_hash(d)}
    return {"format": FORMAT, "version": version or new_version(), "built_at": int(time.time()),
            "count": len(apps), "apps": apps}


def diff(old: dict, new: dict) -> dict:
    """What a new list changes against the current one: {"new": [...], "changed": [...], "removed": [...]}
    (app ids)."""
    old, new = old or {}, new or {}
    return {"new": sorted(k for k in new if k not in old),
            "changed": sorted(k for k in new if k in old and new[k].get("hash") != old[k].get("hash")),
            "removed": sorted(k for k in old if k not in new)}


def _sniff_icon(data: bytes) -> bool:
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"\x00\x00\x01\x00", b"GIF87a", b"GIF89a")):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    head = data[:300].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2000].lower())


def validate_dir(root: Path) -> list:
    """Problems found in an unpacked catalog folder; empty means it is safe to use."""
    import yaml
    problems = []
    root = Path(root)
    apps = [p for p in sorted(root.iterdir()) if p.is_dir()]
    if not apps:
        return ["the package contains no apps"]
    for d in apps:
        i = d.name
        if not _ID_RE.fullmatch(i):
            problems.append(f"{i}: the folder name isn't a valid app id")
            continue
        mp, tp = d / "manifest.json", d / "docker-compose.yml.tmpl"
        if not mp.is_file() or not tp.is_file():
            problems.append(f"{i}: manifest.json or docker-compose.yml.tmpl is missing")
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            if not isinstance(m, dict) or not str(m.get("name") or "").strip():
                problems.append(f"{i}: manifest.json has no name")
        except Exception as e:
            problems.append(f"{i}: manifest.json can't be read ({e})")
            continue
        try:
            t = tp.read_text(encoding="utf-8")
            t = re.sub(r"\{\{[A-Za-z_]+\}\}", "x", t)
            doc = yaml.safe_load(t)
            if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
                problems.append(f"{i}: the compose template has no services")
        except Exception as e:
            problems.append(f"{i}: the compose template isn't valid YAML ({str(e).splitlines()[0][:80]})")
        ic = icon_file(d)
        if ic:
            data = ic.read_bytes()
            if len(data) > MAX_ICON_BYTES or not _sniff_icon(data):
                problems.append(f"{i}: the icon isn't a small, valid image")
    return problems


def build(src: Path, out_zip: Path, version: str | None = None) -> dict:
    """Packs the folder `src` (app folders directly inside) into `out_zip`. Returns the release index
    (the in-zip index plus sha256 and size of the zip)."""
    src, out_zip = Path(src), Path(out_zip)
    problems = validate_dir(src)
    if problems:
        raise ValueError("catalog has problems:\n  " + "\n  ".join(problems))
    idx = index_for_dir(src, version)
    tmp = out_zip.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{ROOT}/catalog.json", json.dumps(idx, indent=1, sort_keys=True))
        for d in sorted(p for p in src.iterdir() if p.is_dir()):
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    z.write(f, f"{ROOT}/{d.name}/{f.relative_to(d).as_posix()}")
    shutil.move(str(tmp), str(out_zip))
    data = out_zip.read_bytes()
    return {**idx, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def zip_index(zip_path: Path) -> dict | None:
    """The index inside a catalog zip, or None if it has none (an older, unversioned package)."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return json.loads(z.read(f"{ROOT}/catalog.json").decode("utf-8"))
    except Exception:
        return None


def zip_version(zip_path: Path) -> tuple:
    idx = zip_index(zip_path)
    return version_key(idx.get("version")) if idx else (0,)


def extract_checked(zip_path: Path, dest_parent: Path) -> Path:
    """Unpacks `zip_path` into a fresh temporary folder under `dest_parent` after refusing anything
    unsafe (paths that escape, too many or too large files). Returns the folder holding the app
    folders. Raises ValueError with a plain-language reason."""
    zp = Path(zip_path)
    if zp.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("the catalog package is too large")
    dest_parent = Path(dest_parent)
    dest_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="catalog-", dir=dest_parent))
    try:
        with zipfile.ZipFile(zp) as z:
            infos = z.infolist()
            if len(infos) > MAX_FILES:
                raise ValueError("the catalog package has too many files")
            total = 0
            for info in infos:
                name = info.filename
                parts = Path(name).parts
                if name.startswith(("/", "\\")) or ".." in parts or (len(name) > 1 and name[1] == ":"):
                    raise ValueError(f"unsafe path in the package: {name}")
                if parts and parts[0] != ROOT:
                    raise ValueError(f"unexpected entry outside {ROOT}/: {name}")
                if info.file_size > MAX_FILE_BYTES:
                    raise ValueError(f"{name} is too large")
                total += info.file_size
                if total > 4 * MAX_ZIP_BYTES:
                    raise ValueError("the catalog package unpacks to too much data")
            z.extractall(tmp)
        root = tmp / ROOT
        if not root.is_dir():
            raise ValueError(f"the package has no {ROOT}/ folder")
        return root
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
