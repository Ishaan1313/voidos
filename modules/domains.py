"""
modules/domains.py — custom domains for app tiles (Cloudflare named tunnels).

An admin connects a domain that is on Cloudflare once (an API token plus the domain name). After
that, any app tile can be given a hostname such as jellyfin.example.com and Void OS does the rest
through Cloudflare's API:

    1. creates a named tunnel for the tile            (once, when the hostname is first set)
    2. creates a proxied CNAME  host -> <id>.cfargotunnel.com   (once, and when the hostname changes)
    3. points the tunnel's ingress at the tile's local port     (each time the tunnel starts, so a
                                                                 gate proxy's port is always right)
    4. runs `cloudflared tunnel run` with the tunnel's token    (token fetched at each start, never stored)

HTTPS comes from Cloudflare and nothing needs to be port-forwarded. Everything here is best effort
and returns (value, error_message) instead of raising, so a Cloudflare outage never takes the
dashboard down; the tile just shows the reason.

The API token is stored encrypted (modules/secret_store.py), owner-only, and is never sent back to
the browser.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

from flask import jsonify, request

from modules import secret_store
from modules.activity import _audit
from modules.config import _void_path
from modules.decorators import require_admin
from modules.error_log import _current_user
from modules.file_helpers import _aload
from modules.web import _atomic_write_json, app

DOMAIN_FILE = _void_path("VOID_DOMAIN_FILE", "custom_domain.json")
_API = "https://api.cloudflare.com/client/v4"
_TIMEOUT = 12
_lock = threading.Lock()

# a DNS name: labels of letters/digits/hyphens, 1-63 chars each, no leading/trailing hyphen
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


# ── stored connection ────────────────────────────────────────────────
def _load() -> dict:
    """{'token', 'domain', 'zone_id', 'account_id'} or {} when nothing (readable) is stored."""
    with _lock:
        try:
            raw = json.loads(DOMAIN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if secret_store.is_sealed(raw):
            try:
                raw = secret_store.unseal(raw)
            except ValueError:
                return {}
        elif raw and secret_store.available():
            try:
                _atomic_write_json(DOMAIN_FILE, secret_store.seal(raw), private=True)  # migrate a plain file
            except OSError:
                pass
        return raw if isinstance(raw, dict) and raw.get("token") and raw.get("zone_id") else {}


def _save(d: dict) -> None:
    with _lock:
        _atomic_write_json(DOMAIN_FILE, secret_store.seal(d), private=True)


def _forget() -> None:
    with _lock:
        try:
            DOMAIN_FILE.unlink()
        except OSError:
            pass


def connected_domain() -> str:
    return _load().get("domain", "")


# ── Cloudflare API ───────────────────────────────────────────────────
def _cf(method: str, path: str, token: str, body=None):
    """-> (result, error). `error` is a plain-language sentence or None."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "User-Agent": "voidos"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        return None, _explain(payload, e.code)
    except Exception as e:
        return None, f"Couldn't reach Cloudflare ({type(e).__name__}). Check this server's internet connection."
    if not payload.get("success", True):
        return None, _explain(payload, 200)
    return payload.get("result"), None


def _explain(payload: dict, status: int) -> str:
    errs = payload.get("errors") or []
    msg = (errs[0].get("message") if errs and isinstance(errs[0], dict) else "") or ""
    if status in (401, 403) or "authentication" in msg.lower() or "permission" in msg.lower():
        return ("Cloudflare refused the token" + (f" ({msg})" if msg else "") +
                ". It needs: Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit, Zone > Zone > Read.")
    if status == 429:
        return "Cloudflare is rate-limiting requests right now. Try again in a minute."
    return msg or f"Cloudflare returned an error (HTTP {status})."


# ── connect / disconnect ─────────────────────────────────────────────
def connect(token: str, domain: str):
    """Verify the token against the domain and store it. -> (info dict, error)."""
    token = (token or "").strip()
    domain = (domain or "").strip().lower().rstrip(".")
    if not token:
        return None, "Paste a Cloudflare API token."
    if not domain or "." not in domain or not all(_LABEL.match(p) for p in domain.split(".")):
        return None, "Enter the domain as it is in Cloudflare, e.g. example.com (no https://, no path)."
    zones, err = _cf("GET", f"/zones?name={domain}", token)
    if err:
        return None, err
    if not zones:
        return None, (f"{domain} isn't a zone this token can see. Check the spelling, that the domain is "
                      "added to Cloudflare, and that the token's zone resources include it.")
    z = zones[0]
    zone_id, account_id = z.get("id"), (z.get("account") or {}).get("id")
    if not zone_id or not account_id:
        return None, "Cloudflare didn't return the zone's account. Try a token with Zone > Zone > Read."
    # Probe the two permissions that provisioning needs, so a wrong token fails here, not on first use.
    _, err = _cf("GET", f"/zones/{zone_id}/dns_records?per_page=1", token)
    if err:
        return None, "The token can't read DNS records for this zone. " + err
    _, err = _cf("GET", f"/accounts/{account_id}/cfd_tunnel?per_page=1&is_deleted=false", token)
    if err:
        return None, "The token can't manage Cloudflare Tunnels. " + err
    _save({"token": token, "domain": domain, "zone_id": zone_id, "account_id": account_id})
    return {"domain": domain}, None


# ── hostname checks ──────────────────────────────────────────────────
def check_hostname(host: str, exclude_app_id: str | None = None):
    """-> (normalised host, error). The host must sit under the connected domain and not be used by
    another tile."""
    cfg = _load()
    if not cfg:
        return None, "Connect your Cloudflare domain first."
    host = (host or "").strip().lower().rstrip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0]
    dom = cfg["domain"]
    if host and "." not in host:
        host = f"{host}.{dom}"  # just "jellyfin" is enough
    if host != dom and not host.endswith("." + dom):
        return None, f"The address has to end in .{dom}"
    if not all(_LABEL.match(p) for p in host.split(".")):
        return None, "That isn't a valid hostname. Use letters, digits and hyphens only."
    for a in _aload():
        if a.get("id") != exclude_app_id and (a.get("custom_hostname") or "").lower() == host:
            return None, f"{host} is already used by the \"{a.get('name', 'another')}\" tile."
    return host, None


# ── per-tile provisioning ────────────────────────────────────────────
def prepare(app_id: str, app_name: str, host: str, cur: dict):
    """Create what a tile needs for `host`: its named tunnel and DNS record. `cur` is the tile's
    saved state ({'cf_tunnel_id', 'cf_dns_id', 'custom_hostname'}). Idempotent.
    -> ({'cf_tunnel_id', 'cf_dns_id'}, error)."""
    cfg = _load()
    if not cfg:
        return None, "The Cloudflare domain isn't connected (or the server's secret key changed). Connect it again."
    tok, acct, zone = cfg["token"], cfg["account_id"], cfg["zone_id"]

    tunnel_id = cur.get("cf_tunnel_id")
    if tunnel_id:
        t, err = _cf("GET", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}", tok)
        if err or not t or t.get("deleted_at"):
            tunnel_id = None  # gone on Cloudflare's side: make a new one
    if not tunnel_id:
        t, err = _cf("POST", f"/accounts/{acct}/cfd_tunnel", tok,
                     {"name": f"voidos-{app_id}-{re.sub(r'[^a-z0-9]+', '-', app_name.lower()).strip('-')[:30] or 'app'}",
                      "config_src": "cloudflare"})
        if err:
            return None, err
        tunnel_id = t["id"]

    def undo(err):
        # don't leave a tunnel behind that this call just created and can't finish setting up
        if tunnel_id != cur.get("cf_tunnel_id"):
            _cf("DELETE", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}", tok)
        return None, err

    target = f"{tunnel_id}.cfargotunnel.com"
    dns_id = cur.get("cf_dns_id") if cur.get("custom_hostname") == host else None
    recs, err = _cf("GET", f"/zones/{zone}/dns_records?name={host}", tok)
    if err:
        return undo(err)
    for r in recs or []:
        if r.get("type") == "CNAME" and str(r.get("content", "")).endswith(".cfargotunnel.com"):
            dns_id = r["id"]  # ours (or a previous tunnel of ours): reuse and repoint
        else:
            return undo(f"{host} already has a {r.get('type')} record in Cloudflare. Remove it there or "
                        "pick another name; Void OS won't overwrite records it didn't create.")
    body = {"type": "CNAME", "name": host, "content": target, "proxied": True,
            "comment": f"Void OS app tile {app_id}"}
    if dns_id:
        _, err = _cf("PUT", f"/zones/{zone}/dns_records/{dns_id}", tok, body)
    else:
        r, err = _cf("POST", f"/zones/{zone}/dns_records", tok, body)
        dns_id = (r or {}).get("id")
    if err:
        return undo(err)
    return {"cf_tunnel_id": tunnel_id, "cf_dns_id": dns_id}, None


def start_info(tunnel_id: str, host: str, port: int):
    """Called by the tunnel supervisor at every (re)start: point the tunnel's ingress at `port` and
    fetch the run token. -> (token, error)."""
    cfg = _load()
    if not cfg:
        return None, "The Cloudflare domain isn't connected."
    tok, acct = cfg["token"], cfg["account_id"]
    _, err = _cf("PUT", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}/configurations", tok, {
        "config": {"ingress": [{"hostname": host, "service": f"http://127.0.0.1:{int(port)}"},
                               {"service": "http_status:404"}]}})
    if err:
        return None, err
    token, err = _cf("GET", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}/token", tok)
    if err:
        return None, err
    return token, None


def release(tunnel_id: str | None, dns_id: str | None) -> None:
    """Best effort clean-up when a tile drops its custom hostname or is deleted. Runs in a thread;
    nothing here may raise."""
    def work():
        cfg = _load()
        if not cfg:
            return
        tok = cfg["token"]
        try:
            if dns_id:
                _cf("DELETE", f"/zones/{cfg['zone_id']}/dns_records/{dns_id}", tok)
            if tunnel_id:
                # the tunnel's process was just stopped; Cloudflare needs a moment to see it as gone
                import time
                for _ in range(4):
                    _, err = _cf("DELETE", f"/accounts/{cfg['account_id']}/cfd_tunnel/{tunnel_id}", tok)
                    if not err:
                        break
                    time.sleep(3)
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


# ── routes ───────────────────────────────────────────────────────────
@app.route("/api/admin/domain", methods=["GET"])
@require_admin
def api_domain_get():
    cfg = _load()
    used = [a.get("name") for a in _aload() if a.get("custom_hostname")]
    return jsonify({"connected": bool(cfg), "domain": cfg.get("domain", ""),
                    "stored_but_unreadable": (not cfg) and DOMAIN_FILE.exists(),
                    "tiles_using": used})


@app.route("/api/admin/domain", methods=["PUT"])
@require_admin
def api_domain_connect():
    d = request.get_json(silent=True) or {}
    info, err = connect(d.get("api_token", ""), d.get("domain", ""))
    if err:
        return jsonify({"error": err}), 400
    _audit("domain_connected", _current_user(), target=info["domain"])
    return jsonify({"connected": True, "domain": info["domain"]})


@app.route("/api/admin/domain", methods=["DELETE"])
@require_admin
def api_domain_disconnect():
    used = [a.get("name") for a in _aload() if a.get("custom_hostname")]
    if used:
        return jsonify({"error": "Remove the custom address from these tiles first: " + ", ".join(map(str, used))}), 400
    dom = connected_domain()
    _forget()
    _audit("domain_disconnected", _current_user(), target=dom)
    return jsonify({"connected": False})
