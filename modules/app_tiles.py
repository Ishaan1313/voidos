"""
modules/app_tiles.py — Dashboard app tiles, tunnels/gate settings per tile, Panic button.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify
from flask import request
from urllib.parse import urlparse
from werkzeug.security import generate_password_hash
import secrets
from modules import domains, errors, state
from modules.config import PIN_LEN, PORT as _VOID_PORT

# Ports an app tile may never expose through a tunnel: Void OS itself, and well-known services that were
# never meant for the internet (SSH, mail, the Docker API, common databases).
_NEVER_PUBLISH = {_VOID_PORT, 22, 23, 25, 111, 2049, 2375, 2376, 3306, 5432, 6379, 11211, 27017}
from modules.file_helpers import _aload, _asave, _clean_tile_icon
from modules.web import _apps_lock, app
from modules.activity import _audit
from modules.error_log import _can_use_docker, _current_user, _is_admin_level
from modules.decorators import require_admin, require_auth, require_docker_access, require_superuser


# ══════════════════════════════════════════════════════════════════════
# ROUTES — APPS (any auth)
# ══════════════════════════════════════════════════════════════════════
def _extract_port(url: str):
    """Pull the local port + any path suffix out of a tile URL like
    'http://localhost:8080/foo'. Returns (port:int|None, path:str)."""
    try:
        p = urlparse(url)
        return p.port, (p.path or "")
    except Exception:
        return None, ""

def _public_app_view(a: dict, is_admin: bool) -> dict:
    """What a client actually receives for one app tile. Non-tunneled tiles
    are returned unchanged. Tunneled tiles get their clickable `url`
    overridden with the live tunnel URL (or left inert with a status while
    connecting/erroring) — guests should never see the raw localhost:PORT
    address, since it isn't reachable off-LAN anyway and reveals internal
    port layout for no benefit to them."""
    out = {"id": a["id"], "name": a["name"], "icon": a["icon"], "description": a.get("description", "")}
    if not a.get("tunnel_enabled"):
        out["url"] = a.get("url", "#")
        return out
    st = APP_TUNNELS.get(a["id"])
    status = st["status"] if st else "stopped"
    tunnel_url = st["url"] if st else None
    out["tunnel_enabled"] = True
    out["tunnel_status"] = status
    if is_admin and st and st.get("error"):
        out["tunnel_error"] = st["error"]
    if tunnel_url:
        _, path = _extract_port(a.get("url", ""))
        out["url"] = tunnel_url + (path if path and path != "/" else "")
    else:
        out["url"] = "#"  # not connected yet — frontend shows a disabled/connecting state instead of a dead link
    if is_admin:
        out["local_port"] = a.get("local_port")
        out["gate_enabled"] = bool(a.get("gate_enabled"))
        out["gate_is_pin"] = a.get("gate_is_pin", True)
        out["custom_hostname"] = a.get("custom_hostname") or ""
        out["hidden_from_guests"] = bool(a.get("hidden_from_guests"))
        out["hidden_from_admins"] = bool(a.get("hidden_from_admins"))
        if a.get("tunnel_enabled_before_panic"):
            out["tunnel_enabled_before_panic"] = True
        if st:
            out["gate_status"] = st.get("gate_status")
            if st.get("gate_error"):
                out["gate_error"] = st["gate_error"]
    return out

@app.route("/api/apps", methods=["GET"])
@require_auth
def api_apps_get():
    user = _current_user()
    is_admin = _is_admin_level(user)
    role = user.get("role")
    apps = _aload()
    if role != "superuser":
        # Super User sees every tile unfiltered. An ordinary Admin never even
        # receives a tile the Super User has hidden from Admins — same
        # "doesn't exist as far as you're concerned" treatment already used
        # for e.g. a Guest probing another Admin's session id. Guests get the
        # Guest-facing flag applied on top of whatever visibility already
        # scoped them (additive, not a replacement).
        key = "hidden_from_admins" if is_admin else "hidden_from_guests"
        apps = [a for a in apps if not a.get(key)]
    return jsonify([_public_app_view(a, is_admin) for a in apps])

@app.route("/api/apps/reorder", methods=["PUT"])
@require_admin
def api_apps_reorder():
    """Persists a new tile display order — the array order in apps.json IS
    the display order (see api_apps_get), so this just resorts that array
    to match the id sequence the drag-and-drop grid sends up. Any id not
    in the request keeps its relative position, appended after the ones
    that were explicitly reordered — protects against a stale client
    payload (e.g. a tile installed by someone else mid-drag) silently
    dropping an app from the list instead of just leaving it be."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    order = d.get("order")
    if not isinstance(order, list) or not order:
        return jsonify({"error": "order must be a non-empty list of app ids"}), 400
    with _apps_lock:
        apps = _aload()
        by_id = {a["id"]: a for a in apps}
        seen = set()
        new_list = []
        for aid in order:
            a = by_id.get(str(aid))
            if a and a["id"] not in seen:
                new_list.append(a)
                seen.add(a["id"])
        for a in apps:  # anything the client didn't send stays, appended in its old relative order
            if a["id"] not in seen:
                new_list.append(a)
        _asave(new_list)
    _audit("apps_reordered", user)
    return jsonify({"ok": True})

def _validate_tunnel_fields(d: dict, existing_url_fallback: str = ""):
    """Shared by add + edit. Returns (tunnel_enabled, local_port, error)."""
    tunnel_enabled = bool(d.get("tunnel_enabled"))
    if not tunnel_enabled:
        return False, None, None
    url_for_port = str(d.get("url", "")).strip() or existing_url_fallback
    port, _ = _extract_port(url_for_port)
    if d.get("local_port"):
        try: port = int(d["local_port"])
        except (TypeError, ValueError): port = None
    if not port or not (1 <= port <= 65535):
        return True, None, "Tunneling needs a valid local port — include it in the URL (e.g. http://localhost:8080) or set it explicitly."
    if port in _NEVER_PUBLISH:
        return True, None, f"Port {port} can't be published through a tunnel (it's Void OS itself or a service that should never face the internet)."
    return True, port, None

def _validate_gate_fields(d: dict, tunnel_enabled: bool, existing_pin_hash: str = None, existing_ttl: float = 12,
                           existing_is_pin: bool = None, existing_pin_length: int = None):
    """Shared by add + edit. Returns (gate_enabled, pin_hash, ttl_hours, is_pin, pin_length, error).
    A blank `gate_pin` on edit means "keep the existing secret" — never forces
    re-entry on every save, same treatment as any other password-style field;
    its mode/length travel with it unchanged, since the display mode has to
    match what's actually inside the hash. A blank `gate_pin` on create (no
    existing hash to fall back to) is an error. Turning the gate OFF preserves
    the stored hash/ttl/mode rather than wiping them, so re-enabling later
    doesn't force the secret to be retyped.

    `gate_mode` ("pin" or "password") lets the admin choose explicitly, same
    as share links — PIN mode shows an animated keypad and requires an
    all-digit secret (since the keypad UI can only submit digits); Password
    mode allows anything. Omitted mode falls back to the tile's existing mode,
    defaulting to PIN for a brand new gate (this feature was PIN-only
    originally, so that stays the default)."""
    gate_enabled = bool(d.get("gate_enabled"))
    if not gate_enabled:
        return False, existing_pin_hash, existing_ttl, existing_is_pin, existing_pin_length, None
    if not tunnel_enabled:
        return True, None, None, None, None, "A gate needs its own tunnel enabled first — a gate with no tunnel guards nothing."
    ttl = d.get("gate_cookie_ttl_hours", existing_ttl or 12)
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        return True, None, None, None, None, "Invalid gate cookie duration"
    if not (1 <= ttl <= 720):  # 1 hour .. 30 days
        return True, None, None, None, None, "Gate cookie duration must be between 1 and 720 hours"

    mode = d.get("gate_mode")
    is_pin = (mode == "pin") if mode in ("pin", "password") else (
        existing_is_pin if existing_is_pin is not None else True)

    pin = str(d.get("gate_pin") or "")
    if not pin:
        if existing_pin_hash:
            return True, existing_pin_hash, ttl, existing_is_pin, existing_pin_length, None  # keep current secret unchanged
        return True, None, None, None, None, (
            f"A PIN of exactly {PIN_LEN} digits is required to enable the gate." if is_pin
            else "A password is required to enable the gate.")
    if is_pin:
        if not (pin.isdigit() and len(pin) == PIN_LEN):
            return True, None, None, None, None, f"PIN must be exactly {PIN_LEN} digits."
    else:
        if len(pin) < GATE_PASSWORD_MIN_LEN or len(pin) > 64:
            return True, None, None, None, None, f"Password must be {GATE_PASSWORD_MIN_LEN}-64 characters."
    return True, generate_password_hash(pin), ttl, is_pin, (len(pin) if is_pin else None), None

def _domain_fields(d: dict, cur: dict, tunnel_enabled: bool, aid: str, name: str):
    """Custom-domain part of an add/edit. -> (fields to merge into the tile, error).
    `custom_hostname` absent from the request keeps what the tile has; "" removes it. A hostname is
    only acted on while the tile's tunnel is on (it rides on that tunnel)."""
    if "custom_hostname" not in d or not tunnel_enabled:
        return {}, None
    want = str(d.get("custom_hostname") or "").strip()
    if not want:
        if cur.get("custom_hostname"):
            return {"custom_hostname": "", "cf_tunnel_id": None, "cf_dns_id": None}, None
        return {}, None
    host, err = domains.check_hostname(want, exclude_app_id=aid)
    if err:
        return None, err
    prov, err = domains.prepare(aid, name, host, cur)
    if err:
        return None, err
    if cur.get("cf_dns_id") and cur.get("cf_dns_id") != prov["cf_dns_id"]:
        domains.release(None, cur["cf_dns_id"])  # the hostname changed: drop the old record
    return {"custom_hostname": host, **prov}, None

@app.route("/api/apps", methods=["POST"])
@require_admin
def api_apps_add():
    d=request.get_json(silent=True) or {}
    url = str(d.get("url","")).strip()
    scheme = urlparse(url).scheme.lower()
    if url and scheme not in ("http","https") and not url.startswith("/"):
        return jsonify({"error":"App URL must start with http://, https://, or /"}), 400
    tunnel_enabled, local_port, err = _validate_tunnel_fields(d)
    if err: return jsonify({"error": err}), 400
    gate_enabled, gate_pin_hash, gate_ttl, gate_is_pin, gate_pin_length, gerr = _validate_gate_fields(d, tunnel_enabled)
    if gerr: return jsonify({"error": gerr}), 400
    name = str(d.get("name","App"))[:64]
    new_id = secrets.token_hex(4)
    dom_fields, derr = _domain_fields(d, {}, tunnel_enabled, new_id, name)
    if derr: return jsonify({"error": derr}), 400
    e={"id":new_id,"name":name,
       "url":(url or "#")[:512],"icon":_clean_tile_icon(d.get("icon","🔗")),
       "description":str(d.get("description",""))[:128],
       "tunnel_enabled": tunnel_enabled, "local_port": local_port,
       "gate_enabled": gate_enabled, "gate_pin_hash": gate_pin_hash,
       "gate_cookie_ttl_hours": gate_ttl, "gate_is_pin": gate_is_pin, "gate_pin_length": gate_pin_length,
       "hidden_from_guests": False, "hidden_from_admins": False}
    e.update(dom_fields)
    with _apps_lock:
        a=_aload(); a.append(e); _asave(a)
    if tunnel_enabled:
        gate_cfg = ({"pin_hash": gate_pin_hash, "ttl_hours": gate_ttl, "name": name,
                      "is_pin": gate_is_pin, "pin_length": gate_pin_length} if gate_enabled else None)
        ok, terr = _start_app_tunnel(e["id"], local_port, gate_cfg, named_cfg(e))
        if not ok: return jsonify({**e, "tunnel_error": terr, "gate_pin_hash": None})
    return jsonify({**e, "gate_pin_hash": None})  # never echo the hash back, same as any other password field

@app.route("/api/apps/<aid>", methods=["PUT"])
@require_admin
def api_apps_edit(aid):
    d=request.get_json(silent=True) or {}
    with _apps_lock:
        a=_aload()
        idx = next((i for i,x in enumerate(a) if x.get("id")==aid), None)
        if idx is None: return jsonify({"error":"not found"}), 404
        cur = a[idx]
        if cur.get("hidden_from_admins") and _current_user().get("role") != "superuser":
            return jsonify({"error":"not found"}), 404  # hidden from Admins: doesn't exist for them
        url = str(d.get("url", cur.get("url",""))).strip()
        scheme = urlparse(url).scheme.lower()
        if url and scheme not in ("http","https") and not url.startswith("/"):
            return jsonify({"error":"App URL must start with http://, https://, or /"}), 400
        tunnel_enabled, local_port, err = _validate_tunnel_fields(d, existing_url_fallback=cur.get("url",""))
        if err: return jsonify({"error": err}), 400
        gate_enabled, gate_pin_hash, gate_ttl, gate_is_pin, gate_pin_length, gerr = _validate_gate_fields(
            d, tunnel_enabled, existing_pin_hash=cur.get("gate_pin_hash"),
            existing_ttl=cur.get("gate_cookie_ttl_hours", 12),
            existing_is_pin=cur.get("gate_is_pin"), existing_pin_length=cur.get("gate_pin_length"))
        if gerr: return jsonify({"error": gerr}), 400
        name = str(d.get("name",cur["name"]))[:64]
        old_tunnel_id, old_dns_id = cur.get("cf_tunnel_id"), cur.get("cf_dns_id")
        dom_fields, derr = _domain_fields(d, cur, tunnel_enabled, aid, name)
        if derr: return jsonify({"error": derr}), 400
        cur.update(dom_fields)
        cur.update({"name":name,
                    "url":(url or "#")[:512],
                    "icon":_clean_tile_icon(d.get("icon",cur["icon"])),
                    "description":str(d.get("description",cur.get("description","")))[:128],
                    "tunnel_enabled": tunnel_enabled, "local_port": local_port,
                    "gate_enabled": gate_enabled, "gate_pin_hash": gate_pin_hash,
                    "gate_cookie_ttl_hours": gate_ttl, "gate_is_pin": gate_is_pin, "gate_pin_length": gate_pin_length})
        if "tunnel_enabled" in d:
            # A deliberate manual touch of this tile's tunnel state supersedes
            # whatever Panic recorded — otherwise a later Undo could clobber
            # a choice the admin made well after Panic fired.
            cur.pop("tunnel_enabled_before_panic", None)
        _asave(a)
    if tunnel_enabled:
        gate_cfg = ({"pin_hash": gate_pin_hash, "ttl_hours": gate_ttl, "name": name,
                      "is_pin": gate_is_pin, "pin_length": gate_pin_length} if gate_enabled else None)
        ok, terr = _start_app_tunnel(aid, local_port, gate_cfg, named_cfg(cur))
        if old_tunnel_id and not cur.get("cf_tunnel_id"):
            domains.release(old_tunnel_id, old_dns_id)  # custom address removed: the old tunnel can go
        if not ok: return jsonify({**cur, "tunnel_error": terr, "gate_pin_hash": None})
    else:
        _stop_app_tunnel(aid)
    return jsonify({**cur, "gate_pin_hash": None})

@app.route("/api/apps/<aid>", methods=["DELETE"])
@require_admin
def api_apps_del(aid):
    with _apps_lock:
        apps = _aload()
        t = next((a for a in apps if a.get("id")==aid), None)
        if t is None or (t.get("hidden_from_admins") and _current_user().get("role") != "superuser"):
            return jsonify({"error":"not found"}), 404
        _asave([a for a in apps if a.get("id")!=aid])
    _stop_app_tunnel(aid)
    if t.get("cf_tunnel_id") or t.get("cf_dns_id"):
        domains.release(t.get("cf_tunnel_id"), t.get("cf_dns_id"))  # tidy Cloudflare too
    return jsonify({"ok":True})

@app.route("/api/apps/<aid>/visibility", methods=["PUT"])
@require_admin
def api_apps_visibility(aid):
    """Any Admin/Super User may set hidden_from_guests. Only the Super User
    may set hidden_from_admins — mirroring exactly how only the Super User
    can toggle an Admin's own terminal/docker/guest_file_view flags: an
    ordinary Admin gets a flat rejection for the whole request rather than
    a partial silent-ignore, same shape as the existing rank-management
    routes (e.g. api_users_promote) reject an unauthorized attempt."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    if "hidden_from_admins" in d and user.get("role") != "superuser":
        return jsonify({"error": "Only the Super User can change an app's visibility to Admins"}), 403
    with _apps_lock:
        apps = _aload()
        idx = next((i for i, x in enumerate(apps) if x.get("id") == aid), None)
        if idx is None:
            return jsonify({"error": "not found"}), 404
        cur = apps[idx]
        if cur.get("hidden_from_admins") and user.get("role") != "superuser":
            return jsonify({"error": "not found"}), 404
        changed = []
        if "hidden_from_guests" in d:
            cur["hidden_from_guests"] = bool(d["hidden_from_guests"])
            changed.append(f"guests={'hidden' if cur['hidden_from_guests'] else 'visible'}")
        if "hidden_from_admins" in d:
            cur["hidden_from_admins"] = bool(d["hidden_from_admins"])
            changed.append(f"admins={'hidden' if cur['hidden_from_admins'] else 'visible'}")
        _asave(apps)
    if changed:
        _audit("app_visibility_change", user, target=cur["name"], detail=", ".join(changed))
    return jsonify(_public_app_view(cur, True))

@app.route("/api/admin/panic", methods=["POST"])
@require_superuser
def api_admin_panic():
    """Tier 1 (default, no Docker permission required): stop every currently
    tunneled app tile, recording tunnel_enabled_before_panic on each so
    Undo can restore exactly what was actually on — never the dashboard's
    own Cloudflare tunnel (CF_TUNNEL_EN/TUNNEL_PROC), which this never
    touches, so triggering Panic while connected through it can't lock the
    admin out of undoing their own action.
    Tier 2 (opt-in, requires Docker permission): also stops every running
    container. Validated before anything runs, so an unauthorized Tier 2
    request fails closed rather than partially executing Tier 1 first."""
    user = _current_user()
    d = request.get_json(silent=True) or {}
    tier2 = bool(d.get("tier2"))
    if tier2 and not _can_use_docker(user):
        return jsonify({"error": "Docker access is required for Tier 2 (stopping containers)"}), 403

    with _apps_lock:
        apps = _aload()
        stopped_ids, stopped = [], []
        for a in apps:
            if a.get("tunnel_enabled"):
                a["tunnel_enabled_before_panic"] = True
                a["tunnel_enabled"] = False
                stopped_ids.append(a["id"]); stopped.append(a["name"])
        _asave(apps)
    for a_id in stopped_ids:
        _stop_app_tunnel(a_id)

    containers_stopped = None
    if tier2:
        containers_stopped = 0
        if state.DOCKER_OK:
            try:
                for c in state._docker.containers.list():
                    try:
                        c.stop(timeout=10)
                        containers_stopped += 1
                    except Exception:
                        pass
            except Exception:
                pass

    _audit("panic_triggered", user, detail=f"tier2={tier2}, tunnels_stopped={len(stopped)}"
           + (f", containers_stopped={containers_stopped}" if tier2 else ""))
    return jsonify({"tunnels_stopped": len(stopped), "tiles": stopped, "containers_stopped": containers_stopped})

@app.route("/api/admin/panic/undo", methods=["POST"])
@require_superuser
def api_admin_panic_undo():
    """Restores tunnel_enabled on every tile that still carries
    tunnel_enabled_before_panic — and only those. A tile an admin manually
    re-touched after Panic fired has that flag cleared by the normal edit
    route already (see api_apps_edit), so Undo can never clobber a later,
    deliberate choice; this only ever restores what Panic itself turned off
    and nothing has touched since."""
    user = _current_user()
    with _apps_lock:
        apps = _aload()
        restored = []
        for a in apps:
            if a.get("tunnel_enabled_before_panic"):
                a["tunnel_enabled"] = True
                a.pop("tunnel_enabled_before_panic", None)
                restored.append(a)
        _asave(apps)
    for a in restored:
        if a.get("local_port"):
            gate_cfg = None
            if a.get("gate_enabled") and a.get("gate_pin_hash"):
                gate_cfg = {"pin_hash": a["gate_pin_hash"], "ttl_hours": a.get("gate_cookie_ttl_hours", 12),
                            "name": a.get("name"), "is_pin": a.get("gate_is_pin", True),
                            "pin_length": a.get("gate_pin_length")}
            _start_app_tunnel(a["id"], int(a["local_port"]), gate_cfg, named_cfg(a))
    _audit("panic_undo", user, detail=f"tunnels_restored={len(restored)}")
    return jsonify({"tunnels_restored": len(restored), "tiles": [a["name"] for a in restored]})

@app.route("/api/admin/docker/ports", methods=["GET"])
@require_docker_access
def api_docker_ports():
    """Published host ports of currently running containers, so the 'Add App
    Tile' tunnel picker can offer a dropdown instead of asking the admin to
    remember which host port maps to which container."""
    if not state.DOCKER_OK: return jsonify({"error": state.DOCKER_ERROR, "ports": []}), 200
    out = []
    try:
        for c in state._docker.containers.list():  # running only — a stopped container has no live port to tunnel
            for _, bindings in (c.ports or {}).items():
                for b in (bindings or []):
                    hp = b.get("HostPort")
                    if hp:
                        out.append({"container": c.name, "port": int(hp)})
    except Exception as e:
        return jsonify({"error": errors.describe(e), "ports": []}), 200
    # de-dupe (a container can publish the same host port under multiple protocol entries)
    seen = set(); dedup = []
    for p in out:
        key = (p["container"], p["port"])
        if key not in seen: seen.add(key); dedup.append(p)
    return jsonify({"ports": dedup})


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global APP_TUNNELS, GATE_PASSWORD_MIN_LEN, _start_app_tunnel, _stop_app_tunnel, named_cfg
    from modules.tunnels import APP_TUNNELS, GATE_PASSWORD_MIN_LEN, _start_app_tunnel, _stop_app_tunnel, named_cfg
