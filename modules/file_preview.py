"""
modules/file_preview.py — Read-only previews of Office documents (PowerPoint, Word, Excel and the
OpenDocument equivalents) for the file viewer.

These formats are zip files of XML. The text is pulled out here, on the server, with hard limits on
every step (file size, size of each XML part, slides / paragraphs / rows / columns), so a huge or
hostile document can only ever produce a small, bounded answer. Nothing is executed or rendered
server-side, and the result is plain data the page draws itself.

  GET /api/file/office?path=...             -> {"kind": "pptx"|"docx"|"xlsx", ...}
  GET /api/file/office/media?path=&name=... -> one picture from inside a presentation/document
"""
import hashlib
import io
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from flask import Response, jsonify, request

from modules.config import TMP_DIR
from modules.decorators import require_auth
from modules.error_log import _current_user
from modules.file_access import _safe
from modules.web import app

MAX_FILE_BYTES = 60 * 1024 * 1024      # larger documents: "download it to open it"
MAX_PART_BYTES = 12 * 1024 * 1024      # uncompressed size of any one XML part we read
MAX_MEDIA_BYTES = 8 * 1024 * 1024      # one picture inside a document
MAX_SLIDES, MAX_PARAS_PER_SLIDE, MAX_PARAS = 150, 60, 2500
MAX_SHEETS, MAX_ROWS, MAX_COLS = 8, 200, 26
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_KIND = {"pptx": "pptx", "pptm": "pptx", "ppsx": "pptx", "docx": "docx", "docm": "docx", "xlsx": "xlsx", "xlsm": "xlsx"}
_LEGACY = {"ppt": "PowerPoint", "doc": "Word", "xls": "Excel", "odp": "OpenDocument", "ods": "OpenDocument", "odt": "OpenDocument"}
_IMG_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
              "webp": "image/webp", "bmp": "image/bmp"}


class _Refused(Exception):
    pass


def _q(prefix: str, tag: str) -> str:
    return "{" + _NS[prefix] + "}" + tag


def _xml(zf: zipfile.ZipFile, name: str):
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise _Refused("This document is too large to preview — download it to open it.")
    data = zf.read(name)
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:      # no entity tricks in a document we only read
        raise _Refused("This document can't be previewed.")
    return ET.fromstring(data)


def _rels(zf, part: str) -> dict:
    d, f = posixpath.split(part)
    root = _xml(zf, posixpath.join(d, "_rels", f + ".rels"))
    out = {}
    if root is not None:
        for r in root:
            tgt = r.get("Target") or ""
            if r.get("TargetMode") == "External":
                continue
            out[r.get("Id")] = posixpath.normpath(tgt.lstrip("/") if tgt.startswith("/") else posixpath.join(d, tgt))
    return out


def _para_text(p, prefix="a") -> str:
    parts = []
    for el in p.iter():
        if el.tag == _q(prefix, "t") and el.text:
            parts.append(el.text)
        elif el.tag in (_q(prefix, "br"), _q(prefix, "tab")):
            parts.append(" ")
    return "".join(parts).strip()


def _pptx(zf) -> dict:
    pres = _xml(zf, "ppt/presentation.xml")
    rels = _rels(zf, "ppt/presentation.xml")
    order = []
    if pres is not None:
        for sid in pres.iter(_q("p", "sldId")):
            t = rels.get(sid.get(_q("r", "id")))
            if t:
                order.append(t)
    if not order:
        order = sorted((n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[-1]))
    slides = []
    for part in order[:MAX_SLIDES]:
        root = _xml(zf, part)
        if root is None:
            continue
        title, body = "", []
        for sp in root.iter(_q("p", "sp")):
            ph = sp.find(".//" + _q("p", "ph"))
            is_title = ph is not None and (ph.get("type") or "") in ("title", "ctrTitle")
            texts = [t for t in (_para_text(p) for p in sp.iter(_q("a", "p"))) if t]
            if is_title and not title:
                title = " ".join(texts)
            else:
                body.extend(texts)
        # tables
        for tbl in root.iter(_q("a", "tbl")):
            for tr in tbl.iter(_q("a", "tr")):
                cells = [_para_text(tc) for tc in tr.iter(_q("a", "tc"))]
                if any(cells):
                    body.append(" | ".join(cells))
        rel = _rels(zf, part)
        images = []
        for blip in root.iter(_q("a", "blip")):
            tgt = rel.get(blip.get(_q("r", "embed")))
            if tgt and tgt.rsplit(".", 1)[-1].lower() in _IMG_TYPES and tgt not in images:
                images.append(tgt)
        notes = ""
        for tgt in rel.values():
            if "notesSlides/" in tgt:
                nroot = _xml(zf, tgt)
                if nroot is not None:
                    notes = " ".join(t for t in (_para_text(p) for p in nroot.iter(_q("a", "p"))) if t)[:1500]
        slides.append({"title": title[:300], "text": [t[:600] for t in body[:MAX_PARAS_PER_SLIDE]],
                       "images": images[:4], "notes": notes})
    return {"kind": "pptx", "slides": slides, "total": len(order), "truncated": len(order) > MAX_SLIDES}


def _docx(zf) -> dict:
    root = _xml(zf, "word/document.xml")
    if root is None:
        raise _Refused("This isn't a readable Word document.")
    blocks, count = [], 0
    body = root.find(_q("w", "body"))
    for el in (body if body is not None else []):
        if count >= MAX_PARAS:
            break
        if el.tag == _q("w", "p"):
            text = _para_text(el, "w")
            if not text:
                continue
            st = el.find(_q("w", "pPr") + "/" + _q("w", "pStyle"))
            style = (st.get(_q("w", "val")) if st is not None else "") or ""
            level = 0
            m = re.match(r"(?i)heading\s*([1-6])", style)
            if m:
                level = int(m.group(1))
            elif style.lower() == "title":
                level = 1
            blocks.append({"t": "h" if level else "p", "level": level, "text": text[:2000]})
            count += 1
        elif el.tag == _q("w", "tbl"):
            rows = []
            for tr in el.iter(_q("w", "tr")):
                rows.append([_para_text(tc, "w")[:200] for tc in tr.iter(_q("w", "tc"))][:12])
                if len(rows) >= 60:
                    break
            blocks.append({"t": "table", "rows": rows})
            count += 1
    return {"kind": "docx", "blocks": blocks, "truncated": count >= MAX_PARAS}


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Za-z]+", ref).group(0).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(zf) -> dict:
    shared = []
    sst = _xml(zf, "xl/sharedStrings.xml")
    if sst is not None:
        for si in sst.iter(_q("s", "si")):
            shared.append("".join(t.text or "" for t in si.iter(_q("s", "t")))[:300])
            if len(shared) > 200_000:
                break
    wb = _xml(zf, "xl/workbook.xml")
    rels = _rels(zf, "xl/workbook.xml")
    sheets = []
    if wb is None:
        raise _Refused("This isn't a readable Excel workbook.")
    names = list(wb.iter(_q("s", "sheet")))
    for sh in names[:MAX_SHEETS]:
        part = rels.get(sh.get(_q("r", "id")))
        root = _xml(zf, part) if part else None
        rows, width = [], 0
        if root is not None:
            for row in root.iter(_q("s", "row")):
                if len(rows) >= MAX_ROWS:
                    break
                cells = {}
                for c in row.iter(_q("s", "c")):
                    ref = c.get("r") or ""
                    if not ref:
                        continue
                    ci = _col_index(ref)
                    if ci >= MAX_COLS:
                        continue
                    t, v = c.get("t"), c.find(_q("s", "v"))
                    if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(_q("s", "t")))
                    elif v is not None and v.text is not None:
                        val = v.text
                        if t == "b":
                            val = "TRUE" if val == "1" else "FALSE"
                    else:
                        val = ""
                    cells[ci] = val[:200]
                if cells:
                    width = max(width, max(cells) + 1)
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        sheets.append({"name": (sh.get("name") or "Sheet")[:60], "rows": rows, "cols": min(width, MAX_COLS),
                       "truncated": len(rows) >= MAX_ROWS})
    return {"kind": "xlsx", "sheets": sheets, "total": len(names)}


# ── true-to-the-original rendering ─────────────────────────────────────
# Pulling text out of the XML (above) loses layout, fonts, colours and shapes. When LibreOffice is installed
# the document is converted to a PDF once (cached in tmp/office_pdf, keyed by path + size + modified time) and the
# page shows it in the same viewer as any PDF. Without LibreOffice the plain preview above is used.
RENDER_TIMEOUT = 120
_RENDER_EXT = {"pptx", "pptm", "ppsx", "ppt", "pps", "odp", "docx", "docm", "doc", "odt", "xlsx", "xlsm", "xls", "ods"}
_PDF_DIR = TMP_DIR / "office_pdf"
_render_lock = threading.Lock()        # one conversion at a time: LibreOffice is heavy


def _soffice():
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_to_pdf(src: Path):
    """Return (pdf_path, None) or (None, reason)."""
    exe = _soffice()
    if not exe:
        return None, "LibreOffice isn't installed on this server."
    st = src.stat()
    key = hashlib.sha256(f"{src}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:32]
    out = _PDF_DIR / (key + ".pdf")
    if out.is_file():
        return out, None
    with _render_lock:
        if out.is_file():
            return out, None
        _PDF_DIR.mkdir(parents=True, exist_ok=True)
        for old in sorted(_PDF_DIR.glob("*.pdf"), key=lambda x: x.stat().st_mtime)[:-30]:   # keep the newest 30
            old.unlink(missing_ok=True)
        work = Path(tempfile.mkdtemp(prefix="job-", dir=_PDF_DIR))
        try:
            inp = work / ("in." + src.suffix.lstrip(".").lower())       # a plain name: nothing from the file name reaches the command
            shutil.copyfile(src, inp)
            env = dict(os.environ, HOME=str(work))
            cmd = [exe, "--headless", "--norestore", "--nologo", "--nolockcheck", "--nodefault",
                   f"-env:UserInstallation={(work / 'profile').as_uri()}",
                   "--convert-to", "pdf", "--outdir", str(work / "out"), str(inp)]
            try:
                subprocess.run(cmd, cwd=str(work), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=RENDER_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                return None, "Rendering took too long."
            made = work / "out" / "in.pdf"
            if not made.is_file() or made.stat().st_size < 100:
                return None, "LibreOffice couldn't open this file."
            shutil.move(str(made), str(out))
            return out, None
        finally:
            shutil.rmtree(work, ignore_errors=True)


@app.route("/api/file/office/pdf")
@require_auth
def api_file_office_pdf():
    p, err = _open(_current_user(), request.args.get("path", ""))
    if err:
        return err
    if p.suffix.lstrip(".").lower() not in _RENDER_EXT:
        return jsonify({"error": "Not an Office document"}), 400
    if p.stat().st_size > MAX_FILE_BYTES:
        return jsonify({"error": "This document is too large to preview — download it to open it."}), 413
    pdf, why = _convert_to_pdf(p)
    if not pdf:
        return jsonify({"error": why}), 503
    resp = Response(pdf.read_bytes(), mimetype="application/pdf")
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Disposition"] = "inline"
    return resp


def _open(user, raw):
    try:
        p = _safe(raw, user)
    except PermissionError as e:
        return None, (jsonify({"error": str(e)}), 403)
    if not p.is_file():
        return None, (jsonify({"error": "File not found"}), 404)
    return p, None


@app.route("/api/file/office")
@require_auth
def api_file_office():
    p, err = _open(_current_user(), request.args.get("path", ""))
    if err:
        return err
    ext = p.suffix.lstrip(".").lower()
    lo = bool(_soffice())
    if ext in _LEGACY and lo and p.stat().st_size <= MAX_FILE_BYTES:
        return jsonify({"kind": "render", "name": p.name, "lo": True})   # only LibreOffice can open the old formats
    if ext in _LEGACY:
        return jsonify({"kind": "legacy", "format": _LEGACY[ext],
                        "note": f"Older {_LEGACY[ext]} files can't be shown in the browser. Download it to open it, "
                                f"or save it as a modern file (.pptx / .docx / .xlsx) and open that."})
    kind = _KIND.get(ext)
    if not kind:
        return jsonify({"error": "Not an Office document"}), 400
    if p.stat().st_size > MAX_FILE_BYTES:
        return jsonify({"kind": "toolarge", "note": "This document is too large to preview — download it to open it."})
    try:
        with zipfile.ZipFile(p) as zf:
            out = {"pptx": _pptx, "docx": _docx, "xlsx": _xlsx}[kind](zf)
    except _Refused as e:
        return jsonify({"kind": "toolarge", "note": str(e)})
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError):
        return jsonify({"kind": "broken", "note": "This document couldn't be read — it may be damaged, "
                                                   "password-protected or not really an Office file."})
    out["name"] = p.name
    out["lo"] = lo                  # true: the page may also offer the exact LibreOffice rendering
    return jsonify(out)


@app.route("/api/file/office/raw")
@require_auth
def api_file_office_raw():
    """The document's own bytes, for the in-browser renderers (modern formats only, size-limited)."""
    p, err = _open(_current_user(), request.args.get("path", ""))
    if err:
        return err
    if p.suffix.lstrip(".").lower() not in _KIND:
        return jsonify({"error": "Not an Office document"}), 400
    if p.stat().st_size > MAX_FILE_BYTES:
        return jsonify({"error": "This document is too large to preview — download it to open it."}), 413
    resp = Response(p.read_bytes(), mimetype="application/octet-stream")
    resp.headers["Cache-Control"] = "private, max-age=60"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/api/file/office/media")
@require_auth
def api_file_office_media():
    p, err = _open(_current_user(), request.args.get("path", ""))
    if err:
        return err
    name = request.args.get("name", "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if p.suffix.lstrip(".").lower() not in _KIND or ext not in _IMG_TYPES or not name.startswith(("ppt/media/", "word/media/")):
        return jsonify({"error": "Not found"}), 404
    try:
        with zipfile.ZipFile(p) as zf:
            info = zf.getinfo(name)
            if info.file_size > MAX_MEDIA_BYTES:
                return jsonify({"error": "Picture too large"}), 413
            data = zf.read(name)
    except (zipfile.BadZipFile, KeyError, OSError):
        return jsonify({"error": "Not found"}), 404
    resp = Response(data, mimetype=_IMG_TYPES[ext])
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
