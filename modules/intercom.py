"""
modules/intercom.py — the intercom: rooms, live audio and who may talk.

What it is. People sign in to Void OS from a phone or a computer and talk to, or listen to, audio
devices attached to the server: a Bluetooth speaker in the kitchen, a USB headset in the workshop,
the machine's own speakers and microphone. Those devices are named once by an admin as **rooms**
("Kitchen" = this speaker + that microphone), and from then on anyone allowed to use the intercom
just picks a room.

Moving parts
  rooms      a small JSON file (intercom.json beside the Bluetooth names): name, icon, one output
             device (the speaker you talk *to*) and/or one input device (the microphone you listen *to*).
  hubs       one reader per input device, opened when the first person listens and closed with the
             last. Each 20 ms frame is encoded once and fanned out to every listener, so ten people
             listening cost the same as one.
  sinks      one writer per output device. Only one person talks into a speaker at a time (the
             "floor"): the second person is told who has it. An admin can take it over. Different
             rooms are independent, so several conversations run at once, and one person can talk
             into several rooms together (an announcement).
  connection one per open browser tab, over a single WebSocket (see modules/live_sockets.py).

Wire protocol (version 2). Text frames are JSON, binary frames are audio:
  client -> server   {"t":"listen","rooms":[id,...]}        which rooms to hear (replaces the set)
                     {"t":"talk","rooms":[id,...],"force":bool}   ask for the floor in those rooms
                     {"t":"talk_end"}                       give it back
                     {"t":"ping","ts":n}
                     0x01 + mu-law frame                    microphone audio for the rooms you hold
  server -> client   {"t":"hello", ...}  {"t":"state","rooms":[...]}  {"t":"floor", ...}
                     {"t":"notice","message":...}  {"t":"pong","ts":n}
                     0x02 + room number + mu-law frame      audio from a room's microphone
Audio is mono, 24 kHz, 20 ms frames, mu-law (see modules/intercom_audio.py). Nothing is ever recorded
or written to disk.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from modules import intercom_audio as A
from modules import intercom_bt as BT

PROTOCOL = 2
MAX_ROOMS = 40
MAX_TALK_SECONDS = 600           # a floor is released after this long of continuous talking
FLOOR_STALE_SECONDS = 4          # a holder that has sent nothing for this long loses the floor
SINK_IDLE_CLOSE = 10             # an output device is closed this long after the last talker leaves
AUDIO_UP, AUDIO_DOWN = 0x01, 0x02

_cfg_path: Path | None = None
_atomic_write = None
_audit_fn = None
_lock = threading.RLock()


def configure(devices_path: Path, atomic_write_fn, audit_fn):
    """Called once at start-up with the app's own helpers (atomic JSON writes, the Activity Log)."""
    global _cfg_path, _atomic_write, _audit_fn
    _cfg_path = Path(devices_path).with_name("intercom.json")
    _atomic_write = atomic_write_fn
    _audit_fn = audit_fn
    BT.configure(devices_path, atomic_write_fn)
    BT.set_audit(audit_fn)
    try:
        BT.start_acceptor()           # accept inbound Bluetooth pairing/connections (Linux; VOID_BT_ACCEPT=off to disable)
    except Exception:
        pass


def _audit(action: str, user: dict | None, target: str = "", detail: str = ""):
    if _audit_fn:
        try:
            _audit_fn(action, user, target=target, detail=detail)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Rooms
# ══════════════════════════════════════════════════════════════════════
def _load() -> dict:
    try:
        d = json.loads(_cfg_path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("rooms"), list):
            return d
    except Exception:
        pass
    return {"version": 2, "rooms": [], "next_n": 1}


def _save(cfg: dict):
    _atomic_write(_cfg_path, cfg)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32] or "room"


# Always there, nothing to set up: the computer's own default speaker and microphone.
BUILTIN_ID = "this-computer"
BUILTIN_N = 255


def _builtin() -> dict:
    return {"id": BUILTIN_ID, "n": BUILTIN_N, "name": "This computer", "icon": "💻",
            "input": A.DEFAULT_ID, "output": A.DEFAULT_ID, "builtin": True}


def rooms() -> list:
    with _lock:
        return [_builtin()] + [dict(r) for r in _load()["rooms"]]


def get_room(room_id: str) -> dict | None:
    return next((r for r in rooms() if r["id"] == room_id), None)


def save_room(data: dict, room_id: str | None = None) -> tuple:
    """Create (room_id None) or update a room. -> (room, error)."""
    name = str(data.get("name", "")).strip()[:40]
    if not name:
        return None, "Give the room a name."
    icon = str(data.get("icon", "") or "").strip()[:4] or "\U0001F50A"
    inp = str(data.get("input") or "").strip()[:200] or None
    out = str(data.get("output") or "").strip()[:200] or None
    if room_id == BUILTIN_ID:
        return None, "“This computer” is built in; it always follows the computer's default speaker and microphone."
    if not inp and not out:
        return None, "Pick a speaker, a microphone, or both for this room."
    with _lock:
        cfg = _load()
        rs = cfg["rooms"]
        if room_id is None:
            if len(rs) >= MAX_ROOMS:
                return None, f"At most {MAX_ROOMS} rooms."
            base, rid, k = _slug(name), None, 1
            ids = {r["id"] for r in rs}
            rid = base
            while rid in ids:
                k += 1
                rid = f"{base}-{k}"
            used = {r.get("n") for r in rs}
            n = next(i for i in range(1, 255) if i not in used)
            room = {"id": rid, "n": n, "name": name, "icon": icon, "input": inp, "output": out}
            rs.append(room)
        else:
            room = next((r for r in rs if r["id"] == room_id), None)
            if room is None:
                return None, "No such room."
            room.update({"name": name, "icon": icon, "input": inp, "output": out})
        _save(cfg)
    broadcast_state()
    return dict(room), None


def delete_room(room_id: str) -> bool:
    with _lock:
        cfg = _load()
        before = len(cfg["rooms"])
        cfg["rooms"] = [r for r in cfg["rooms"] if r["id"] != room_id]
        if len(cfg["rooms"]) == before:
            return False
        _save(cfg)
    for c in list(_conns.values()):
        c.drop_room(room_id)
    broadcast_state()
    return True


def _log_crash(e: Exception, context: str):
    """Write an unexpected failure to Void OS's error log (never raises)."""
    try:
        from modules.error_log import _log_exception
        _log_exception(e, "intercom", context)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# Devices and health
# ══════════════════════════════════════════════════════════════════════
_dev_cache = {"t": 0.0, "list": []}


def devices(fresh: bool = False) -> list:
    """The server's audio devices (any kind: Bluetooth, USB, built in). Cached for a few seconds,
    because enumerating them is not free and the page asks often."""
    with _lock:
        if not fresh and time.time() - _dev_cache["t"] < 4:
            return list(_dev_cache["list"])
    be = A.get_backend()
    lst = be.devices() if be else []
    with _lock:
        _dev_cache.update(t=time.time(), list=lst)
    return list(lst)


def _device_ok(dev_id: str | None, kind: str) -> bool:
    if not dev_id:
        return False
    return any(d["id"] == dev_id and d["kind"] == kind for d in devices())


def health() -> dict:
    """A checklist the page renders as-is: each step says what is wrong and how to fix it."""
    be = A.get_backend(refresh=True)
    steps = []
    if be is None:
        steps.append({"id": "audio", "label": "Audio engine", "ok": False,
                      "detail": A.backend_error() or "No audio engine is available.", "fix": []})
        audio_ok = False
    else:
        try:
            caps = be.capabilities()
        except Exception as e:
            caps = {"ok": False, "errors": [A.explain_error(e, "audio system")], "hints": []}
        steps.append({"id": "audio", "label": "Audio engine", "ok": caps["ok"],
                      "detail": (" ".join(caps["errors"]) if caps["errors"] else
                                 f"{be.name}: {len([d for d in be.devices() if d['kind'] == 'output' and d['id'] != 'default'])} speaker(s), "
                                 f"{len([d for d in be.devices() if d['kind'] == 'input' and d['id'] != 'default'])} microphone(s)"),
                      "fix": caps.get("hints", [])})
        audio_ok = caps["ok"]
    try:
        bt = BT.capabilities()
    except Exception as e:
        bt = {"available": False, "errors": [f"Bluetooth check failed ({type(e).__name__})"], "hints": [],
              "adapter_present": False, "adapter_powered": False, "audio_support": True, "platform": ""}
    steps.append({"id": "bluetooth", "label": "Bluetooth (optional)", "ok": bool(bt.get("available")),
                  "optional": True,
                  "detail": (" ".join(bt["errors"]) if bt.get("errors") else
                             ("Adapter ready" if bt.get("adapter_powered") else "Adapter found")),
                  "fix": bt.get("hints", [])})
    return {"ok": audio_ok, "backend": be.name if be else None, "platform": bt.get("platform", ""),
            "bluetooth": bool(bt.get("available")), "bluetooth_info": bt, "steps": steps}


def startup_summary() -> dict:
    """Cheap facts for the start-up banner (no PowerShell, no Bluetooth probing)."""
    be = A.get_backend()
    return {"ok": be is not None, "backend": be.name if be else None, "error": A.backend_error()}


# ══════════════════════════════════════════════════════════════════════
# Input hubs — one reader per microphone, fanned out to every listener
# ══════════════════════════════════════════════════════════════════════
class _Hub:
    def __init__(self, dev_id: str):
        self.dev_id = dev_id
        self.subs: dict = {}                 # Connection -> room number
        self.stream = None
        self.thread: threading.Thread | None = None
        self.error = ""
        self._stop = False
        self._lk = threading.Lock()

    def add(self, conn, room_n: int) -> str:
        with self._lk:
            self.subs[conn] = room_n
            if self.thread is None or not self.thread.is_alive():
                self._stop = False
                try:
                    self._open()
                except Exception as e:
                    self.subs.pop(conn, None)
                    self.error = A.explain_error(e, "microphone")
                    return self.error
                self.thread = threading.Thread(target=self._run, daemon=True, name=f"ic-in-{self.dev_id[:12]}")
                self.thread.start()
        return ""

    def _open(self):
        be = A.get_backend()
        if be is None:
            raise A.AudioError(A.backend_error() or "no audio engine")
        self.stream = be.open_input(self.dev_id)
        self.error = ""

    def remove(self, conn) -> bool:
        """-> True when this hub has no listeners left (the caller discards it)."""
        with self._lk:
            self.subs.pop(conn, None)
            if self.subs:
                return False
            self._stop = True
            st, self.stream = self.stream, None
        if st:
            try:
                st.close()
            except Exception:
                pass
        return True

    def _label(self) -> str:
        names = [r["name"] for r in rooms() if r.get("input") == self.dev_id]
        return " / ".join(names) or "a microphone"

    def _say(self, level: str, text: str):
        for c in list(self.subs):
            c.push_json({"t": "notice", "level": level, "message": text})

    def _run(self):
        try:
            self._loop()
        except Exception as e:
            _log_crash(e, f"microphone reader for {self.dev_id}")
            self._say("error", f"Listening to “{self._label()}” stopped because of an unexpected problem "
                               f"({type(e).__name__}). Turn Listen off and on again to retry; the details are "
                               "in the error log.")

    def _loop(self):
        lost_since = None
        reported = False
        last_fail_msg = ""
        while not self._stop:
            st = self.stream
            frame = st.read(0.5) if st else None
            if frame is None:
                if st is not None and getattr(st, "closed", False) or st is None:
                    lost_since = lost_since or time.time()
                    if time.time() - lost_since > 1.0:
                        if not reported:
                            reported = True
                            self._say("warn", f"The microphone for “{self._label()}” stopped: it was probably "
                                              "unplugged, switched off or is out of range. Trying to reconnect…")
                        # device vanished (unplugged, speaker switched off): try to get it back
                        time.sleep(2.0)
                        if self._stop:
                            return
                        with self._lk:
                            try:
                                if self.stream:
                                    self.stream.close()
                            except Exception:
                                pass
                            try:
                                self._open()
                                lost_since = None
                                if reported:
                                    reported = False
                                    last_fail_msg = ""
                                    self._say("info", f"The microphone for “{self._label()}” is back.")
                            except Exception as e:
                                self.error = A.explain_error(e, "microphone")
                                if self.error != last_fail_msg:      # say why, once, not every two seconds
                                    last_fail_msg = self.error
                                    self._say("warn", f"“{self._label()}”: {self.error}")
                continue
            lost_since = None
            enc = A.ulaw_encode(frame)
            for conn, n in list(self.subs.items()):
                conn.push_audio(n, enc)


_hubs: dict = {}


def _listen_add(conn, room: dict) -> str:
    dev = room.get("input")
    if not dev:
        return "This room has no microphone."
    with _lock:
        hub = _hubs.get(dev)
        if hub is None:
            hub = _hubs[dev] = _Hub(dev)
    err = hub.add(conn, room["n"])
    if err:
        with _lock:
            if not hub.subs and _hubs.get(dev) is hub:
                _hubs.pop(dev, None)
    return err


def _listen_remove(conn, dev: str):
    with _lock:
        hub = _hubs.get(dev)
    if hub and hub.remove(conn):
        with _lock:
            if _hubs.get(dev) is hub and not hub.subs:
                _hubs.pop(dev, None)


# ══════════════════════════════════════════════════════════════════════
# Output sinks — one writer per speaker, with a floor
# ══════════════════════════════════════════════════════════════════════
class _Sink:
    def __init__(self, dev_id: str):
        self.dev_id = dev_id
        self.stream = None
        self.owner = None                    # Connection holding the floor
        self.since = 0.0
        self.last_audio = 0.0
        self.lk = threading.Lock()
        self.close_timer: threading.Timer | None = None

    def holder_name(self) -> str | None:
        o = self.owner
        return o.user.get("username") if o else None

    def acquire(self, conn, force: bool) -> tuple:
        """-> (granted, held_by)"""
        with self.lk:
            o = self.owner
            now = time.time()
            if o is not None and o is not conn:
                stale = (now - self.last_audio > FLOOR_STALE_SECONDS) and (now - self.since > FLOOR_STALE_SECONDS)
                if not (force or stale or not o.alive):
                    return False, o.user.get("username")
                o.floor_lost(self.dev_id, by=conn.user.get("username"))
            if self.close_timer:
                self.close_timer.cancel()
                self.close_timer = None
            if self.stream is None:
                be = A.get_backend()
                if be is None:
                    raise A.AudioError(A.backend_error() or "no audio engine")
                self.stream = be.open_output(self.dev_id)
            self.owner = conn
            self.since = self.last_audio = now
            return True, None

    def write(self, conn, pcm: bytes):
        if self.owner is not conn or self.stream is None:
            return
        self.last_audio = time.time()
        err = None
        st = self.stream
        try:
            if getattr(st, "healthy", True) is False:
                err = "the speaker disconnected (switched off, out of range or unplugged)"
            else:
                st.write(pcm)
        except Exception as e:
            err = A.explain_error(e, "speaker")
        if err:
            self.stream = None                       # the next talk opens it afresh
            try:
                st.close()
            except Exception:
                pass
            conn.push_json({"t": "notice", "level": "error",
                            "message": f"Your voice can't be played: {err}. Check the device and talk again."})
            threading.Thread(target=conn.stop_talking, daemon=True).start()

    def release(self, conn):
        with self.lk:
            if self.owner is not conn:
                return
            self.owner = None
            st = self.stream
            if st is not None:
                try:
                    st.drain(0.8)
                except Exception:
                    pass
            if self.close_timer:
                self.close_timer.cancel()
            self.close_timer = threading.Timer(SINK_IDLE_CLOSE, self._close_if_idle)
            self.close_timer.daemon = True
            self.close_timer.start()

    def _close_if_idle(self):
        with self.lk:
            if self.owner is None and self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

    def play_frames(self, frames: list):
        """Used by the output test: plays frames at real time, holding the floor for the duration."""

        class _T:  # a throwaway "connection"
            alive = True
            user = {"username": "test"}
            def floor_lost(self, *a, **k): pass
        t = _T()
        ok, by = self.acquire(t, force=False)
        if not ok:
            raise A.AudioError(f"{by} is using this speaker right now")
        try:
            for f in frames:
                self.write(t, f)
                time.sleep(A.FRAME_MS / 1000.0)
        finally:
            self.release(t)


_sinks: dict = {}


def _sink(dev: str) -> _Sink:
    with _lock:
        s = _sinks.get(dev)
        if s is None:
            s = _sinks[dev] = _Sink(dev)
        return s


def play_test_tone(dev_id: str) -> str:
    """Beep through a speaker so a person can confirm it is the right one. -> error text or ''."""
    if not _device_ok(dev_id, "output"):
        return "That speaker isn't available right now."
    try:
        _sink(dev_id).play_frames(A.tone())
    except A.AudioError as e:
        return str(e)
    except Exception as e:
        if "PaErrorCode" not in str(e):      # a device refusing to open is expected, not a crash
            _log_crash(e, "speaker test")
        return A.explain_error(e, "speaker")
    return ""


def measure_input(dev_id: str, seconds: float = 0.8) -> dict:
    """Listen to a microphone briefly and report how loud it is. -> {ok, rms, peak, error}."""
    if not _device_ok(dev_id, "input"):
        return {"ok": False, "error": "That microphone isn't available right now."}
    be = A.get_backend()
    if be is None:
        return {"ok": False, "error": A.backend_error() or "No audio engine is available."}
    try:
        st = be.open_input(dev_id)
    except Exception as e:
        return {"ok": False, "error": A.explain_error(e, "microphone")}
    peak = rms_acc = 0.0
    n = 0
    end = time.time() + seconds
    try:
        while time.time() < end:
            f = st.read(0.3)
            if f:
                r, p = A.level(f)
                rms_acc += r
                n += 1
                peak = max(peak, p)
    finally:
        st.close()
    if n == 0:
        return {"ok": False, "error": "The microphone produced no audio."}
    return {"ok": True, "rms": round(rms_acc / n, 4), "peak": round(peak, 4)}


# ══════════════════════════════════════════════════════════════════════
# Connections
# ══════════════════════════════════════════════════════════════════════
_conns: dict = {}


class Connection:
    def __init__(self, user: dict, send, close):
        """`send(data)` writes one WebSocket frame (str or bytes); `close()` ends the socket."""
        self.id = uuid.uuid4().hex[:8]
        self.user = user
        self._send = send
        self._close = close
        self.alive = True
        self.listening: dict = {}            # room id -> input device id
        self.talking: dict = {}              # room id -> output device id
        self.talk_started = 0.0
        self.q: queue.Queue = queue.Queue(maxsize=240)
        self.tlock = threading.Lock()
        self._sender = threading.Thread(target=self._send_loop, daemon=True, name="ic-send")
        self._sender.start()
        threading.Thread(target=self._watchdog, daemon=True, name="ic-watch").start()

    # ---- outgoing
    def _send_loop(self):
        while self.alive:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._send(item)
            except Exception:
                self.shutdown()
                break

    def _put(self, item):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            try:
                self.q.get_nowait()          # keep the newest audio; the oldest is the least useful
                self.q.put_nowait(item)
            except Exception:
                pass

    def push_audio(self, room_n: int, ulaw: bytes):
        if self.alive:
            self._put(bytes((AUDIO_DOWN, room_n)) + ulaw)

    def push_json(self, obj: dict):
        if self.alive:
            self._put(json.dumps(obj, separators=(",", ":")))

    # ---- incoming
    def on_audio(self, frame: bytes):
        """One microphone frame from this browser, for every room whose floor it holds."""
        if not self.talking or len(frame) < 2 or frame[0] != AUDIO_UP:
            return
        body = frame[1:]
        if len(body) > A.ULAW_FRAME_BYTES * 4:
            return
        try:
            pcm = A.ulaw_decode(body)
            for dev in set(self.talking.values()):
                _sink(dev).write(self, pcm)
        except Exception as e:
            self._crash(e, "playing your voice")

    def _crash(self, e: Exception, doing: str):
        _log_crash(e, doing)
        if not getattr(self, "_crash_told", False):
            self._crash_told = True
            self.push_json({"t": "notice", "level": "error",
                            "message": f"Something went wrong while {doing} ({type(e).__name__}). "
                                       "Reload the page; the details are in the error log."})

    def on_json(self, msg: dict):
        try:
            self._on_json(msg)
        except Exception as e:
            self._crash(e, "handling that request")

    def _on_json(self, msg: dict):
        t = msg.get("t")
        if t == "ping":
            self.push_json({"t": "pong", "ts": msg.get("ts")})
        elif t == "listen":
            self.set_listening([str(x) for x in (msg.get("rooms") or [])][:MAX_ROOMS])
        elif t == "talk":
            self.start_talking([str(x) for x in (msg.get("rooms") or [])][:MAX_ROOMS], bool(msg.get("force")))
        elif t == "talk_end":
            self.stop_talking()

    # ---- listening
    def set_listening(self, room_ids: list):
        by_id = {r["id"]: r for r in rooms()}
        want = {rid: by_id[rid] for rid in room_ids if rid in by_id and by_id[rid].get("input")}
        errs = []
        for rid in list(self.listening):
            if rid not in want:
                _listen_remove(self, self.listening.pop(rid))
        for rid, room in want.items():
            if rid in self.listening:
                continue
            err = _listen_add(self, room)
            if err:
                errs.append(f"{room['name']}: {err}")
            else:
                self.listening[rid] = room["input"]
        if errs:
            self.push_json({"t": "notice", "level": "error", "message": " ".join(errs)})
        broadcast_state()

    # ---- talking
    def start_talking(self, room_ids: list, force: bool = False):
        user_role = self.user.get("role")
        force = force and user_role in ("superuser", "admin")
        by_id = {r["id"]: r for r in rooms()}
        granted, denied = [], []
        self.stop_talking(quiet=True)
        seen_dev = set()
        for rid in room_ids:
            room = by_id.get(rid)
            if not room or not room.get("output"):
                denied.append({"room": rid, "by": None, "reason": "This room has no speaker."})
                continue
            dev = room["output"]
            if dev in seen_dev:
                self.talking[rid] = dev
                granted.append(rid)
                continue
            try:
                ok, by = _sink(dev).acquire(self, force)
            except Exception as e:
                reason = A.explain_error(e, "speaker")
                mac = None
                try:
                    mac = BT.can_reconnect(dev)
                except Exception:
                    pass
                if mac:     # a Bluetooth speaker that has dropped off (an Echo does not reconnect by itself)
                    reason = ("That speaker is disconnected. Reconnecting to it now; try again in a few seconds.")
                    self._reconnect_async(mac, room.get("name", "the speaker"))
                denied.append({"room": rid, "by": None, "reason": reason})
                continue
            if ok:
                seen_dev.add(dev)
                self.talking[rid] = dev
                granted.append(rid)
            else:
                denied.append({"room": rid, "by": by, "reason": f"{by} is talking in this room."})
        if granted:
            self.talk_started = time.time()
            _audit("intercom_talk_start", self.user, target=",".join(granted))
        self.push_json({"t": "floor", "granted": granted, "denied": denied})
        broadcast_state()

    def _reconnect_async(self, mac: str, name: str):
        def go():
            try:
                ok, msg = BT.reconnect(mac)
            except Exception as e:
                ok, msg = False, A.explain_error(e, "Bluetooth speaker")
            devices(fresh=True)
            if ok:
                self.push_json({"t": "notice", "level": "info",
                                "message": f"“{name}” is connected again. You can talk now."})
            else:
                self.push_json({"t": "notice", "level": "error",
                                "message": f"Couldn't reconnect “{name}”: {msg}"})
            broadcast_state()
        threading.Thread(target=go, daemon=True, name="ic-reconnect").start()

    def stop_talking(self, quiet: bool = False):
        if not self.talking:
            return
        rids = list(self.talking)
        devs = set(self.talking.values())
        self.talking = {}
        for dev in devs:
            _sink(dev).release(self)
        dur = round(time.time() - self.talk_started, 1) if self.talk_started else 0
        _audit("intercom_talk_end", self.user, target=",".join(rids), detail=f"duration_s={dur}")
        if not quiet:
            self.push_json({"t": "floor", "granted": [], "denied": []})
            broadcast_state()

    def floor_lost(self, dev_id: str, by: str | None = None):
        """Someone with more authority took the speaker over."""
        lost = [r for r, d in self.talking.items() if d == dev_id]
        for r in lost:
            self.talking.pop(r, None)
        self.push_json({"t": "notice", "level": "warn",
                        "message": f"{by or 'An admin'} took over the speaker."})
        self.push_json({"t": "floor", "granted": list(self.talking), "denied": []})

    def drop_room(self, room_id: str):
        if room_id in self.listening:
            _listen_remove(self, self.listening.pop(room_id))
        self.talking.pop(room_id, None)

    # ---- housekeeping
    def _watchdog(self):
        while self.alive:
            time.sleep(1.0)
            if self.talking and time.time() - self.talk_started > MAX_TALK_SECONDS:
                self.push_json({"t": "notice", "level": "warn", "message": "Talking stopped: that was a long time."})
                self.stop_talking()

    def shutdown(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.stop_talking(quiet=True)
        except Exception:
            pass
        for rid in list(self.listening):
            try:
                _listen_remove(self, self.listening.pop(rid))
            except Exception:
                pass
        _conns.pop(self.id, None)
        try:
            self.q.put_nowait(None)
        except Exception:
            pass
        try:
            self._close()
        except Exception:
            pass
        broadcast_state()


def open_connection(user: dict, send, close) -> Connection:
    c = Connection(user, send, close)
    with _lock:
        _conns[c.id] = c
    _audit("intercom_connect", user, target=user.get("username", ""))
    c.push_json({"t": "hello", "protocol": PROTOCOL, "rate": A.RATE, "frame_ms": A.FRAME_MS, "codec": "ulaw",
                 "you": {"username": user.get("username"), "admin": user.get("role") in ("superuser", "admin")},
                 "rooms": room_states()})
    broadcast_state()
    return c


# ══════════════════════════════════════════════════════════════════════
# State shared with every open page
# ══════════════════════════════════════════════════════════════════════
def room_states() -> list:
    cs = list(_conns.values())
    out = []
    for r in rooms():
        sink = _sinks.get(r.get("output")) if r.get("output") else None
        out.append({
            "id": r["id"], "n": r["n"], "name": r["name"], "icon": r["icon"], "builtin": bool(r.get("builtin")),
            "has_input": bool(r.get("input")), "has_output": bool(r.get("output")),
            "input_ok": _device_ok(r.get("input"), "input") if r.get("input") else None,
            "output_ok": _device_ok(r.get("output"), "output") if r.get("output") else None,
            "talking_by": sink.holder_name() if sink else None,
            "listeners": sum(1 for c in cs if r["id"] in c.listening),
        })
    return out


_bc_lock = threading.Lock()
_bc_pending = [False]


def broadcast_state():
    """Tell every open page the new room list. Several changes in a row become one message."""
    with _bc_lock:
        if _bc_pending[0]:
            return
        _bc_pending[0] = True

    def go():
        time.sleep(0.05)
        with _bc_lock:
            _bc_pending[0] = False
        msg = {"t": "state", "rooms": room_states()}
        for c in list(_conns.values()):
            c.push_json(msg)
    threading.Thread(target=go, daemon=True).start()


def status() -> dict:
    return {"rooms": room_states(), "connections": len(_conns)}
