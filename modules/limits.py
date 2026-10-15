"""
modules/limits.py — Fair-use limits for downloads, so one account (or one visitor
address) can't hold every request thread with slow transfers.

A download holds a server thread for as long as the client takes to receive it,
so a client that reads slowly can park threads indefinitely. Each download takes a
slot (per account for signed-in users, per address for public share links, plus a
global cap that always leaves threads free for ordinary pages) and gives it back
when the response is closed, including when the client disconnects half way.
"""
import threading
import weakref

from flask import jsonify

from modules.config import MAX_DOWNLOADS_GLOBAL, MAX_DOWNLOADS_PER_USER, MAX_SHARE_DOWNLOADS_PER_IP

_lock = threading.Lock()
_held: dict = {}          # ("u", user id) or ("ip", address) -> downloads in progress
_total = 0


def acquire_download(user_id: str | None = None, ip: str | None = None):
    """(release, None) when there is room, else (None, message)."""
    global _total
    key = ("u", user_id) if user_id else ("ip", ip or "?")
    cap = MAX_DOWNLOADS_PER_USER if user_id else MAX_SHARE_DOWNLOADS_PER_IP
    with _lock:
        if _held.get(key, 0) >= cap:
            who = "You already have" if user_id else "This address already has"
            return None, (f"{who} {cap} download(s) in progress \u2014 "
                          "wait for one to finish before starting another")
        if _total >= MAX_DOWNLOADS_GLOBAL:
            return None, "The server is busy with other downloads right now \u2014 try again in a moment"
        _held[key] = _held.get(key, 0) + 1
        _total += 1
    released = threading.Event()

    def release():
        global _total
        if released.is_set():
            return
        released.set()
        with _lock:
            n = _held.get(key, 1) - 1
            if n <= 0:
                _held.pop(key, None)
            else:
                _held[key] = n
            _total = max(0, _total - 1)
    return release, None


def guarded_send(make_response, user_id: str | None = None, ip: str | None = None):
    """Run `make_response()` (usually a send_file call) while holding a download
    slot; the slot is returned when the response is closed. Over the limit:
    a 429 with an explanation."""
    release, err = acquire_download(user_id, ip)
    if release is None:
        return jsonify({"error": err}), 429
    try:
        resp = make_response()
    except BaseException:
        release()
        raise
    resp.call_on_close(release)
    # Normally the server calls close() when the response ends. On Windows' built-in
    # server it doesn't when the client drops mid-download, so the slot is also given
    # back when the response body object is discarded.
    body = resp.response
    try:
        weakref.finalize(body, release)
    except TypeError:                       # a list/tuple/bytes body: nothing is streaming
        if isinstance(body, (list, tuple, bytes, str)):
            release()
        else:
            def _stream():
                try:
                    yield from body
                finally:
                    release()
            resp.response = _stream()
    return resp
