"""
modules/cloud_storage.py — Dropbox + Google Drive integration for Void OS.

Connect a personal cloud account, browse its files from inside Void OS,
pull files down into your own storage, and push files back up. This is a
browse/import/export model, not a continuous two-way sync/mount — the
honest shape to build without a background sync daemon or an rclone
dependency (continuous sync lives in modules/cloud_sync.py).

Dependency-free by design (same spirit as modules/gate_proxy.py) — every
call here is a plain HTTPS request built with the stdlib's own
urllib.request, matching the pattern voidos.py already uses for GHCR/
Docker Hub tag lookups rather than adding `requests` as a new dependency
for what is, underneath, a handful of REST calls.

Nothing in this module reads voidos.py's globals or the filesystem beyond
what's explicitly passed in — provider config (client id/secret) and
per-user token records are the caller's responsibility to load/save,
exactly like gate_proxy.py's own separation of "mechanism here, state and
config owned by the caller."

SECURITY NOTE: OAuth access/refresh tokens are returned to the caller as
plain dicts. modules/cloud_routes.py stores them in cloud_tokens.json
encrypted with a key derived from VOID_SECRET_KEY (modules/secret_store.py)
and readable by the owner only; continuous sync hands them to rclone through
its environment, never an rclone config file. That protects a copied data
folder or backup, not a server where someone can read .env as well — it's
still not a secrets vault, which matches this app's trust model (a
single-tenant home server). Worth knowing if you point VOID_FILE_ROOT or the
data directory at something with a wider audience than that.
"""
from __future__ import annotations
import json
import re
import time
import mimetypes
import urllib.request
import urllib.parse
import urllib.error

USER_AGENT = "VoidOS-CloudStorage/1.0"
TIMEOUT = 20

# ── Provider endpoint tables ────────────────────────────────────────────
PROVIDERS = {
    "dropbox": {
        "label": "Dropbox",
        "authorize_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "scope": "",  # Dropbox scopes are set in the App Console, not the URL, for legacy apps
        "extra_authorize_params": {"token_access_type": "offline"},
    },
    "gdrive": {
        "label": "Google Drive",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # drive.readonly would block the "export a Void OS file back to
        # Drive" direction, and drive.file would hide files the user
        # didn't create through this app — full `drive` is the minimum
        # scope that supports both browse-existing and push-new-files.
        "scope": "https://www.googleapis.com/auth/drive",
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
}


class CloudError(Exception):
    """Raised for anything an admin/user should see as a clear error message
    rather than a stack trace — expired auth, a provider outage, a bad path."""


def _req(url, method="GET", headers=None, data=None, timeout=TIMEOUT):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    r = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.getheaders())
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError) as e:
        raise CloudError(f"Network error reaching {urllib.parse.urlparse(url).netloc}: {e}")


def _json(status, body, context):
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        parsed = {"raw": body[:300].decode("utf-8", "replace")}
    if status >= 400:
        msg = parsed.get("error_description") or parsed.get("error", {}).get("message") \
            if isinstance(parsed.get("error"), dict) else parsed.get("error")
        raise CloudError(f"{context} failed ({status}): {msg or parsed}")
    return parsed


# ── OAuth flow ───────────────────────────────────────────────────────────
def build_authorize_url(provider: str, client_id: str, redirect_uri: str, state: str) -> str:
    cfg = PROVIDERS[provider]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if cfg["scope"]:
        params["scope"] = cfg["scope"]
    params.update(cfg["extra_authorize_params"])
    return cfg["authorize_url"] + "?" + urllib.parse.urlencode(params)


def exchange_code(provider: str, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Authorization code -> {access_token, refresh_token, expires_at, ...}."""
    cfg = PROVIDERS[provider]
    form = {
        "code": code, "grant_type": "authorization_code",
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    status, body, _ = _req(cfg["token_url"], "POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(form).encode())
    tok = _json(status, body, f"{cfg['label']} token exchange")
    return _normalize_token(tok)


def refresh_token(provider: str, client_id: str, client_secret: str, refresh_tok: str) -> dict:
    cfg = PROVIDERS[provider]
    form = {
        "refresh_token": refresh_tok, "grant_type": "refresh_token",
        "client_id": client_id, "client_secret": client_secret,
    }
    status, body, _ = _req(cfg["token_url"], "POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(form).encode())
    tok = _json(status, body, f"{cfg['label']} token refresh")
    out = _normalize_token(tok)
    if not out.get("refresh_token"):              # providers don't re-send it on refresh (Dropbox never does)
        out["refresh_token"] = refresh_tok        # (setdefault would keep the empty string _normalize_token puts there)
    return out


def _normalize_token(tok: dict) -> dict:
    expires_in = tok.get("expires_in", 3600)
    return {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + float(expires_in) - 30,  # 30s safety margin
        "account": tok.get("account_id") or tok.get("id_token") or "",
    }


def ensure_fresh(provider: str, client_id: str, client_secret: str, token_record: dict) -> dict:
    """Returns a token_record guaranteed not to be within its 30s expiry margin,
    refreshing it first if needed. Caller is responsible for persisting the
    result if it changed (compare object identity or just always re-save)."""
    if token_record.get("expires_at", 0) > time.time():
        return token_record
    if not token_record.get("refresh_token"):
        raise CloudError("Session expired and no refresh token is on file — reconnect this account.")
    fresh = refresh_token(provider, client_id, client_secret, token_record["refresh_token"])
    merged = dict(token_record)
    merged.update(fresh)
    if not merged.get("refresh_token"):           # never lose the one we already had
        merged["refresh_token"] = token_record["refresh_token"]
    return merged


# ── Dropbox ──────────────────────────────────────────────────────────────
def _dbx_list(access_token: str, path: str) -> list:
    status, body, _ = _req("https://api.dropboxapi.com/2/files/list_folder", "POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        data=json.dumps({"path": path, "limit": 500}).encode())
    data = _json(status, body, "Dropbox list folder")
    out = []
    for e in data.get("entries", []):
        out.append({
            "name": e.get("name"), "id": e.get("path_lower"),
            "type": "dir" if e.get(".tag") == "folder" else "file",
            "size": e.get("size"), "modified": e.get("server_modified"),
        })
    return sorted(out, key=lambda x: (x["type"] != "dir", x["name"].lower()))


def _dbx_download(access_token: str, remote_path: str, dest_fp) -> None:
    r = urllib.request.Request("https://content.dropboxapi.com/2/files/download",
        headers={"Authorization": f"Bearer {access_token}",
                 "Dropbox-API-Arg": json.dumps({"path": remote_path})})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                dest_fp.write(chunk)
    except urllib.error.HTTPError as e:
        raise CloudError(f"Dropbox download failed ({e.code}): {e.read()[:200]}")


def _dbx_upload(access_token: str, remote_folder: str, filename: str, local_fp, size: int) -> dict:
    remote_path = (remote_folder.rstrip("/") + "/" + filename) if remote_folder else "/" + filename
    r = urllib.request.Request("https://content.dropboxapi.com/2/files/upload",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/octet-stream",
                 "Dropbox-API-Arg": json.dumps({"path": remote_path, "mode": "add", "autorename": True})},
        data=local_fp.read())
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return _json(resp.status, resp.read(), "Dropbox upload")
    except urllib.error.HTTPError as e:
        raise CloudError(f"Dropbox upload failed ({e.code}): {e.read()[:200]}")


# ── Google Drive ─────────────────────────────────────────────────────────
def _gd_list(access_token: str, folder_id: str) -> list:
    fid = folder_id or "root"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fid):
        raise CloudError("That isn't a valid Google Drive folder id.")
    q = urllib.parse.quote(f"'{fid}' in parents and trashed=false")
    url = ("https://www.googleapis.com/drive/v3/files?q=" + q +
           "&fields=" + urllib.parse.quote("files(id,name,mimeType,size,modifiedTime)") +
           "&pageSize=500")
    status, body, _ = _req(url, headers={"Authorization": f"Bearer {access_token}"})
    data = _json(status, body, "Google Drive list")
    out = []
    for f in data.get("files", []):
        is_dir = f.get("mimeType") == "application/vnd.google-apps.folder"
        out.append({
            "name": f.get("name"), "id": f.get("id"),
            "type": "dir" if is_dir else "file",
            "size": int(f["size"]) if f.get("size") else None,
            "modified": f.get("modifiedTime"),
        })
    return sorted(out, key=lambda x: (x["type"] != "dir", x["name"].lower()))


def _gd_download(access_token: str, file_id: str, dest_fp) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", file_id or ""):
        raise CloudError("That isn't a valid Google Drive file id.")
    url = f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id, safe='')}?alt=media"
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                dest_fp.write(chunk)
    except urllib.error.HTTPError as e:
        raise CloudError(f"Google Drive download failed ({e.code}): {e.read()[:200]}")


def _gd_upload(access_token: str, folder_id: str, filename: str, local_fp, size: int) -> dict:
    boundary = "voidos-cloud-boundary-8f61c2"
    meta = {"name": filename}
    if folder_id and folder_id != "root":
        meta["parents"] = [folder_id]
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n"
        f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
    ).encode() + local_fp.read() + f"\r\n--{boundary}--".encode()
    r = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": f"multipart/related; boundary={boundary}"},
        data=body)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return _json(resp.status, resp.read(), "Google Drive upload")
    except urllib.error.HTTPError as e:
        raise CloudError(f"Google Drive upload failed ({e.code}): {e.read()[:200]}")


# ── Dispatch ─────────────────────────────────────────────────────────────
def list_folder(provider: str, access_token: str, remote_path_or_id: str) -> list:
    if provider == "dropbox":
        return _dbx_list(access_token, remote_path_or_id or "")
    if provider == "gdrive":
        return _gd_list(access_token, remote_path_or_id or "root")
    raise CloudError(f"Unknown provider: {provider}")


def download_to(provider: str, access_token: str, remote_id: str, dest_fp) -> None:
    if provider == "dropbox":
        return _dbx_download(access_token, remote_id, dest_fp)
    if provider == "gdrive":
        return _gd_download(access_token, remote_id, dest_fp)
    raise CloudError(f"Unknown provider: {provider}")


def upload_from(provider: str, access_token: str, remote_folder: str, filename: str, local_fp, size: int) -> dict:
    if provider == "dropbox":
        return _dbx_upload(access_token, remote_folder, filename, local_fp, size)
    if provider == "gdrive":
        return _gd_upload(access_token, remote_folder, filename, local_fp, size)
    raise CloudError(f"Unknown provider: {provider}")
