"""
modules/intercom_bt.py — Bluetooth for the intercom: scan, pair, connect, forget.

Audio itself (opening a speaker or microphone, whatever kind it is) is modules/intercom_audio.py. This
module only deals with getting a Bluetooth device paired and connected so that the operating system
offers it as an audio device; once it does, it simply shows up in the intercom's device list.

  Linux    bluetoothctl (BlueZ). Audio profiles are cross-checked against PipeWire's real nodes.
  Windows  Windows' own WinRT Bluetooth APIs, called through Windows PowerShell 5.1.

What Windows does not expose to apps is said plainly rather than faked: there is no API to force a
paired device's audio profiles up or down, so connect() nudges Windows and reports the real result,
and disconnect() explains what to do instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from modules import intercom_audio

_devices_path: Path | None = None
_atomic_write = None
_devices_lock = threading.RLock()


def configure(devices_path: Path, atomic_write_fn):
    """Where the remembered friendly names live (written with the app's atomic JSON writer)."""
    global _devices_path, _atomic_write
    _devices_path = devices_path
    _atomic_write = atomic_write_fn
    try:
        if not _devices_path.exists():
            _atomic_write(_devices_path, {})
    except Exception:
        pass


def list_audio_nodes() -> list:
    """The OS's audio devices in the shape this module has always used: {name, description, kind}."""
    be = intercom_audio.get_backend()
    if be is None:
        return []
    return [{"id": d["id"], "name": d["id"], "description": d["name"],
             "kind": "source" if d["kind"] == "input" else "sink"} for d in be.devices() if d["id"] != "default"]


# ══════════════════════════════════════════════════════════════════════
# BLUETOOTH MANAGER
# ══════════════════════════════════════════════════════════════════════
_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

# UUID substrings bluetoothctl prints that indicate a usable profile.
# "Audio Sink" == A2DP sink (device can be sent audio -> usable as OUTPUT).
# "Handsfree"/"Headset" == HFP/HSP (device offers a mic -> usable as INPUT too).
_A2DP_MARKERS = ("Audio Sink", "A/V Remote Control Target")
_HFP_MARKERS = ("Handsfree", "Headset")


def _run_bt(args: list, timeout: float = 10) -> str:
    try:
        r = subprocess.run([*_BTCTL, *args], capture_output=True,
                            text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def _load_devices() -> dict:
    """mac -> persisted metadata (friendly name, last-known profile
    capabilities, per-user preferred routing). Merged with live
    bluetoothctl state at read time — this store only remembers what
    bluetoothctl itself would forget across restarts/rescans."""
    try:
        return json.loads(_devices_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_devices(d: dict):
    with _devices_lock:
        _atomic_write(_devices_path, d)


def bluetoothctl_info(mac: str) -> dict:
    """Parse `bluetoothctl info <mac>` into paired/connected/name/profiles.
    Cross-checked against real PipeWire nodes by list_devices() below,
    since a device can advertise HFP in its UUIDs and still not actually
    switch into HSP/HFP mode — a real, common failure with cheap
    peripherals (FR §5). This function only reports what the device
    *claims*; list_devices() is what reports what's actually usable."""
    out = _run_bt(["info", mac])
    info = {"name": mac, "paired": False, "connected": False,
             "claims_a2dp": False, "claims_hfp": False}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            info["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Alias:") and info["name"] == mac:
            info["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            info["paired"] = "yes" in line
        elif line.startswith("Connected:"):
            info["connected"] = "yes" in line
        elif line.startswith("UUID:"):
            if any(m in line for m in _A2DP_MARKERS):
                info["claims_a2dp"] = True
            if any(m in line for m in _HFP_MARKERS):
                info["claims_hfp"] = True
    return info


def _is_powered() -> bool:
    return "Powered: yes" in _run_bt(["show"], timeout=5)


def _ensure_powered() -> tuple[bool, str]:
    """Discovery against a powered-off controller either errors immediately
    or, on some BlueZ versions, just silently finds nothing — which looks
    identical to 'no devices nearby' from the UI's side and is the single
    most common reason a scan comes back empty with a real device sitting
    right there in pairing mode. Tried automatically before every scan,
    not just reported as a host-status warning, since it's usually fixable
    without any manual step at all."""
    if _is_powered():
        return True, ""
    _run_bt(["power", "on"], timeout=8)
    time.sleep(1)  # bluetoothd needs a moment to bring the radio up
    if _is_powered():
        return True, ""
    return False, ("Couldn't power on the Bluetooth adapter automatically — "
                    "run 'bluetoothctl power on' on the host directly, and check "
                    "'rfkill list bluetooth' isn't showing it soft-blocked.")


def scan(duration: int = 20) -> dict:
    """Blocking scan — the route calling this runs it in a background
    thread (see voidos.py's /api/intercom/bluetooth/scan) since a Flask
    request thread shouldn't block for the full scan window. Returns
    {devices, warning} rather than a bare list — a scan that legitimately
    found nothing (device out of range, not actually in pairing mode) and
    a scan that never really ran (adapter off, rfkill-blocked, bluetoothd
    not responding) both used to look identical: an empty array with zero
    explanation. Now the latter case comes back with a warning message
    the frontend surfaces instead of just re-showing an empty grid."""
    powered, warning = _ensure_powered()
    if not powered:
        return {"devices": list_devices(), "warning": warning}
    d = max(1, min(duration, 30))
    # The bluetoothctl-side --timeout already bounds this to `d` seconds;
    # the Python-side subprocess timeout below only exists as an external
    # backstop if bluetoothctl itself hangs, so it needs real headroom
    # above `d`, not the flat default=10 _run_bt otherwise uses — with the
    # old default, any scan longer than 10s could get killed by Python
    # *before* bluetoothctl's own --timeout ever fired, discarding the
    # scan's results and silently reporting them as "".
    found = _scan_session(d)
    devices = list_devices()
    known = {x["mac"] for x in devices}
    for mac, f in found.items():                 # seen during the scan but not yet in BlueZ's own list
        if mac not in known:
            devices.append({"mac": mac, "name": f.get("name") or mac, "paired": False, "connected": False,
                            "profiles": {"a2dp": False, "hfp": False, "verified": False}})
    for x in devices:
        f = found.get(x["mac"])
        x["seen_now"] = bool(f)
        if f and f.get("rssi") is not None:
            x["rssi"] = f["rssi"]
    def _unnamed(x):                      # BlueZ shows an unnamed device as its address, with : or - between the bytes
        return (x.get("name") or "").upper().replace("-", ":") == x["mac"]
    devices.sort(key=lambda x: (not x.get("seen_now"), _unnamed(x), not is_amazon_speaker(x.get("name", "")),
                                x.get("name", "").lower()))
    warning = "" if devices or found else (
        "Scan completed but nothing was found — the adapter may not actually be discovering. "
        "Try again, or run 'bluetoothctl scan on' directly on the host to see what it reports.")
    return {"devices": devices, "warning": warning}


# ══════════════════════════════════════════════════════════════════════
# bluetoothctl sessions: a live scan, and the always-on "accept inbound connections" agent
# ══════════════════════════════════════════════════════════════════════
_BTCTL = ["bluetoothctl"]                       # replaced in tests
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r")
_DEV_RE = re.compile(r"\bDevice ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b(.*)$")


class _Session:
    """One interactive bluetoothctl process. It runs on a pseudo-terminal where there is one, because
    bluetoothctl only flushes its prompts (\"Authorize service ... (yes/no):\") promptly to a terminal;
    on a plain pipe the output can sit in a buffer. Reading is done by a thread into a queue."""

    def __init__(self, cmd):
        import queue
        self.q = queue.Queue()
        self._fd = None
        try:
            import pty
            master, slave = pty.openpty()
            self.p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            import os as _os
            _os.close(slave)
            self._fd = master
            threading.Thread(target=self._read_fd, daemon=True, name="bt-session-read").start()
        except ImportError:                       # no pty on this platform: plain pipes
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            threading.Thread(target=self._read_pipe, daemon=True, name="bt-session-read").start()

    def _read_fd(self):
        import os as _os
        while True:
            try:
                data = _os.read(self._fd, 4096)
            except OSError:
                data = b""
            self.q.put(data)
            if not data:
                return

    def _read_pipe(self):
        while True:
            data = self.p.stdout.read1(4096) if hasattr(self.p.stdout, "read1") else self.p.stdout.read(1)
            self.q.put(data or b"")
            if not data:
                return

    def send(self, line: str):
        raw = (line + "\n").encode()
        try:
            if self._fd is not None:
                import os as _os
                _os.write(self._fd, raw)
            else:
                self.p.stdin.write(raw)
                self.p.stdin.flush()
        except (OSError, ValueError):
            pass

    def chunks(self, timeout: float):
        """Yield decoded text as it arrives; ends on timeout (yields '') or when the process closes (None)."""
        import queue
        while True:
            try:
                data = self.q.get(timeout=timeout)
            except queue.Empty:
                yield ""
                return
            if not data:
                yield None
                return
            yield data.decode("utf-8", "replace")

    def close(self):
        try:
            self.send("quit")
            self.p.wait(timeout=2)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
        if self._fd is not None:
            try:
                import os as _os
                _os.close(self._fd)
            except OSError:
                pass


def _lines(buf: str):
    """Split a stream buffer into complete lines and the unfinished tail."""
    buf = _ANSI_RE.sub("", buf)
    parts = buf.split("\n")
    return parts[:-1], parts[-1]


def _scan_session(seconds: int) -> dict:
    """Run discovery for `seconds` inside one bluetoothctl session and collect every device that shows up
    (name, signal strength). One continuous session beats the old one-shot call: nothing is lost when the
    process exits, and classic Bluetooth devices (an Echo in pairing mode) get a whole inquiry cycle."""
    found = {}
    try:
        s = _Session(_BTCTL)
    except OSError:
        return found
    try:
        s.send("scan on")
        end = time.time() + seconds
        tail = ""
        while time.time() < end:
            got = next(s.chunks(min(1.0, max(0.1, end - time.time()))), "")
            if got is None:
                break
            if not got:
                continue
            lines, tail = _lines(tail + got)
            for ln in lines:
                m = _DEV_RE.search(ln)
                if not m:
                    continue
                mac, rest = m.group(1).upper(), m.group(2).strip()
                d = found.setdefault(mac, {"name": "", "rssi": None})
                if "[DEL]" in ln:
                    found.pop(mac, None)
                    continue
                if rest.startswith(("Name:", "Alias:")):
                    d["name"] = rest.split(":", 1)[1].strip() or d["name"]
                elif rest.startswith("RSSI:"):
                    mm = re.search(r"(-?\d+)\)?\s*$", rest)
                    d["rssi"] = int(mm.group(1)) if mm else d["rssi"]
                elif "[NEW]" in ln and rest and ":" not in rest.split()[0]:
                    d["name"] = rest
        s.send("scan off")
    finally:
        s.close()
    return found


# ── accept inbound pairing and connections ───────────────────────────────
# The server presents itself as a Bluetooth audio device that anything nearby can pair with, so an Echo can
# be connected from the Alexa app ("Pair a new device" -> this server). It needs three things BlueZ does not
# do by itself: be discoverable and pairable, have an agent that answers pairing / authorisation prompts,
# and trust a device once it has paired so it can come back later without asking again.
# Keyboards, mice and game controllers are never accepted (an HID device could type into the server).
ACCEPT_INBOUND = os.environ.get("VOID_BT_ACCEPT", "on").strip().lower() not in ("0", "off", "false", "no")
ACCEPT_ANY_CLASS = os.environ.get("VOID_BT_ACCEPT_ALL", "").strip().lower() in ("1", "on", "true", "yes")
_acc = {"running": False, "discoverable": False, "recent": [], "stop": threading.Event(), "thread": None}
_audit_cb = None


def set_audit(fn):
    global _audit_cb
    _audit_cb = fn


_held: dict = {}                    # MAC -> time until which inbound pairing/connections from it are refused
HOLD_SECONDS = 300


def hold(mac: str, seconds: int = HOLD_SECONDS):
    _held[mac.upper()] = time.time() + seconds


def release_hold(mac: str):
    _held.pop(mac.upper(), None)


def is_held(mac: str) -> bool:
    until = _held.get(mac.upper(), 0)
    if until and until < time.time():
        _held.pop(mac.upper(), None)
        return False
    return bool(until)


def _acc_note(msg: str):
    _acc["recent"] = ([{"t": time.time(), "msg": msg}] + _acc["recent"])[:20]
    if _audit_cb:
        try:
            _audit_cb("intercom_bt_inbound", None, target="", detail=msg[:200])
        except Exception:
            pass


def _device_facts(mac: str) -> dict:
    """Name, icon and class of a device as BlueZ knows it right now."""
    out = _run_bt(["info", mac], timeout=6)
    facts = {"name": "", "icon": "", "class": ""}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:") and not facts["name"]:
            facts["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Alias:") and not facts["name"]:
            facts["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Icon:"):
            facts["icon"] = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Class:"):
            facts["class"] = line.split(":", 1)[1].strip()
    return facts


def _inbound_allowed(mac: str) -> tuple[bool, str]:
    f = _device_facts(mac)
    label = f["name"] or mac
    if is_held(mac):
        return False, f"Refused {label}: you just forgot it (press Add or Connect to use it again)"
    if not ACCEPT_ANY_CLASS and f["icon"].startswith("input-"):
        return False, f"Refused {label}: keyboards, mice and game controllers aren't accepted"
    return True, f"Accepted {label}"


def _has_audio(mac: str) -> bool:
    tag = mac.replace(":", "_").upper()
    return any(tag in (n.get("name") or "").upper() for n in list_audio_nodes())


def _settle_new_device(mac: str):
    """After pairing: trust it (so it can reconnect by itself) and bring up its audio connection.
    The device having connected is not enough. An Echo that reaches us over the radio still waits for the
    server to open the audio (A2DP) link, and tells its owner "couldn't connect" when that never happens; a
    plain `connect` is what asks BlueZ for the audio profiles, so it is sent even though the link is up,
    and repeated until the audio device shows up (or it is clear it won't)."""
    _run_bt(["trust", mac], timeout=8)
    for attempt in range(4):
        time.sleep(2.5)
        out = _run_bt(["connect", mac], timeout=20)
        time.sleep(1.5)
        if _has_audio(mac):
            _acc_note(f"Connected audio for {mac}")
            return
    _acc_note(f"{mac} connected, but its audio didn't come up: " + (out.strip().splitlines()[-1][:100] if out.strip() else "no answer"))


def _acceptor_main():
    stop = _acc["stop"]
    wait = 5
    while not stop.is_set():
        s = None
        try:
            if not shutil.which(_BTCTL[0]) and _BTCTL == ["bluetoothctl"]:
                stop.wait(60)
                continue
            _ensure_powered()
            s = _Session(_BTCTL)
            _acc["running"] = True
            for c in ("agent off", "agent NoInputNoOutput", "default-agent", "pairable-timeout 0",
                      "pairable on", "discoverable-timeout 0", "discoverable on"):
                s.send(c)
                time.sleep(0.25)
            _acc["discoverable"] = True
            current, tail, settled = None, "", set()
            while not stop.is_set():
                got = next(s.chunks(1.0), "")
                if got is None:
                    break                              # bluetoothctl exited: start again below
                text = tail + got if got else tail
                lines, tail = _lines(text)
                check = lines + ([tail] if tail else [])
                for ln in check:
                    low = ln.lower()
                    m = _DEV_RE.search(ln)
                    if m:
                        current = m.group(1).upper()
                    if "(yes/no)" in low:
                        if current:
                            ok, why = _inbound_allowed(current)
                            s.send("yes" if ok else "no")
                            if not ok:
                                _run_bt(["remove", current], timeout=8)
                            _acc_note(why)
                        else:
                            s.send("yes")
                        tail = ""
                    elif "enter pin code" in low or "request pin" in low:
                        s.send("0000")
                        tail = ""
                    elif "enter passkey" in low:
                        s.send("0")
                        tail = ""
                if tail and "(yes/no)" in tail.lower():
                    pass
                for ln in lines:
                    m = _DEV_RE.search(ln)
                    if m and "Paired: yes" in ln:
                        mac = m.group(1).upper()
                        if mac not in settled:
                            settled.add(mac)
                            threading.Thread(target=_settle_new_device, args=(mac,), daemon=True).start()
                    elif m and "Connected: yes" in ln and not is_held(m.group(1)) and m.group(1).upper() not in settled:
                        mac = m.group(1).upper()
                        settled.add(mac)
                        threading.Thread(target=_settle_new_device, args=(mac,), daemon=True).start()
                    elif m and "Connected: yes" in ln and is_held(m.group(1)):
                        mac = m.group(1).upper()
                        _run_bt(["disconnect", mac], timeout=8)
                        _run_bt(["remove", mac], timeout=8)
                        _acc_note(f"Disconnected {mac}: it was just forgotten")
                    elif m and "Connected: yes" in ln and not ACCEPT_ANY_CLASS:
                        mac = m.group(1).upper()
                        if _device_facts(mac)["icon"].startswith("input-"):
                            _run_bt(["disconnect", mac], timeout=8)
                            _run_bt(["remove", mac], timeout=8)
                            _acc_note(f"Disconnected {mac}: input devices aren't accepted")
            wait = 5
        except Exception:
            wait = min(wait * 2, 120)
        finally:
            _acc["running"] = False
            _acc["discoverable"] = False
            if s is not None:
                s.close()
        stop.wait(wait)


def start_acceptor():
    """Start accepting inbound Bluetooth connections (Linux only; a no-op elsewhere or when turned off with
    VOID_BT_ACCEPT=off)."""
    if sys.platform != "linux" or not ACCEPT_INBOUND or _acc["thread"] is not None:
        return False
    if not shutil.which("bluetoothctl"):
        return False
    _acc["stop"].clear()
    t = threading.Thread(target=_acceptor_main, daemon=True, name="bt-acceptor")
    _acc["thread"] = t
    t.start()
    return True


def acceptor_status() -> dict:
    return {"enabled": ACCEPT_INBOUND, "running": _acc["running"], "discoverable": _acc["discoverable"],
            "recent": [r["msg"] for r in _acc["recent"][:5]]}



def pair(mac: str) -> tuple[bool, str]:
    release_hold(mac)
    out = _run_bt(["pair", mac], timeout=20)
    ok = "Pairing successful" in out or "already paired" in out.lower()
    if not ok and "AlreadyExists" in out:
        ok = True
    return ok, out.strip().splitlines()[-1] if out.strip() else ("paired" if ok else "pairing failed")


def connect(mac: str) -> tuple[bool, str]:
    release_hold(mac)
    out = _run_bt(["connect", mac], timeout=15)
    ok = "Connection successful" in out or "already connected" in out.lower()
    if ok:
        return True, "connected"
    msg = out.strip().splitlines()[-1] if out.strip() else "connect failed"
    if "profile" in msg.lower() and not _pipewire_has_bluez_module():
        # The generic bluetoothctl error alone ("Connection profile not
        # found") gives no hint that the actual fix is a missing PipeWire
        # plugin, not a Bluetooth-side problem — see
        # _pipewire_has_bluez_module's docstring for why this is the
        # specific, common cause worth calling out here rather than
        # passing the raw BlueZ error straight through.
        msg += (" — PipeWire's Bluetooth audio support isn't loaded on this host "
                "(install libspa-0.2-bluetooth and restart PipeWire/WirePlumber); "
                "see the Intercom tab's host-status check for details.")
    return False, msg


_GONE_WORDS = ("successful", "notconnected", "not connected", "not available", "doesnotexist", "does not exist",
               "has been removed")


def disconnect(mac: str) -> tuple[bool, str]:
    """Disconnect, and treat "it wasn't connected" as success: BlueZ answers org.bluez.Error.NotConnected when the
    link is already down (the speaker dropped it first, or the state was stale), which is the result we want.
    If it genuinely refuses, blocking the device makes BlueZ tear the link down, and unblocking frees it again."""
    out = ""
    for attempt in range(2):
        out = _run_bt(["disconnect", mac], timeout=12)
        if any(w in out.lower() for w in _GONE_WORDS):
            return True, "disconnected"
        time.sleep(1)
    _run_bt(["block", mac], timeout=8)
    time.sleep(0.7)
    _run_bt(["unblock", mac], timeout=8)
    if not bluetoothctl_info(mac).get("connected"):
        return True, "disconnected"
    last = out.strip().splitlines()[-1] if out.strip() else "no answer from bluetoothctl"
    return False, "disconnect failed (" + last[:120] + ")"


def remove(mac: str) -> tuple[bool, str]:
    """Forget a device. It is disconnected and untrusted first, and for a few minutes anything it sends us is
    refused: a speaker that still remembers the pairing (an Echo does) would otherwise reconnect at once and be
    accepted again by the inbound agent."""
    hold(mac)
    _run_bt(["untrust", mac], timeout=6)
    disconnect(mac)
    out = _run_bt(["remove", mac], timeout=10)
    ok = any(w in out.lower() for w in _GONE_WORDS)
    if not ok:
        ok = "not available" in _run_bt(["info", mac], timeout=6).lower()      # already gone
    if ok:
        with _devices_lock:
            d = _load_devices()
            d.pop(mac, None)
            _save_devices(d)
    return ok, ("removed" if ok else "remove failed: " + (out.strip().splitlines()[-1][:120] if out.strip() else "no answer"))


def list_devices() -> list:
    """Paired + currently-visible devices, each annotated with *real*
    detected profile capability (cross-checked against live PipeWire
    nodes, not just the advertised UUIDs — FR §5's core requirement)."""
    macs = set()
    for line in _run_bt(["devices"]).splitlines():
        m = _MAC_RE.search(line)
        if m:
            macs.add(m.group(1))
    for line in _run_bt(["devices", "Paired"]).splitlines():
        m = _MAC_RE.search(line)
        if m:
            macs.add(m.group(1))

    nodes = list_audio_nodes()
    persisted = _load_devices()
    out = []
    for mac in sorted(macs):
        info = bluetoothctl_info(mac)
        node_suffix = mac.replace(":", "_").lower()
        has_output_node = any(f"bluez_output.{mac.replace(':', '_').upper()}" in n["name"]
                               or node_suffix in n["name"].lower() for n in nodes if n["kind"] == "sink")
        has_input_node = any(f"bluez_input.{mac.replace(':', '_').upper()}" in n["name"]
                              or node_suffix in n["name"].lower() for n in nodes if n["kind"] == "source")
        real_a2dp = info["connected"] and (has_output_node or info["claims_a2dp"])
        audio_ready = bool(info["connected"] and (has_output_node or has_input_node))
        real_hfp = info["connected"] and has_input_node
        meta = persisted.get(mac, {})
        out.append({
            "mac": mac,
            "name": meta.get("friendly_name") or info["name"],
            "paired": info["paired"],
            "connected": info["connected"],
            "audio_ready": audio_ready,             # False = linked, but BlueZ/PipeWire haven't brought up the audio profile
            "profiles": {
                "a2dp": real_a2dp if info["connected"] else info["claims_a2dp"],
                "hfp": real_hfp if info["connected"] else info["claims_hfp"],
                # True only once actually verified against a live node —
                # this is what the UI badges as a confirmed vs. claimed profile.
                "verified": info["connected"],
            },
        })
        if mac not in persisted or persisted.get(mac, {}).get("friendly_name") != info["name"]:
            persisted.setdefault(mac, {})["friendly_name"] = info["name"]
    if persisted:
        _save_devices(persisted)
    return out


def _pipewire_has_bluez_module() -> bool:
    """Whether PipeWire has its Bluetooth audio support actually loaded —
    a different thing from the pw-record/pw-play/pw-dump *binaries*
    existing (what check_host_capabilities' original pipewire check
    covers). Those binaries are core PipeWire; Bluetooth audio is a
    separate SPA plugin (commonly packaged as libspa-0.2-bluetooth on
    Debian/Ubuntu/Raspberry Pi OS), loaded into PipeWire as a
    'libpipewire-module-bluez5' module by WirePlumber's Bluetooth policy.
    Without it, BlueZ has no local audio endpoint to hand a connection off
    to — this is the actual, specific root cause of bluetoothctl's
    'Connection profile not found' when connecting an audio device, not a
    generic Bluetooth problem, so it's worth detecting explicitly rather
    than leaving people to decode that error message themselves."""
    try:
        r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout)
    except Exception:
        return False
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Module":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if "bluez5" in str(props.get("module.name", "")).lower():
            return True
    return False


def trust(mac: str) -> tuple[bool, str]:
    """Let BlueZ reconnect this device by itself later (a speaker that is switched off and on again
    would otherwise stay disconnected until someone connects it by hand)."""
    out = _run_bt(["trust", mac], timeout=8)
    return ("trust succeeded" in out.lower() or "already" in out.lower()), out.strip().splitlines()[-1] if out.strip() else ""


def capabilities() -> dict:
    """What is and isn't ready for Bluetooth on this machine, as plain facts and fixes."""
    res = {"platform": "linux", "available": False, "adapter_present": False, "adapter_powered": False,
           "audio_support": True, "errors": [], "hints": []}
    if not shutil.which("bluetoothctl"):
        res["errors"].append("BlueZ isn't installed, so Bluetooth devices can't be managed here.")
        res["hints"].append("sudo apt install bluez")
        return res
    try:
        out = subprocess.run(["bluetoothctl", "list"], capture_output=True, text=True, timeout=5).stdout
        res["adapter_present"] = bool(out.strip())
        if not res["adapter_present"]:
            res["errors"].append("No Bluetooth adapter was found on this machine.")
        else:
            show = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=5).stdout
            res["adapter_powered"] = "Powered: yes" in show
            if not res["adapter_powered"]:
                res["errors"].append("The Bluetooth adapter is switched off (Void OS turns it on before scanning).")
                res["hints"].append("bluetoothctl power on   (and check: rfkill list bluetooth)")
    except Exception:
        res["errors"].append("The Bluetooth service doesn't seem to be running.")
        res["hints"].append("sudo systemctl enable --now bluetooth")
    if shutil.which("pw-dump") and res["adapter_present"] and not _pipewire_has_bluez_module():
        res["audio_support"] = False
        res["errors"].append("PipeWire is running without Bluetooth audio support, so a connected speaker or "
                             "headset won't appear as an audio device.")
        res["hints"].append("sudo apt install libspa-0.2-bluetooth && systemctl --user restart wireplumber pipewire pipewire-pulse")
    res["available"] = res["adapter_present"]
    res["accepting"] = acceptor_status()
    return res


if sys.platform == "win32":
    from modules import winplat

    _BT_CLASSIC_PROTOCOL = "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"
    _win_ids: dict = {}          # MAC -> AEP device id, refreshed by every listing
    _win_list_cache = {"t": 0.0, "data": None}
    _win_lock = threading.Lock()

    _PS_WINRT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$__asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type, [int]$ms = 30000) {
  $task = $__asTask.MakeGenericMethod($type).Invoke($null, @($op))
  if (-not $task.Wait($ms)) { throw 'Windows Bluetooth call timed out' }
  $task.Result
}
$null = [Windows.Devices.Bluetooth.BluetoothAdapter, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$null = [Windows.Devices.Bluetooth.BluetoothDevice, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
$null = [Windows.Devices.Radios.Radio, Windows.Devices.Radios, ContentType=WindowsRuntime]
$AEP = [Windows.Devices.Enumeration.DeviceInformationKind]::AssociationEndpoint
$PROPS = [System.Collections.Generic.List[string]]::new()
foreach ($p in @('System.Devices.Aep.DeviceAddress','System.Devices.Aep.IsConnected','System.Devices.Aep.IsPaired',
                 'System.Devices.Aep.Bluetooth.Cod.Major','System.Devices.Aep.Bluetooth.Cod.Minor')) { $PROPS.Add($p) }
function Prop($di, [string]$k) {
  # Properties is a WinRT IMapView: indexing a missing key throws, so guard it
  if ($di.Properties.ContainsKey($k)) { return $di.Properties[$k] }
  return $null
}
function Get-Radio {
  $a = Await ([Windows.Devices.Bluetooth.BluetoothAdapter]::GetDefaultAsync()) ([Windows.Devices.Bluetooth.BluetoothAdapter])
  if ($null -eq $a) { return $null }
  Await ($a.GetRadioAsync()) ([Windows.Devices.Radios.Radio])
}
"""

    def _ps_json(body: str, timeout: float = 40):
        """Runs WinRT prelude + body; body must print exactly one JSON value.
        Returns (data, error_message)."""
        script = _PS_WINRT + "try {\n" + body + "\n} catch { ConvertTo-Json -Compress @{ error = $_.Exception.Message } }"
        ok, out, err = winplat.run_powershell(script, timeout=timeout)
        line = next((l for l in reversed(out.splitlines()) if l.strip().startswith(("{", "["))), "")
        if not line:
            return None, (err or out or "PowerShell returned nothing")[:400]
        try:
            data = json.loads(line)
        except ValueError:
            return None, line[:400]
        if isinstance(data, dict) and data.get("error"):
            return None, str(data["error"])[:400]
        return data, ""

    def _ensure_powered() -> tuple[bool, str]:  # noqa: F811
        data, err = _ps_json(r"""
$r = Get-Radio
if ($null -eq $r) { ConvertTo-Json -Compress @{ ok = $false; msg = 'no adapter' }; return }
if ($r.State -eq [Windows.Devices.Radios.RadioState]::On) { ConvertTo-Json -Compress @{ ok = $true }; return }
$acc = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
$st = Await ($r.SetStateAsync([Windows.Devices.Radios.RadioState]::On)) ([Windows.Devices.Radios.RadioAccessStatus])
ConvertTo-Json -Compress @{ ok = ($st -eq [Windows.Devices.Radios.RadioAccessStatus]::Allowed); msg = [string]$st }
""", timeout=20)
        if data and data.get("ok"):
            time.sleep(1)
            return True, ""
        return False, ("Couldn't turn Bluetooth on automatically" + (f" ({data.get('msg')})" if data else f" ({err})")
                       + " — turn it on in Settings → Bluetooth & devices, and check Airplane mode is off.")

    # ── Device listing / scan ──
    _LIST_PS = r"""
$sel = [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState(__PAIRED__)
$infos = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($sel, $PROPS)) ([Windows.Devices.Enumeration.DeviceInformationCollection]) __WAIT_MS__
$out = @()
foreach ($di in $infos) {
  $m = @{}
  foreach ($kv in $di.Properties) { $m[[string]$kv.Key] = $kv.Value }
  $addr = [string]$m['System.Devices.Aep.DeviceAddress']
  if (-not $addr) { $addr = ([string]$di.Id) -replace '^.*-','' }
  $out += [pscustomobject]@{
    mac = $addr.ToUpper(); id = $di.Id; name = [string]$di.Name
    paired = [bool]$m['System.Devices.Aep.IsPaired']
    connected = [bool]$m['System.Devices.Aep.IsConnected']
    major = $m['System.Devices.Aep.Bluetooth.Cod.Major']
    minor = $m['System.Devices.Aep.Bluetooth.Cod.Minor'] }
}
ConvertTo-Json -Compress -Depth 3 -InputObject @($out)
"""

    def _win_query(paired_only: bool, timeout: float):
        body = (_LIST_PS.replace("__PAIRED__", "$true" if paired_only else "$false")
                .replace("__WAIT_MS__", str(int((timeout - 5) * 1000))))
        data, err = _ps_json(body, timeout=timeout)
        if data is None:
            return None, err
        rows = data if isinstance(data, list) else [data]
        with _win_lock:
            for r in rows:
                if r.get("mac") and r.get("id"):
                    _win_ids[r["mac"]] = r["id"]
        return rows, ""

    def _annotate(rows: list) -> list:
        """Same shape list_devices() returns on Linux, with profiles verified
        against real Windows audio endpoints named after the device."""
        nodes = list_audio_nodes()
        persisted = _load_devices()
        out = []
        for r in sorted(rows, key=lambda x: (not x.get("paired"), (x.get("name") or "").lower())):
            mac = r["mac"]
            name = r.get("name") or mac
            key = name.casefold()
            has_out = any(key and key in n["name"].casefold() for n in nodes if n["kind"] == "sink")
            has_in = any(key and key in n["name"].casefold() for n in nodes if n["kind"] == "source")
            try:
                major, minor = int(r.get("major") or 0), int(r.get("minor") or 0)
            except (TypeError, ValueError):
                major, minor = 0, 0
            claims_a2dp = major == 4                      # Class of Device: Audio/Video
            claims_hfp = major == 4 and minor in (1, 2)   # wearable headset / hands-free
            connected = bool(r.get("connected"))
            meta = persisted.get(mac, {})
            out.append({
                "mac": mac, "name": meta.get("friendly_name") or name,
                "paired": bool(r.get("paired")), "connected": connected,
                "profiles": {
                    "a2dp": (connected and (has_out or claims_a2dp)) if connected else claims_a2dp,
                    "hfp": has_in if connected else claims_hfp,
                    "verified": connected,
                },
            })
            if meta.get("friendly_name") != name:
                persisted.setdefault(mac, {})["friendly_name"] = name
        if persisted:
            _save_devices(persisted)
        return out

    def list_devices() -> list:  # noqa: F811
        """Paired devices (Windows remembers these; cheap cache-only query).
        Cached for a few seconds because the Intercom tab asks for this on
        several calls in a row and each one is a PowerShell launch."""
        with _win_lock:
            if _win_list_cache["data"] is not None and time.time() - _win_list_cache["t"] < 4:
                return _win_list_cache["data"]
        rows, _ = _win_query(paired_only=True, timeout=25)
        devices = _annotate(rows or [])
        with _win_lock:
            _win_list_cache.update(t=time.time(), data=devices)
        return devices

    def _invalidate():
        with _win_lock:
            _win_list_cache.update(t=0.0, data=None)

    # FindAllAsync with the "all Bluetooth AEPs" query never returns for devices that aren't paired yet;
    # the pairing-state selector is the one that makes Windows run a real inquiry (about 30 s).
    _SCAN_PS = r"""
$sel = [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState($false)
$infos = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($sel, $PROPS)) ([Windows.Devices.Enumeration.DeviceInformationCollection]) 40000
$out = @()
foreach ($di in $infos) {
  $addr = ''
  foreach ($kv in $di.Properties) { if ($kv.Key -eq 'System.Devices.Aep.DeviceAddress') { $addr = [string]$kv.Value } }
  if (-not $addr) { $addr = ([string]$di.Id) -replace '^.*-','' }
  $out += [pscustomobject]@{ mac = $addr.ToUpper(); id = $di.Id; name = [string]$di.Name; paired = $false; connected = $false }
}
ConvertTo-Json -Compress -Depth 3 -InputObject @($out)
"""

    def scan(duration: int = 8) -> dict:  # noqa: F811
        powered, warning = _ensure_powered()
        if not powered:
            return {"devices": list_devices(), "warning": warning}
        found, err = _ps_json(_SCAN_PS, timeout=55)
        paired, _ = _win_query(paired_only=True, timeout=25)
        _invalidate()
        if found is None and paired is None:
            return {"devices": list_devices(), "warning": f"Scan failed: {err}"}
        rows = {r["mac"]: r for r in (paired or []) if r.get("mac")}
        for r in (found if isinstance(found, list) else ([found] if found else [])):
            mac = r.get("mac") or ""
            if mac and r.get("id"):
                with _win_lock:
                    _win_ids[mac] = r["id"]
                rows.setdefault(mac, r)
        devices = _annotate(list(rows.values()))
        warning = ""
        if found is None:
            warning = f"Showing paired devices only; looking for new ones failed: {err}"
        elif not devices:
            warning = ("Scan finished but Windows found no Bluetooth devices — make sure the device is in "
                       "pairing mode and close to this PC, then scan again.")
        return {"devices": devices, "warning": warning}

    def _id_for(mac: str, allow_scan: bool) -> str | None:
        with _win_lock:
            if mac in _win_ids:
                return _win_ids[mac]
        _win_query(paired_only=True, timeout=25)
        with _win_lock:
            if mac in _win_ids or not allow_scan:
                return _win_ids.get(mac)
        _win_query(paired_only=False, timeout=35)
        with _win_lock:
            return _win_ids.get(mac)

    def pair(mac: str) -> tuple[bool, str]:  # noqa: F811
        dev_id = _id_for(mac, allow_scan=True)
        if not dev_id:
            return False, "Device not found — put it in pairing mode and Scan again"
        data, err = _ps_json(f"""
$di = Await ([Windows.Devices.Enumeration.DeviceInformation]::CreateFromIdAsync({winplat.ps_quote(dev_id)}, $PROPS, $AEP)) ([Windows.Devices.Enumeration.DeviceInformation])
if ($di.Pairing.IsPaired) {{ ConvertTo-Json -Compress @{{ status = 'AlreadyPaired' }}; return }}
if (-not $di.Pairing.CanPair) {{ ConvertTo-Json -Compress @{{ status = 'NotReadyToPair' }}; return }}
$r = Await ($di.Pairing.PairAsync()) ([Windows.Devices.Enumeration.DevicePairingResult]) 45000
ConvertTo-Json -Compress @{{ status = [string]$r.Status }}
""", timeout=60)
        _invalidate()
        if data is None:
            return False, f"Pairing failed: {err}"
        st = data.get("status", "")
        if st in ("Paired", "AlreadyPaired"):
            return True, "paired"
        hints = {
            "NotReadyToPair": "the device isn't in pairing mode (or is out of range)",
            "AuthenticationTimeout": "the device wanted a PIN/confirmation that nobody answered — pair it once "
                                     "in Windows Settings on the host itself",
            "AuthenticationFailure": "authentication failed — put the device back in pairing mode and retry",
            "ConnectionRejected": "the device rejected the connection",
            "RejectedByHandler": "Windows needs someone to confirm on the host's screen",
            "AccessDenied": "access denied — Windows blocked pairing from this process",
        }
        return False, f"Pairing failed ({st}): {hints.get(st, 'see Windows Settings → Bluetooth for details')}"

    _CONNECT_PS = r"""
$addr = [UInt64]::Parse('__HEX__', [System.Globalization.NumberStyles]::HexNumber)
$bt = Await ([Windows.Devices.Bluetooth.BluetoothDevice]::FromBluetoothAddressAsync($addr)) ([Windows.Devices.Bluetooth.BluetoothDevice])
if ($null -eq $bt) { ConvertTo-Json -Compress @{ connected = $false; found = $false }; return }
$null = [Windows.Devices.Bluetooth.Rfcomm.RfcommDeviceServicesResult, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
try { $null = Await ($bt.GetRfcommServicesAsync([Windows.Devices.Bluetooth.BluetoothCacheMode]::Uncached)) ([Windows.Devices.Bluetooth.Rfcomm.RfcommDeviceServicesResult]) 15000 } catch { }
Start-Sleep -Seconds 3
ConvertTo-Json -Compress @{ connected = ($bt.ConnectionStatus -eq [Windows.Devices.Bluetooth.BluetoothConnectionStatus]::Connected); found = $true }
"""

    def connect(mac: str) -> tuple[bool, str]:  # noqa: F811
        data, err = _ps_json(_CONNECT_PS.replace("__HEX__", mac.replace(":", "")), timeout=40)
        _invalidate()
        if data is None:
            return False, f"Connect failed: {err}"
        if not data.get("found"):
            return False, "Windows doesn't know this device — pair it first"
        if data.get("connected"):
            return True, "connected"
        return False, ("Windows didn't bring the device's audio up. Turn the device off and on again (Windows "
                       "reconnects paired audio devices by itself), or use Settings → Bluetooth & devices → Connect.")

    def disconnect(mac: str) -> tuple[bool, str]:  # noqa: F811
        return False, ("Windows doesn't let apps disconnect a Bluetooth audio device — power the device off, "
                       "or use Settings → Bluetooth & devices → Disconnect. 'Forget' here unpairs it completely.")

    def remove(mac: str) -> tuple[bool, str]:  # noqa: F811
        dev_id = _id_for(mac, allow_scan=False)
        if not dev_id:
            with _devices_lock:
                d = _load_devices(); d.pop(mac, None); _save_devices(d)
            return True, "removed"
        data, err = _ps_json(f"""
$di = Await ([Windows.Devices.Enumeration.DeviceInformation]::CreateFromIdAsync({winplat.ps_quote(dev_id)}, $PROPS, $AEP)) ([Windows.Devices.Enumeration.DeviceInformation])
if (-not $di.Pairing.IsPaired) {{ ConvertTo-Json -Compress @{{ status = 'AlreadyUnpaired' }}; return }}
$r = Await ($di.Pairing.UnpairAsync()) ([Windows.Devices.Enumeration.DeviceUnpairingResult])
ConvertTo-Json -Compress @{{ status = [string]$r.Status }}
""", timeout=40)
        _invalidate()
        ok = bool(data) and data.get("status") in ("Unpaired", "AlreadyUnpaired")
        if ok:
            with _devices_lock:
                d = _load_devices(); d.pop(mac, None); _save_devices(d)
            with _win_lock:
                _win_ids.pop(mac, None)
            return True, "removed"
        return False, f"Forget failed: {err or (data or {}).get('status', 'unknown')}"

    def trust(mac: str) -> tuple[bool, str]:  # noqa: F811
        return True, "Windows reconnects paired audio devices by itself"

    def capabilities() -> dict:  # noqa: F811
        res = {"platform": "windows", "available": False, "adapter_present": False, "adapter_powered": False,
               "audio_support": True, "errors": [], "hints": []}
        if not winplat.powershell_exe():
            res["errors"].append("Windows PowerShell wasn't found; it is needed to talk to Windows' Bluetooth.")
            return res
        data, err = _ps_json(r"""
$r = Get-Radio
if ($null -eq $r) { ConvertTo-Json -Compress @{ adapter = $false; powered = $false } }
else { ConvertTo-Json -Compress @{ adapter = $true; powered = ($r.State -eq [Windows.Devices.Radios.RadioState]::On) } }
""", timeout=20)
        if data is None:
            res["errors"].append(f"Couldn't query Windows Bluetooth: {err}")
            return res
        res["adapter_present"] = bool(data.get("adapter"))
        res["adapter_powered"] = bool(data.get("powered"))
        if not res["adapter_present"]:
            res["errors"].append("Windows reports no Bluetooth radio (check Device Manager > Bluetooth).")
        elif not res["adapter_powered"]:
            res["errors"].append("Bluetooth is turned off (Void OS tries to turn it on before scanning). "
                                 "Check Settings > Bluetooth & devices, and that Airplane mode is off.")
        res["available"] = res["adapter_present"]
        return res


# ── Amazon Echo (and other "speaker only, one connection at a time" devices) ──────────────
# An Echo is an A2DP speaker and nothing else: it has no microphone the computer can use. It only shows
# up in a scan while it is in pairing mode ("Alexa, pair"), accepts a single Bluetooth connection at a
# time (so it refuses us while it is connected to a phone), and does not reconnect by itself, so it
# has to be asked each time. All of that is handled here, in words a person can act on.
_AMAZON_RE = re.compile(r"\b(echo|alexa|fire ?tv|fire ?stick|amazon)\b", re.I)

ECHO_PAIR_HELP = ("Echo speakers only appear while they are in pairing mode: say “Alexa, pair” (or open the "
                  "Alexa app → Devices → your Echo → Bluetooth devices → Pair a new device), then scan again.")
ECHO_BUSY_HELP = ("An Echo talks to one device at a time. If it is connected to your phone, say “Alexa, "
                  "disconnect” or turn Bluetooth off on the phone, then press Connect again.")


def is_amazon_speaker(name: str) -> bool:
    return bool(_AMAZON_RE.search(name or ""))


def _tag(devices: list) -> list:
    for d in devices or []:
        if is_amazon_speaker(d.get("name", "")):
            d["speaker_only"] = True
            d["hint"] = ("Echo: plays your voice out loud, but it has no microphone for the computer to listen "
                         "to. " + (ECHO_BUSY_HELP if d.get("paired") and not d.get("connected") else ""))
    return devices


_raw_list_devices = list_devices
_raw_scan = scan


def list_devices() -> list:  # noqa: F811
    return _tag(_raw_list_devices())


def scan(duration: int = 20) -> dict:  # noqa: F811
    res = _raw_scan(duration)
    res["devices"] = _tag(res.get("devices") or [])
    if not any(d.get("speaker_only") for d in res["devices"]) and not res.get("warning"):
        res["tip"] = "Looking for an Echo? " + ECHO_PAIR_HELP
    return res


def _echo_words(name: str, message: str, step: str) -> str:
    """Add what is specific to an Echo to a generic failure."""
    if not is_amazon_speaker(name):
        return message
    if step == "Pair":
        return f"{message} {ECHO_PAIR_HELP}"
    return f"{message} {ECHO_BUSY_HELP}"


def add_device(mac: str) -> dict:  # noqa: F811
    """One step for the person: pair (if needed), trust, connect, then say what the system now offers.
    Returns {ok, steps: [{name, ok, message}], message}. An Echo gets a few connection attempts, because it
    often refuses the first one while it is still finishing pairing."""
    steps = []
    release_hold(mac)

    def step(name, fn):
        ok, msg = fn()
        steps.append({"name": name, "ok": bool(ok), "message": msg})
        return ok
    devs = {d["mac"]: d for d in list_devices()}
    known = devs.get(mac) or {}
    name = known.get("name", "")
    if not (known and known.get("paired")):
        if not step("Pair", lambda: pair(mac)):
            msg = _echo_words(name, steps[-1]["message"], "Pair")
            return {"ok": False, "steps": steps, "message": msg}
        name = next((d.get("name", "") for d in list_devices() if d["mac"] == mac), name)
    step("Trust", lambda: trust(mac))
    attempts = 3 if is_amazon_speaker(name) else 1
    ok = False
    for i in range(attempts):
        ok = step("Connect", lambda: connect(mac))
        if ok:
            break
        if i + 1 < attempts:
            time.sleep(2.5)
    msg = "Connected." if ok else _echo_words(name, steps[-1]["message"], "Connect")
    return {"ok": ok, "steps": steps, "message": msg}


# ── reconnect on demand ─────────────────────────────────────────────────
_reconnect_at: dict = {}
_HEXMAC = re.compile(r"([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})"
                     r"[:_-]([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})")


def mac_for_device_id(dev_id: str) -> str | None:
    """Which paired Bluetooth device does an audio device id belong to? (None when it isn't Bluetooth.)"""
    if not dev_id or dev_id == "default":
        return None
    m = _HEXMAC.search(dev_id)
    if m:
        return ":".join(g.upper() for g in m.groups())
    low = dev_id.casefold()
    try:
        for d in _raw_list_devices():
            n = (d.get("name") or "").casefold()
            if d.get("paired") and n and len(n) > 2 and n in low:
                return d["mac"]
    except Exception:
        pass
    return None


def can_reconnect(dev_id: str) -> str | None:
    """-> the MAC when this is a paired Bluetooth speaker we may try to bring back right now."""
    mac = mac_for_device_id(dev_id)
    if mac and time.time() - _reconnect_at.get(mac, 0) > 20:
        return mac
    return None


def reconnect(mac: str) -> tuple[bool, str]:
    _reconnect_at[mac] = time.time()
    ok, msg = connect(mac)
    if not ok:
        name = next((d.get("name", "") for d in _raw_list_devices() if d["mac"] == mac), "")
        msg = _echo_words(name, msg, "Connect")
    return ok, msg
