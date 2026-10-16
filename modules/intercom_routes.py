"""
modules/intercom_routes.py — intercom HTTP routes (the live audio is the /ws/intercom WebSocket).

Using the intercom (listen and talk to rooms) needs the per-account "Intercom" permission.
Setting it up (rooms, testing devices, pairing Bluetooth) needs that and an Admin or the Super User:
talking through the house's speakers is a different thing from deciding which speakers exist.

Actions that touch hardware (scan, pair, test) are rate-limited per account.
"""

import functools
from flask import jsonify
from flask import request
from modules.config import _MAC_ROUTE_RE
from modules.activity import _audit
from modules.error_log import _current_user, _is_admin_level
from modules.auth import _intercom_rate_check
from modules.web import app
from modules import intercom, intercom_bt
from modules.decorators import require_intercom_access


def _guarded(fn):
    """Any unexpected failure becomes a readable message and an entry in the error log, not a blank 500."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            try:
                from modules.error_log import _log_exception
                _log_exception(e, "intercom", request.path)
            except Exception:
                pass
            msg = intercom.A.explain_error(e, "audio device")
            return jsonify({"error": f"The intercom hit an unexpected problem. {msg} "
                                     "The details were written to the error log."}), 500
    return wrapper


def _limited():
    ok, retry = _intercom_rate_check(_current_user()["id"])
    if ok:
        return None
    resp = jsonify({"error": f"Too many intercom actions. Try again in {retry}s."})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry)
    return resp


def _admin_only():
    if not _is_admin_level(_current_user()):
        return jsonify({"error": "Only an Admin can change how the intercom is set up."}), 403
    return None


def _mac():
    mac = str((request.get_json(silent=True) or {}).get("mac", ""))
    return mac if _MAC_ROUTE_RE.match(mac) else None


# ── overview ──────────────────────────────────────────────────────────
@app.route("/api/intercom/overview")
@require_intercom_access
@_guarded
def api_ic_overview():
    """Everything the page needs to draw itself, fast (no Bluetooth probing)."""
    user = _current_user()
    return jsonify({"rooms": intercom.room_states(), "devices": intercom.devices(),
                    "admin": _is_admin_level(user), "engine": intercom.startup_summary()})


@app.route("/api/intercom/health")
@require_intercom_access
@_guarded
def api_ic_health():
    """The set-up checklist. Slower (it asks the Bluetooth stack), so the page loads it afterwards."""
    return jsonify(intercom.health())


@app.route("/api/intercom/devices")
@require_intercom_access
@_guarded
def api_ic_devices():
    return jsonify(intercom.devices(fresh=request.args.get("fresh") == "1"))


# ── rooms (admin) ─────────────────────────────────────────────────────
@app.route("/api/intercom/rooms")
@require_intercom_access
@_guarded
def api_ic_rooms():
    """The rooms as configured (device ids included), for the editor."""
    bad = _admin_only()
    if bad:
        return bad
    return jsonify(intercom.rooms())


@app.route("/api/intercom/rooms", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_room_add():
    bad = _admin_only()
    if bad:
        return bad
    room, err = intercom.save_room(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    _audit("intercom_room_added", _current_user(), target=room["name"])
    return jsonify(room)


@app.route("/api/intercom/rooms/<rid>", methods=["PUT"])
@require_intercom_access
@_guarded
def api_ic_room_edit(rid):
    bad = _admin_only()
    if bad:
        return bad
    room, err = intercom.save_room(request.get_json(silent=True) or {}, room_id=rid)
    if err:
        return jsonify({"error": err}), 400 if err != "No such room." else 404
    _audit("intercom_room_changed", _current_user(), target=room["name"])
    return jsonify(room)


@app.route("/api/intercom/rooms/<rid>", methods=["DELETE"])
@require_intercom_access
@_guarded
def api_ic_room_del(rid):
    bad = _admin_only()
    if bad:
        return bad
    room = intercom.get_room(rid)
    if not intercom.delete_room(rid):
        return jsonify({"error": "No such room."}), 404
    _audit("intercom_room_removed", _current_user(), target=(room or {}).get("name", rid))
    return jsonify({"ok": True})


# ── device tests (admin) ──────────────────────────────────────────────
@app.route("/api/intercom/devices/test-output", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_test_output():
    bad = _admin_only()
    if bad:
        return bad
    lim = _limited()
    if lim:
        return lim
    dev = str((request.get_json(silent=True) or {}).get("device", ""))
    err = intercom.play_test_tone(dev)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


@app.route("/api/intercom/devices/test-input", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_test_input():
    bad = _admin_only()
    if bad:
        return bad
    lim = _limited()
    if lim:
        return lim
    dev = str((request.get_json(silent=True) or {}).get("device", ""))
    res = intercom.measure_input(dev)
    return jsonify(res), (200 if res.get("ok") else 400)


# ── Bluetooth (admin) ─────────────────────────────────────────────────
@app.route("/api/intercom/volumes")
@require_intercom_access
@_guarded
def api_ic_volumes():
    """The system volume of every speaker, for the sliders (polled every second or two)."""
    bad = _admin_only()
    if bad:
        return bad
    if intercom.A._vol_tool() is None:
        return jsonify({"supported": False, "volumes": {}})
    out = {}
    for d in intercom.devices():
        if d["kind"] == "output":
            v = intercom.A.get_volume(d["id"])
            if v is not None:
                out[d["id"]] = v
    return jsonify({"supported": True, "volumes": out})


_vol_audit_at: dict = {}


@app.route("/api/intercom/volume", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_volume():
    bad = _admin_only()
    if bad:
        return bad
    d = request.get_json(silent=True) or {}
    dev = str(d.get("device", ""))
    if not any(x["id"] == dev and x["kind"] == "output" for x in intercom.devices()):
        return jsonify({"error": "That speaker isn't available."}), 404
    vol, muted = d.get("volume"), d.get("muted")
    try:
        vol = None if vol is None else max(0.0, min(1.0, float(vol)))
    except (TypeError, ValueError):
        return jsonify({"error": "volume must be a number between 0 and 1"}), 400
    if muted is not None:
        muted = bool(muted)
    if vol is None and muted is None:
        return jsonify({"error": "Nothing to change"}), 400
    ok = intercom.A.set_volume(dev, vol, muted)
    key = (_current_user()["id"], dev)
    import time as _t
    if _t.time() - _vol_audit_at.get(key, 0) > 30:          # a drag is many requests: one log entry per half minute
        _vol_audit_at[key] = _t.time()
        _audit("intercom_volume", _current_user(), target=dev, detail=("muted" if muted else f"{int(round((vol or 0) * 100))}%"))
    if not ok:
        return jsonify({"ok": False, "error": "Couldn't change that speaker's volume."}), 502
    return jsonify({"ok": True, **(intercom.A.get_volume(dev) or {})})


@app.route("/api/intercom/bluetooth/devices")
@require_intercom_access
@_guarded
def api_ic_bt_devices():
    bad = _admin_only()
    if bad:
        return bad
    return jsonify(intercom_bt.list_devices())


@app.route("/api/intercom/bluetooth/scan", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_bt_scan():
    bad = _admin_only()
    if bad:
        return bad
    lim = _limited()
    if lim:
        return lim
    res = intercom_bt.scan()
    _audit("intercom_bt_scan", _current_user(), detail=f"found={len(res['devices'])}")
    intercom.devices(fresh=True)
    return jsonify(res)


@app.route("/api/intercom/bluetooth/add", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_bt_add():
    """Pair, trust and connect in one go."""
    bad = _admin_only()
    if bad:
        return bad
    lim = _limited()
    if lim:
        return lim
    mac = _mac()
    if not mac:
        return jsonify({"error": "Invalid Bluetooth address"}), 400
    res = intercom_bt.add_device(mac)
    _audit("intercom_bt_add", _current_user(), target=mac, detail=res["message"])
    intercom.devices(fresh=True)
    return jsonify(res)


@app.route("/api/intercom/bluetooth/<action>", methods=["POST"])
@require_intercom_access
@_guarded
def api_ic_bt_action(action):
    bad = _admin_only()
    if bad:
        return bad
    if action not in ("connect", "disconnect"):
        return jsonify({"error": "Unknown action"}), 404
    lim = _limited()
    if lim:
        return lim
    mac = _mac()
    if not mac:
        return jsonify({"error": "Invalid Bluetooth address"}), 400
    ok, msg = (intercom_bt.connect if action == "connect" else intercom_bt.disconnect)(mac)
    _audit(f"intercom_bt_{action}", _current_user(), target=mac, detail=msg)
    intercom.devices(fresh=True)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/intercom/bluetooth/device/<mac>", methods=["DELETE"])
@require_intercom_access
@_guarded
def api_ic_bt_forget(mac):
    bad = _admin_only()
    if bad:
        return bad
    if not _MAC_ROUTE_RE.match(mac):
        return jsonify({"error": "Invalid Bluetooth address"}), 400
    ok, msg = intercom_bt.remove(mac)
    _audit("intercom_bt_forget", _current_user(), target=mac, detail=msg)
    intercom.devices(fresh=True)
    return jsonify({"ok": ok, "message": msg})
