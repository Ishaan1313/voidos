"""
modules/live_sockets.py — WebSockets: in-browser terminal, live control channel, intercom audio.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import request
from modules.winplat import IS_WIN
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
try:
    import fcntl
except ImportError:  # optional on this platform
    pass
try:
    import pty
except ImportError:  # optional on this platform
    pass
try:
    import select as _sel
except ImportError:  # optional on this platform
    pass
try:
    import termios
except ImportError:  # optional on this platform
    pass
from modules.config import (
    MAX_CONTROL_SOCKETS_GLOBAL, MAX_CONTROL_SOCKETS_PER_USER, MAX_TERMINAL_SESSIONS_GLOBAL,
    MAX_TERMINAL_SESSIONS_PER_USER, PTY_OK, TERM_EN, WIN_PTY_OK, _WinPTY,
)
from modules.error_log import _can_use_intercom, _can_use_terminal, _validate_ws_cookie
from modules.web import _count_live_sessions, _register_live_session, _unregister_live_session, sock
from modules.users import _sid_from_cookies, _terminal_session_unlocked
from modules import intercom, winplat


# ══════════════════════════════════════════════════════════════════════
# TERMINAL — served on the SAME Flask port via a real flask-sock
# WebSocket route (/terminal), so a single Cloudflare Tunnel / reverse
# proxy pointed at PORT carries everything: dashboard, API, AND terminal.
# No second port to expose or firewall.
# ══════════════════════════════════════════════════════════════════════

# Open intercom pages per account, and in total (each one holds a worker thread for as long as it is open).
INTERCOM_SOCKETS_PER_USER = 4
INTERCOM_SOCKETS_GLOBAL = 16

def _shell_env() -> dict:
    """The environment a terminal session starts with: Void OS's own, minus its
    secrets (session-signing key, API keys, password hashes), which a shell has
    no business inheriting."""
    env = os.environ.copy()
    for k in list(env):
        ku = k.upper()
        if ku.startswith("VOID_") and any(w in ku for w in ("SECRET", "TOKEN", "PASSWORD", "KEY")):
            env.pop(k, None)
        elif ku in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN") or ku.startswith("RCLONE_CONFIG_"):
            env.pop(k, None)
    return env

def _shell_cmd():
    """VOID_SHELL overrides the shell on any OS (e.g. VOID_SHELL=wsl.exe or
    VOID_SHELL="C:\\Program Files\\Git\\bin\\bash.exe --login -i").
    Windows default: PowerShell 7 (pwsh) if installed, else Windows
    PowerShell -- but only under pywinpty. Without a real pseudo-console,
    PowerShell's line editor never draws a prompt or echoes input through
    plain pipes, so the pipe fallback uses cmd.exe (UTF-8 code page), which
    behaves sensibly line-at-a-time."""
    override = os.environ.get("VOID_SHELL", "").strip()
    if override:
        import shlex
        return [a.strip('"') for a in shlex.split(override, posix=not IS_WIN)]
    if IS_WIN:
        if WIN_PTY_OK:
            pw = shutil.which("pwsh.exe")
            return [pw, "-NoLogo"] if pw else ["powershell.exe","-NoLogo","-NoProfile"]
        return ["cmd.exe", "/Q", "/K", "chcp 65001>nul"]
    shell = os.environ.get("SHELL","/bin/bash")
    cmd = [shell, "-i"]
    # A terminal is a shell as the account Void OS runs as, and that account can read Void OS's own
    # configuration (the secret key that signs every login). VOID_TERMINAL_USER runs shells as a separate,
    # low-privilege account instead; it needs a sudoers rule letting Void OS's user run commands as it.
    term_user = os.environ.get("VOID_TERMINAL_USER", "").strip()
    if term_user and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", term_user):
        return ["sudo", "-n", "-u", term_user, "-H", "--"] + cmd
    return cmd

def _run_terminal_session(ws, user_id: str, sid: str | None = None):
    """Blocking read/write loop for one already-authenticated terminal
    WebSocket. Identical PTY/winpty/pipe logic as before this session's
    WSGI-server swap — only the transport changed: `ws` is now a
    flask-sock/simple-websocket connection object (ws.send/ws.receive,
    framing handled internally) instead of a raw hijacked socket with
    hand-rolled WebSocket framing.

    Registered in _live_sessions for the duration — this is what lets an
    admin revoking terminal access kill this exact running shell instantly
    (see _notify_permissions_changed) instead of it lingering until the
    browser reloads or reconnects."""
    alive=[True]; fd=None; proc=None; win_proc=None; line_buf=[""]
    def _kill():
        # Flip the same flag the read loop already checks, then force the
        # blocking ws.receive() below to unblock immediately by closing the
        # socket out from under it — the existing `finally` cleanup (kills
        # the shell process, closes the pty) then runs right away, same as
        # a normal disconnect.
        alive[0] = False
        try: ws.close()
        except Exception: pass
    _live_session_id = _register_live_session(user_id, "terminal", _kill, sid=sid)
    try:
        env=_shell_env()
        env["TERM"]="xterm-256color"; env["COLORTERM"]="truecolor"
        env["COLUMNS"]="80"; env["LINES"]="24"
        if PTY_OK:
            mfd,sfd=pty.openpty()
            try:
                attrs=termios.tcgetattr(sfd)
                attrs[6][termios.VERASE]=b'\x7f'
                attrs[1]|=termios.OPOST|termios.ONLCR
                termios.tcsetattr(sfd,termios.TCSANOW,attrs)
                fcntl.ioctl(sfd,termios.TIOCSWINSZ,struct.pack("HHHH",24,80,0,0))
            except: pass
            def _child():
                os.setsid()
                try: fcntl.ioctl(sfd,termios.TIOCSCTTY,0)
                except: pass
            try:
                proc=subprocess.Popen(_shell_cmd(),stdin=sfd,stdout=sfd,stderr=sfd,
                    env=env,preexec_fn=_child,close_fds=True)
            except Exception as e:
                os.close(mfd); os.close(sfd)
                try: ws.send(f"\x1b[31mCould not start shell: {e}\x1b[0m\r\n")
                except Exception: pass
                return
            os.close(sfd); fd=mfd
            def reader():
                try:
                    while alive[0]:
                        r,_,_=_sel.select([fd],[],[],0.05)
                        if r:
                            try:
                                chunk=os.read(fd,4096)
                                if not chunk: break
                                ws.send(chunk.decode("utf-8",errors="replace"))
                            except OSError: break
                except: pass
                alive[0]=False
                try: ws.send("\r\n\x1b[33m[session ended]\x1b[0m\r\n")
                except: pass
            threading.Thread(target=reader,daemon=True).start()
        elif WIN_PTY_OK:
            try:
                win_proc=_WinPTY.spawn(_shell_cmd(),dimensions=(24,80))
            except TypeError:
                win_proc=_WinPTY.spawn(" ".join(_shell_cmd()),dimensions=(24,80))
            def reader():
                while alive[0]:
                    try:
                        chunk=win_proc.read(4096)
                        if chunk: ws.send(chunk)
                    except EOFError: break
                    except:
                        if alive[0]: time.sleep(0.05)
                alive[0]=False
                try: ws.send("\r\n\x1b[33m[session ended]\x1b[0m\r\n")
                except: pass
            threading.Thread(target=reader,daemon=True).start()
        else:
            try: ws.send("\x1b[33mBasic line mode (no pseudo-terminal). For a full terminal: pip install pywinpty, then restart Void OS.\x1b[0m\r\n\r\n")
            except Exception: pass
            try:
                proc=subprocess.Popen(_shell_cmd(),stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0,env=env,
                    **winplat.no_window())
            except Exception as e:
                try: ws.send(f"\x1b[31m{e}\x1b[0m\r\n")
                except Exception: pass
                return
            def reader():
                try:
                    while alive[0]:
                        chunk=proc.stdout.read(4096)
                        if not chunk: break
                        ws.send(chunk.decode("utf-8",errors="replace"))
                except: pass
                alive[0]=False
                try: ws.send("\r\n\x1b[33m[session ended]\x1b[0m\r\n")
                except: pass
            threading.Thread(target=reader,daemon=True).start()
        while alive[0]:
            try:
                data=ws.receive()
            except Exception:
                break
            if data is None:
                break  # clean close
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8", errors="replace")
            try:
                msg=json.loads(data); t=msg.get("t")
                if t=="i":
                    raw=msg["d"].encode("utf-8")
                    if fd is not None: os.write(fd,raw)
                    elif win_proc: win_proc.write(msg["d"])
                    elif proc and proc.stdin:
                        # Pipe mode: no tty means no echo and no line editing, and
                        # xterm.js sends a bare "\r" for Enter. Do minimal line
                        # discipline here: echo locally, handle backspace, and only
                        # hand the shell a complete "\r\n"-terminated line.
                        out_echo=[]
                        for ch in msg["d"]:
                            if ch in ("\r","\n"):
                                out_echo.append("\r\n")
                                proc.stdin.write((line_buf[0]+"\r\n").encode("utf-8")); line_buf[0]=""
                            elif ch in ("\x7f","\b"):
                                if line_buf[0]: line_buf[0]=line_buf[0][:-1]; out_echo.append("\b \b")
                            elif ch=="\x03":
                                line_buf[0]=""; out_echo.append("^C\r\n")
                            elif ch=="\x1b" or not ch.isprintable():
                                continue  # arrow keys etc. -- no line editor to drive in pipe mode
                            else:
                                line_buf[0]+=ch; out_echo.append(ch)
                        proc.stdin.flush()
                        if out_echo:
                            try: ws.send("".join(out_echo))
                            except Exception: pass
                elif t=="r":
                    rows=max(1,int(msg.get("r",24))); cols=max(1,int(msg.get("c",80)))
                    if fd is not None and PTY_OK:
                        fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack("HHHH",rows,cols,0,0))
                        try:
                            import signal as _sig
                            os.killpg(os.getpgid(proc.pid),_sig.SIGWINCH)
                        except: pass
                    elif win_proc:
                        try: win_proc.setwinsize(rows,cols)
                        except: pass
            except: pass
    except: pass
    finally:
        alive[0]=False
        _unregister_live_session(user_id, _live_session_id)
        try: ws.close()
        except: pass
        if fd is not None:
            try: os.close(fd)
            except: pass
        if proc:
            try: proc.terminate()
            except: pass
        if win_proc:
            try: win_proc.terminate(force=True)
            except: pass
            try: win_proc.close()
            except: pass

@sock.route("/terminal")
def ws_terminal(ws):
    """Real Flask WebSocket route (flask-sock), replacing the previous
    raw-socket-hijack WSGIRequestHandler entirely. Requires a WSGI server
    flask-sock actually supports for this (Werkzeug dev server, or
    Gunicorn with the gthread worker class) — see the "Performance &
    limits" section of README.md. Auth logic is unchanged from before: same
    _validate_ws_cookie / _can_use_terminal checks, just now reading a
    normal Flask `request.cookies` instead of hand-parsed raw headers.

    flask-sock completes the WebSocket handshake before this function
    runs, so an unauthorized attempt can't get a clean HTTP 401/403 back
    the way a pre-handshake rejection could — instead it gets a short
    message frame and an immediate close, which the frontend terminal
    view already treats as a failed/ended session."""
    if not TERM_EN:
        try: ws.close()
        except Exception: pass
        return
    user = _validate_ws_cookie(request.cookies)
    if not user:
        try:
            ws.send("\x1b[31mUnauthorized\x1b[0m\r\n"); ws.close()
        except Exception: pass
        return
    if not _can_use_terminal(user):
        try:
            ws.send("\x1b[31mTerminal access not granted\x1b[0m\r\n"); ws.close()
        except Exception: pass
        return
    if not _terminal_session_unlocked(_sid_from_cookies(request.cookies)):
        try:
            ws.send("\x1b[31mTerminal is password-protected — unlock it from the Terminal tab first\x1b[0m\r\n"); ws.close()
        except Exception: pass
        return
    # This app runs under a fixed-size thread pool (see the
    # "Performance & limits" section of README.md) — every open terminal connection blocks
    # one of those threads for as long as the session stays open, which is
    # indefinite by design. With no cap, a single account could open enough
    # sessions to occupy the entire pool and take the whole app offline for
    # everyone, not just themselves — hence both a per-user cap (stop one
    # account from doing this alone) and a global cap well under the total
    # thread count (stop several different accounts from doing it together,
    # leaving threads free for ordinary HTTP traffic even at the ceiling).
    if _count_live_sessions("terminal", user["id"]) >= MAX_TERMINAL_SESSIONS_PER_USER:
        try:
            ws.send(f"\x1b[31mToo many terminal sessions already open for this account "
                     f"(max {MAX_TERMINAL_SESSIONS_PER_USER}) — close one first\x1b[0m\r\n"); ws.close()
        except Exception: pass
        return
    if _count_live_sessions("terminal") >= MAX_TERMINAL_SESSIONS_GLOBAL:
        try:
            ws.send("\x1b[31mServer is at its terminal-session capacity right now — try again shortly\x1b[0m\r\n"); ws.close()
        except Exception: pass
        return
    _run_terminal_session(ws, user["id"], _sid_from_cookies(request.cookies))

@sock.route("/ws/control")
def ws_control(ws):
    """A lightweight push channel every logged-in browser tab opens on
    load — it never carries terminal I/O or any app data, it exists
    purely so the server can tell an already-open tab "your permissions
    changed" or "you were logged out" the instant an admin acts, instead
    of the tab only finding out on its next request or a manual reload.
    Registered in the same _live_sessions registry as /terminal (kind=
    "control"), so _notify_permissions_changed can reach it directly."""
    user = _validate_ws_cookie(request.cookies)
    if not user:
        try: ws.close()
        except Exception: pass
        return
    # Each open tab holds one server thread for as long as it stays open, so an
    # account (or all accounts together) must not be able to use them all up.
    if (_count_live_sessions("control", user["id"]) >= MAX_CONTROL_SOCKETS_PER_USER
            or _count_live_sessions("control") >= MAX_CONTROL_SOCKETS_GLOBAL):
        try:
            ws.send(json.dumps({"type": "too_many"}))   # the page stops retrying instead of hammering
            ws.close()
        except Exception: pass
        return
    alive = [True]
    def _kill():
        alive[0] = False
        try: ws.close()
        except Exception: pass
    def _push(message: dict):
        ws.send(json.dumps(message))
    lsid = _register_live_session(user["id"], "control", _kill, _push, sid=_sid_from_cookies(request.cookies))
    try:
        while alive[0]:
            try:
                data = ws.receive(timeout=25)
            except Exception:
                break
            # The client never actually sends anything meaningful on this
            # channel — receive() here is only how we notice a disconnect
            # (returns None on close) versus our own 25s idle timeout
            # (also returns None, but the socket is still open), so check
            # simple-websocket's own connected flag to tell them apart.
            if data is None and not getattr(ws, "connected", True):
                break
            if data is None:
                # Idle: send a small frame so a tunnel / proxy in between doesn't drop a quiet connection
                # (Cloudflare closes idle sockets), and notice a dead peer promptly.
                try: ws.send('{"type":"ping"}')
                except Exception: break
    finally:
        _unregister_live_session(user["id"], lsid)
        try: ws.close()
        except Exception: pass

@sock.route("/ws/intercom")
def ws_intercom(ws):
    """The intercom's live audio and control channel (protocol in modules/intercom.py).

    Same trust model as /terminal: the session cookie is validated at the handshake, the connection
    is registered as a live session (kind "intercom") so revoking the permission closes it at once,
    and it rides the same port, so one tunnel carries everything. Text frames are JSON control
    messages, binary frames are audio. Everything is handled by intercom.Connection; this function
    only shuttles frames."""
    user = _validate_ws_cookie(request.cookies)
    if not user:
        try: ws.close()
        except Exception: pass
        return
    if not _can_use_intercom(user):
        try:
            ws.send(json.dumps({"t": "notice", "level": "error", "message": "Intercom access has not been granted"}))
            ws.close()
        except Exception: pass
        return
    if intercom.startup_summary()["ok"] is False and intercom.A.get_backend(refresh=True) is None:
        try:
            ws.send(json.dumps({"t": "notice", "level": "error", "fatal": True,
                                "message": intercom.startup_summary()["error"] or "No audio engine is available on this server."}))
            ws.close()
        except Exception: pass
        return

    # Each connection holds a server thread (plus two helpers), so cap them like terminals and control sockets.
    if (_count_live_sessions("intercom", user["id"]) >= INTERCOM_SOCKETS_PER_USER
            or _count_live_sessions("intercom") >= INTERCOM_SOCKETS_GLOBAL):
        try:
            ws.send(json.dumps({"t": "notice", "level": "error", "fatal": True,
                                "message": "Too many intercom connections are open. Close another tab and reload."}))
            ws.close()
        except Exception: pass
        return
    conn = intercom.open_connection(user, ws.send, ws.close)
    lsid = _register_live_session(user["id"], "intercom", conn.shutdown, sid=_sid_from_cookies(request.cookies))
    try:
        while conn.alive:
            try:
                data = ws.receive(timeout=25)
            except Exception:
                break
            if data is None:
                if not getattr(ws, "connected", True):
                    break
                continue
            if isinstance(data, (bytes, bytearray)):
                conn.on_audio(bytes(data))
            else:
                try:
                    msg = json.loads(data)
                except Exception:
                    conn.push_json({"t": "notice", "level": "warn",
                                    "message": "The server received a message it could not read. If this keeps "
                                               "happening, reload the page."})
                    continue
                if isinstance(msg, dict):
                    conn.on_json(msg)
    except Exception as e:
        try:
            intercom._log_crash(e, "live audio connection")
            conn.push_json({"t": "notice", "level": "error",
                            "message": f"The live connection stopped because of an unexpected problem "
                                       f"({type(e).__name__}). The page will reconnect; if it keeps failing, "
                                       "see the error log."})
            time.sleep(0.3)
        except Exception:
            pass
    finally:
        _unregister_live_session(user["id"], lsid)
        conn.shutdown()
        try: ws.close()
        except Exception: pass

