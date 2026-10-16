"""
modules/intercom_audio.py — the intercom's audio layer: devices, streams and the wire codec.

One small interface for every operating system:

    backend = get_backend()                 # None when nothing usable is installed
    backend.devices()                       # [{id, name, kind: "input"|"output", default}]
    inp = backend.open_input(device_id)     # inp.read(timeout) -> one 20 ms frame of PCM16, or None
    out = backend.open_output(device_id)    # out.write(pcm) never blocks; out.close()

Everything runs at one fixed format, mono 24 kHz signed 16-bit, in 20 ms frames (480 samples,
960 bytes). The devices are opened at that rate (PortAudio / PipeWire resample for us), so no
resampling happens in Python.

Backends
  SoundDeviceBackend  PortAudio through the `sounddevice` package. Windows (WASAPI, with an MME
                      fallback) and any other OS that has PortAudio installed.
  PipeWireBackend     Linux: pw-record / pw-play and pw-dump, no Python package needed. Preferred on
                      Linux when the PipeWire tools exist, because Bluetooth devices appear there
                      under stable names that contain the device's address.

Wire codec: G.711-style mu-law, 8 bits per sample (~192 kbit/s at 24 kHz), implemented with lookup
tables so it needs no extra package (Python 3.13 removed `audioop`). The browser does the same
transform in its audio worklet.
"""
from __future__ import annotations

import array
import collections
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

RATE = 24000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000        # 480
FRAME_BYTES = FRAME_SAMPLES * 2                # 960 (PCM16 mono)
ULAW_FRAME_BYTES = FRAME_SAMPLES               # 480


# ══════════════════════════════════════════════════════════════════════
# mu-law
# ══════════════════════════════════════════════════════════════════════
def _lin2ulaw(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    if s < 0:
        s = -s
    if s > 32635:
        s = 32635
    s += 0x84
    exp = max(0, ((s >> 7) & 0xFF).bit_length() - 1)
    mant = (s >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant)) & 0xFF


def _ulaw2lin(u: int) -> int:
    u = ~u & 0xFF
    exp = (u >> 4) & 0x07
    mant = u & 0x0F
    s = (((mant << 3) + 0x84) << exp) - 0x84
    return -s if (u & 0x80) else s


# Index with a *signed* sample: negative indexes wrap to the upper half of the table, which is
# exactly where the negative values were put.
_ENC = bytes(_lin2ulaw(i if i < 32768 else i - 65536) for i in range(65536))
_DEC = array.array("h", (_ulaw2lin(u) for u in range(256)))


def ulaw_encode(pcm: bytes) -> bytes:
    """PCM16 little-endian -> mu-law bytes (half the size)."""
    if len(pcm) & 1:
        pcm = pcm[:-1]
    a = array.array("h")
    a.frombytes(pcm)
    if sys.byteorder == "big":
        a.byteswap()
    return bytes(map(_ENC.__getitem__, a))


def ulaw_decode(data: bytes) -> bytes:
    """mu-law bytes -> PCM16 little-endian."""
    a = array.array("h", map(_DEC.__getitem__, data))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def level(pcm: bytes) -> tuple:
    """(rms, peak) of a PCM16 buffer, each 0..1."""
    if len(pcm) < 2:
        return 0.0, 0.0
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) & ~1])
    if not a:
        return 0.0, 0.0
    peak = max(max(a), -min(a)) / 32768.0
    ss = sum(v * v for v in a)
    return math.sqrt(ss / len(a)) / 32768.0, peak


def tone(freq: float = 660.0, ms: int = 700, amp: float = 0.35) -> list:
    """A short sine beep, as a list of PCM16 frames (with a fade so it does not click)."""
    n = RATE * ms // 1000
    fade = RATE // 60
    out = array.array("h")
    for i in range(n):
        g = min(1.0, i / fade, (n - 1 - i) / fade)
        out.append(int(32767 * amp * g * math.sin(2 * math.pi * freq * i / RATE)))
    raw = out.tobytes()
    raw += b"\x00" * ((-len(raw)) % FRAME_BYTES)
    return [raw[i:i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]


class AudioError(Exception):
    pass


_PA_CODE = re.compile(r"PaErrorCode\s*(-?\d+)")
_PA_TEXT = {
    -10000: "the audio engine isn't running. Restart Void OS.",
    -9998: "the device doesn't offer a single (mono) channel in its current mode. Choose a different device "
           "or the system default.",
    -9997: "the device doesn't accept the audio format Void OS uses (24 kHz mono). Choose the system default "
           "instead, or a different device.",
    -9996: "the device isn't available any more. It was probably unplugged, switched off, disconnected or is out "
           "of Bluetooth range. Reconnect it and try again.",
    -9993: "that combination of devices can't be used together.",
    -9992: "the computer ran out of memory.",
    -9987: "the device didn't answer in time. Reconnect it and try again.",
    -9986: "the audio system reported an internal error. Restart Void OS if it keeps happening.",
    -9985: "the device is busy. Another program has taken it over, or Windows reports it as not ready. Close "
           "apps that use it (calls, music, recorders) and try again. If it's a Bluetooth headset, make sure it "
           "is connected.",
}


def explain_error(exc, what: str = "audio device") -> str:
    """A person-readable reason for an audio failure, with what to try. Never a raw traceback."""
    if isinstance(exc, AudioError):
        return str(exc)
    text = str(exc) or type(exc).__name__
    m = _PA_CODE.search(text)
    code = int(m.group(1)) if m else None
    win = sys.platform == "win32"
    if code == -9999:
        if win:
            tail = (" If this is a microphone, check Windows Settings > Privacy & security > Microphone, and "
                    "turn on 'Let desktop apps access your microphone'." if what == "microphone" else "")
            return (f"Couldn't open the {what}: Windows refused to open it. Another program may have exclusive "
                    "control (turn off 'Allow applications to take exclusive control' in Sound settings > the "
                    f"device > Properties > Advanced), or the device has just been disconnected.{tail}")
        return (f"Couldn't open the {what}: the system's audio server refused it. Make sure PipeWire/PulseAudio is "
                "running for the user that runs Void OS, and that this user may use audio (group: audio).")
    if code in _PA_TEXT:
        return f"Couldn't open the {what}: {_PA_TEXT[code]}"
    low = text.lower()
    if isinstance(exc, PermissionError) or "permission denied" in low:
        return (f"Couldn't use the {what}: this computer's permissions don't allow Void OS to use audio. "
                "On Linux, add the Void OS user to the 'audio' group and restart it.")
    return f"Couldn't use the {what}: {text[:200]}"


def _fix_runtime_dir():
    """Void OS usually runs as a system service, which has no login session, so PipeWire's tools
    can't find the user's audio server. Point them at it (the same user's /run/user/<uid>)."""
    if sys.platform != "linux":
        return
    try:
        rd = os.environ.get("XDG_RUNTIME_DIR", "")
        if not rd or not os.path.isdir(rd):
            cand = f"/run/user/{os.getuid()}"
            if os.path.isdir(cand):
                os.environ["XDG_RUNTIME_DIR"] = cand
    except Exception:
        pass


_fix_runtime_dir()

DEFAULT_ID = "default"      # "whatever the system is using right now": follows the OS's own choice
DEFAULT_NAMES = {"output": "System default speaker (whatever the computer is using)",
                 "input": "System default microphone (whatever the computer is using)"}


def with_default(devs: list) -> list:
    out = []
    for kind in ("output", "input"):
        if any(d["kind"] == kind for d in devs):
            out.append({"id": DEFAULT_ID, "name": DEFAULT_NAMES[kind], "kind": kind, "default": False, "api": "system"})
    return out + devs


# ══════════════════════════════════════════════════════════════════════
# SoundDevice (PortAudio)
# ══════════════════════════════════════════════════════════════════════
def _sd():
    import sounddevice  # imported lazily: a missing package is a reported capability, not a crash
    return sounddevice


# PortAudio takes a snapshot of the machine's audio devices when it starts, so a Bluetooth headset
# connected afterwards is invisible and "the default device" is whatever it was at start-up. It can
# only be restarted while no stream is open, so streams are counted here.
_pa_lock = threading.RLock()
_pa_open = 0


def _pa_opened():
    global _pa_open
    with _pa_lock:
        _pa_open += 1


def _pa_closed():
    global _pa_open
    with _pa_lock:
        _pa_open = max(0, _pa_open - 1)


def _pa_rescan(sd):
    """Re-read the device list from the OS when nothing is playing or recording."""
    with _pa_lock:
        if _pa_open:
            return False
        try:
            sd._terminate()
            sd._initialize()
            return True
        except Exception:
            try:
                sd._initialize()
            except Exception:
                pass
            return False


class _SdInput:
    def __init__(self, sd, candidates):
        self._q = collections.deque(maxlen=40)           # ~0.8 s; the oldest frame is dropped beyond that
        self._cv = threading.Condition()
        self._buf = bytearray()
        self._closed = False
        last = None
        for idx, extra in candidates:
            try:
                _pa_lock.acquire()
                self._s = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES,
                                            device=idx, extra_settings=extra, callback=self._cb)
                self._s.start()
                _pa_opened()
                return
            except Exception as e:  # try the next way of opening this device
                last = e
            finally:
                _pa_lock.release()
        raise AudioError(explain_error(last, "microphone"))

    def _cb(self, indata, frames, t, status):
        with self._cv:
            self._buf += bytes(indata)
            while len(self._buf) >= FRAME_BYTES:
                self._q.append(bytes(self._buf[:FRAME_BYTES]))
                del self._buf[:FRAME_BYTES]
            self._cv.notify()

    def read(self, timeout: float = 0.5):
        with self._cv:
            if not self._q and not self._closed:
                self._cv.wait(timeout)
            return self._q.popleft() if self._q else None

    @property
    def closed(self):
        if self._closed:
            return True
        try:
            return not self._s.active           # the device went away under us
        except Exception:
            return True

    def close(self):
        if not self._closed:
            _pa_closed()
        self._closed = True
        try:
            self._s.stop(); self._s.close()
        except Exception:
            pass
        with self._cv:
            self._cv.notify_all()


class _SdOutput:
    """The device callback pulls from a small buffer, so write() never blocks a network thread, an
    underrun plays silence instead of crackling, and a backlog is trimmed so delay cannot build."""
    PREBUFFER = FRAME_BYTES * 3          # start playing once ~60 ms is queued
    MAXBUF = FRAME_BYTES * 20            # never hold more than ~400 ms

    def __init__(self, sd, candidates):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._primed = False
        self._closed = False
        last = None
        for idx, extra in candidates:
            try:
                _pa_lock.acquire()
                self._s = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES,
                                             device=idx, extra_settings=extra, latency="low", callback=self._cb)
                self._s.start()
                _pa_opened()
                return
            except Exception as e:
                last = e
            finally:
                _pa_lock.release()
        raise AudioError(explain_error(last, "speaker"))

    def _cb(self, outdata, frames, t, status):
        need = frames * 2
        with self._lock:
            if not self._primed and len(self._buf) >= self.PREBUFFER:
                self._primed = True
            if self._primed and len(self._buf) >= need:
                outdata[:] = bytes(self._buf[:need])
                del self._buf[:need]
                return
            if self._primed:                      # ran dry: play what is left, then wait to refill
                n = len(self._buf)
                outdata[:n] = bytes(self._buf)
                outdata[n:] = b"\x00" * (need - n)
                self._buf.clear()
                self._primed = False
                return
        outdata[:] = b"\x00" * need

    def write(self, pcm: bytes):
        if self._closed or not pcm:
            return
        with self._lock:
            self._buf += pcm
            extra = len(self._buf) - self.MAXBUF
            if extra > 0:
                del self._buf[:extra - (extra % 2)]

    def drain(self, timeout: float = 1.5):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if not self._buf:
                    return
            time.sleep(0.02)

    @property
    def queued_ms(self) -> int:
        with self._lock:
            return len(self._buf) // 2 * 1000 // RATE

    @property
    def healthy(self) -> bool:
        try:
            return (not self._closed) and bool(self._s.active)
        except Exception:
            return False

    def close(self):
        if not self._closed:
            _pa_closed()
        self._closed = True
        try:
            self._s.stop(); self._s.close()
        except Exception:
            pass


class SoundDeviceBackend:
    name = "sounddevice"

    def __init__(self):
        self._sd = _sd()

    def _hostapi(self, prefix: str):
        for i, h in enumerate(self._sd.query_hostapis()):
            if h.get("name", "").lower().startswith(prefix.lower()):
                return i
        return None

    def capabilities(self) -> dict:
        devs = self.devices()
        errs, hints = [], []
        if not devs:
            if sys.platform == "win32":
                errs.append("No audio input or output devices were found. Plug in or enable a microphone or "
                            "speaker in Windows Sound settings.")
            else:
                errs.append("No audio input or output devices were found by PortAudio.")
        return {"ok": bool(devs), "backend": self.name, "errors": errs, "hints": hints}

    def devices(self) -> list:
        sd = self._sd
        with _pa_lock:
            _pa_rescan(sd)      # pick up headsets connected (or default changed) since the last look
            try:
                devs = sd.query_devices()
                apis = sd.query_hostapis()
            except Exception:
                return []
        want = None
        if sys.platform == "win32":
            want = self._hostapi("Windows WASAPI")
            if want is None:
                want = self._hostapi("MME")
        out, seen = [], set()
        din = dout = None
        if want is not None:
            din, dout = apis[want].get("default_input_device"), apis[want].get("default_output_device")
        else:
            try:
                din, dout = sd.default.device
            except Exception:
                pass
        for i, d in enumerate(devs):
            if want is not None and d.get("hostapi") != want:
                continue
            name = d.get("name", "")
            if not name:
                continue
            for kind, key, dflt in (("input", "max_input_channels", din), ("output", "max_output_channels", dout)):
                if d.get(key, 0) > 0 and (kind, name) not in seen:
                    seen.add((kind, name))
                    out.append({"id": name, "name": name, "kind": kind,
                                "default": dflt is not None and dflt == i,
                                "api": apis[d["hostapi"]]["name"] if d.get("hostapi") is not None and d["hostapi"] < len(apis) else ""})
        return with_default(out)

    def _candidates(self, name: str, kind: str):
        """Every way of opening a named device, best first. WASAPI shared mode only takes the
        endpoint's own rate unless auto-convert is on (sounddevice >= 0.4.6); the MME twin of the
        same device resamples itself but cuts names at 31 characters, hence prefix matching."""
        sd = self._sd
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        devs = sd.query_devices()
        wasapi, mme = self._hostapi("Windows WASAPI"), self._hostapi("MME")
        res = []
        if name == DEFAULT_ID and sys.platform != "win32":
            return [(None, None)]       # PortAudio's own default (ALSA/Pulse resample for us)
        if name == DEFAULT_ID:      # resolve to whatever the OS is using at this moment
            real = [d for d in self.devices() if d["kind"] == kind and d["id"] != DEFAULT_ID]
            pick = next((d for d in real if d["default"]), real[0] if real else None)
            if pick is None:
                raise AudioError("the system has no such device right now")
            name = pick["id"]
        for i, d in enumerate(devs):
            if d.get(key, 0) <= 0:
                continue
            if d.get("name") == name and (wasapi is None or d.get("hostapi") == wasapi):
                extra = None
                if wasapi is not None and d.get("hostapi") == wasapi:
                    try:
                        extra = sd.WasapiSettings(auto_convert=True)
                    except TypeError:
                        extra = None
                res.append((i, extra))
        for i, d in enumerate(devs):
            if d.get(key, 0) > 0 and mme is not None and d.get("hostapi") == mme:
                n = d.get("name", "")
                if n and (name.startswith(n) or n == name):
                    res.append((i, None))
        if not res:     # a non-Windows host API: take the first device with that name
            for i, d in enumerate(devs):
                if d.get(key, 0) > 0 and d.get("name") == name:
                    res.append((i, None))
        if not res:
            raise AudioError("Couldn't open the device: " + _PA_TEXT[-9996])
        return res

    def open_input(self, dev_id: str):
        with _pa_lock:
            return _SdInput(self._sd, self._candidates(dev_id, "input"))

    def open_output(self, dev_id: str):
        with _pa_lock:
            return _SdOutput(self._sd, self._candidates(dev_id, "output"))


# ══════════════════════════════════════════════════════════════════════
# PipeWire command-line tools (Linux)
# ══════════════════════════════════════════════════════════════════════
def _kill(proc):
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _pw_explain(line: str) -> str:
    low = line.lower()
    if ("connection refused" in low or "host is down" in low or "can't connect" in low
            or "cannot connect" in low or ("no such file or directory" in low and "pipewire" in low)):
        return ("PipeWire (the audio server) can't be reached. It must be running for the user that runs Void OS. "
                "Try: systemctl --user start pipewire wireplumber; for a server without a login session: "
                "sudo loginctl enable-linger <user>, then restart Void OS.")
    if "no node" in low or "target not found" in low or "no such entity" in low or "not found" in low:
        return ("that device isn't available any more (unplugged, switched off, or disconnected). "
                "Reconnect it and try again.")
    if "permission" in low:
        return "permission to use audio was denied. Add the Void OS user to the 'audio' group and restart it."
    return f"PipeWire reported: {line[:200]}"


def _pw_spawn(cmd: list, what: str, settle: float = 0.25, **kw):
    """Start a pw-record/pw-play process, and say why in plain words if it dies straight away.
    `settle` is how long to wait before judging it alive (a Bluetooth speaker needs longer to take the stream).
    What the tool prints on stderr is kept (p.err_tail) so a later failure can be explained."""
    try:
        p = subprocess.Popen(cmd, stderr=subprocess.PIPE, **kw)
    except FileNotFoundError:
        raise AudioError(f"Couldn't use the {what}: PipeWire's {cmd[0]} tool isn't installed "
                         "(sudo apt install pipewire-bin pipewire-audio).")
    except OSError as e:
        raise AudioError(f"Couldn't use the {what}: could not start {cmd[0]} ({e}).")
    end = time.time() + settle
    while time.time() < end and p.poll() is None:
        time.sleep(0.05)
    if p.poll() is not None:
        try:
            lines = (p.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
        except Exception:
            lines = []
        raise AudioError(f"Couldn't use the {what}: " + _pw_explain(
            lines[-1] if lines else f"{cmd[0]} stopped (exit code {p.returncode})"))

    p.err_tail = collections.deque(maxlen=6)

    def _drain():
        try:
            for raw in iter(p.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    p.err_tail.append(line)
        except Exception:
            pass
    threading.Thread(target=_drain, daemon=True, name="ic-pw-err").start()
    return p


class _PwInput:
    def __init__(self, node: str):
        self._q = collections.deque(maxlen=40)
        self._cv = threading.Condition()
        self._closed = False
        self._p = _pw_spawn(["pw-record"] + ([] if node == DEFAULT_ID else ["--target", node])
                            + ["--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1", "-"],
                            "microphone", stdout=subprocess.PIPE)
        threading.Thread(target=self._pump, daemon=True, name="ic-pw-in").start()

    def _pump(self):
        try:
            while not self._closed:
                data = self._p.stdout.read(FRAME_BYTES)
                if not data:
                    break
                if len(data) < FRAME_BYTES:
                    data += b"\x00" * (FRAME_BYTES - len(data))
                with self._cv:
                    self._q.append(data)
                    self._cv.notify()
        except Exception:
            pass
        finally:
            self._closed = True
            with self._cv:
                self._cv.notify_all()

    def read(self, timeout: float = 0.5):
        with self._cv:
            if not self._q and not self._closed:
                self._cv.wait(timeout)
            return self._q.popleft() if self._q else None

    @property
    def closed(self):
        return self._closed and not self._q

    def close(self):
        self._closed = True
        _kill(self._p)
        with self._cv:
            self._cv.notify_all()


class _PwOutput:
    def __init__(self, node: str):
        self._q = collections.deque()
        self._cv = threading.Condition()
        self._closed = False
        self._p = _pw_spawn(["pw-play"] + ([] if node == DEFAULT_ID else ["--target", node])
                            + ["--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1", "-"],
                            "speaker", stdin=subprocess.PIPE, settle=1.2 if "bluez" in node.lower() else 0.25)
        threading.Thread(target=self._pump, daemon=True, name="ic-pw-out").start()

    def _pump(self):
        try:
            while not self._closed:
                with self._cv:
                    if not self._q:
                        self._cv.wait(0.25)
                    chunk = self._q.popleft() if self._q else None
                if chunk:
                    self._p.stdin.write(chunk)
                    self._p.stdin.flush()
        except Exception:
            self._closed = True

    def write(self, pcm: bytes):
        if self._closed or not pcm:
            return
        with self._cv:
            self._q.append(pcm)
            while len(self._q) > 20:       # ~400 ms: drop the oldest so delay cannot build up
                self._q.popleft()
            self._cv.notify()

    def drain(self, timeout: float = 1.5):
        end = time.time() + timeout
        while time.time() < end and self._q:
            time.sleep(0.02)

    @property
    def queued_ms(self) -> int:
        return len(self._q) * FRAME_MS

    @property
    def healthy(self) -> bool:
        return (not self._closed) and self._p.poll() is None

    @property
    def why(self) -> str:
        """What the player last said before it stopped (empty if nothing)."""
        tail = list(getattr(self._p, "err_tail", []) or [])
        return (_pw_explain(tail[-1]) + f" (PipeWire said: {tail[-1][:140]})") if tail else ""

    def close(self):
        self._closed = True
        _kill(self._p)


class PipeWireBackend:
    name = "pipewire"

    @staticmethod
    def available() -> bool:
        return all(shutil.which(b) for b in ("pw-record", "pw-play", "pw-dump"))

    def _dump(self):
        try:
            r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=6)
            return json.loads(r.stdout)
        except Exception:
            return []

    def devices(self) -> list:
        out = []
        for obj in self._dump():
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = (obj.get("info") or {}).get("props") or {}
            mc = props.get("media.class", "")
            kind = "input" if mc == "Audio/Source" else ("output" if mc == "Audio/Sink" else None)
            if not kind:
                continue
            nm = props.get("node.name", str(obj.get("id")))
            out.append({"id": nm, "name": props.get("node.description") or props.get("node.nick") or nm,
                        "kind": kind, "default": False, "api": "pipewire"})
        return with_default(out)

    def capabilities(self) -> dict:
        devs = self.devices()
        errs = []
        if not devs:
            errs.append("PipeWire is installed but lists no audio devices. Is it running for this user? "
                        "Try: systemctl --user start pipewire wireplumber")
        return {"ok": bool(devs), "backend": self.name, "errors": errs, "hints": []}

    def _known(self, dev_id: str, kind: str):
        if dev_id != DEFAULT_ID and dev_id not in {d["id"] for d in self.devices() if d["kind"] == kind}:
            raise AudioError("Couldn't use the device: " + _PA_TEXT[-9996])

    def open_input(self, dev_id: str):
        self._known(dev_id, "input")
        return _PwInput(dev_id)

    def open_output(self, dev_id: str):
        self._known(dev_id, "output")
        return _PwOutput(dev_id)


# ── speaker volume (the operating system's own volume for an output device) ────────────────────────
# Linux only, through `wpctl` (WirePlumber, installed with PipeWire) or, failing that, `pactl`. The browser
# reads it every second or two, so the slider follows changes made anywhere else (the Alexa app, a keyboard
# key, another browser).
_vol_ids = {"t": 0.0, "map": {}}


def _vol_tool() -> str | None:
    if sys.platform == "win32":
        return None
    if shutil.which("wpctl"):
        return "wpctl"
    if shutil.which("pactl"):
        return "pactl"
    return None


def _node_id_for(name: str) -> str | None:
    """PipeWire's numeric id for a node name (wpctl wants the id); cached briefly."""
    if time.time() - _vol_ids["t"] > 8 or name not in _vol_ids["map"]:
        m = {}
        try:
            r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=6)
            for obj in json.loads(r.stdout):
                if obj.get("type") == "PipeWire:Interface:Node":
                    nm = ((obj.get("info") or {}).get("props") or {}).get("node.name")
                    if nm:
                        m[nm] = str(obj.get("id"))
        except Exception:
            pass
        _vol_ids.update(t=time.time(), map=m)
    return _vol_ids["map"].get(name)


def _run_vol(args: list, timeout: float = 4.0) -> str | None:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def get_volume(dev_id: str) -> dict | None:
    """{"volume": 0..1+, "muted": bool} for an output device, or None when this machine can't say."""
    tool = _vol_tool()
    if tool == "wpctl":
        target = "@DEFAULT_AUDIO_SINK@" if dev_id == DEFAULT_ID else _node_id_for(dev_id)
        out = _run_vol(["wpctl", "get-volume", target]) if target else None
        m = re.search(r"Volume:\s*([0-9.]+)", out or "")
        if not m:
            return None
        return {"volume": round(float(m.group(1)), 3), "muted": "[MUTED]" in out}
    if tool == "pactl":
        sink = "@DEFAULT_SINK@" if dev_id == DEFAULT_ID else dev_id
        vol = _run_vol(["pactl", "get-sink-volume", sink])
        mute = _run_vol(["pactl", "get-sink-mute", sink])
        m = re.search(r"(\d+)%", vol or "")
        if not m:
            return None
        return {"volume": int(m.group(1)) / 100.0, "muted": "yes" in (mute or "").lower()}
    return None


def set_volume(dev_id: str, volume: float | None = None, muted: bool | None = None) -> bool:
    tool = _vol_tool()
    ok = True
    if tool == "wpctl":
        target = "@DEFAULT_AUDIO_SINK@" if dev_id == DEFAULT_ID else _node_id_for(dev_id)
        if not target:
            return False
        if volume is not None:
            ok = _run_vol(["wpctl", "set-volume", target, f"{max(0.0, min(1.0, float(volume))):.3f}"]) is not None and ok
        if muted is not None:
            ok = _run_vol(["wpctl", "set-mute", target, "1" if muted else "0"]) is not None and ok
        return ok
    if tool == "pactl":
        sink = "@DEFAULT_SINK@" if dev_id == DEFAULT_ID else dev_id
        if volume is not None:
            ok = _run_vol(["pactl", "set-sink-volume", sink, f"{int(round(max(0.0, min(1.0, float(volume))) * 100))}%"]) is not None and ok
        if muted is not None:
            ok = _run_vol(["pactl", "set-sink-mute", sink, "1" if muted else "0"]) is not None and ok
        return ok
    return False


# ══════════════════════════════════════════════════════════════════════
# choosing one
# ══════════════════════════════════════════════════════════════════════
_backend = None
_backend_err = ""
_backend_lock = threading.Lock()
_backend_checked = 0.0


def get_backend(refresh: bool = False):
    """The audio backend for this machine, or None. The reason is in backend_error()."""
    global _backend, _backend_err, _backend_checked
    with _backend_lock:
        if _backend is not None and not refresh:
            return _backend
        if _backend is None and not refresh and time.time() - _backend_checked < 5:
            return None
        _backend_checked = time.time()
        _backend, _backend_err = None, ""
        if sys.platform != "win32" and PipeWireBackend.available():
            _backend = PipeWireBackend()
            return _backend
        try:
            _backend = SoundDeviceBackend()
            return _backend
        except Exception as e:
            if sys.platform == "win32":
                _backend_err = ("The Python package 'sounddevice' isn't installed. Run: pip install sounddevice, "
                                f"then restart Void OS. ({type(e).__name__})")
            else:
                _backend_err = ("No audio engine found. Install PipeWire (sudo apt install pipewire pipewire-audio "
                                "wireplumber) or PortAudio (sudo apt install libportaudio2 and pip install "
                                f"sounddevice). ({type(e).__name__})")
            return None


def backend_error() -> str:
    return _backend_err
