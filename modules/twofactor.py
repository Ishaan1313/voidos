"""
modules/twofactor.py — Optional two-factor sign-in: time-based one-time codes
(TOTP, RFC 6238 — the 6-digit codes of Google Authenticator, Aegis, 1Password,
Authy...) plus one-time backup codes.

Everything here is plain logic with no Flask routes (those live in
twofa_routes.py), so the sign-in route can use it without importing forward.

Stored per user in users.json:
    "twofa": {"enabled": true, "secret": <sealed>, "last_step": 12345,
              "backup": [sha256 hex of each unused backup code], "enabled_at": "..."}
    "twofa_pending": <sealed secret>    # between "show the QR" and "enter the first code"
The secret is encrypted with secret_store (key derived from VOID_SECRET_KEY),
so users.json alone — or a backup of it — can't generate codes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

from modules import secret_store
from modules.config import OS_NAME
from modules.users import _users_load, _users_save
from modules.web import _users_lock

PERIOD = 30            # seconds a code is valid for
DIGITS = 6
BACKUP_COUNT = 10
# Wrong codes allowed per account before it pauses for a while. Without this a
# stolen password plus a million guesses (spread over many IPs) would get in.
MAX_CODE_FAILS = 5
CODE_LOCK_S = 300
_BACKUP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (0/o, 1/l/i)

_fails: dict[str, tuple[int, float]] = {}      # uid -> (wrong codes, locked until)
_fails_lock = threading.Lock()


# ── TOTP ──────────────────────────────────────────────────────────────
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")      # 32 chars, no padding


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(num).zfill(DIGITS)


def match_totp(secret_b32: str, code: str, last_step: int = 0) -> int | None:
    """The time step `code` is valid for (this one or a neighbour, for clock
    drift), or None. A step at or below `last_step` was already used."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now_step = int(time.time() // PERIOD)
    for step in (now_step - 1, now_step, now_step + 1):
        if step > last_step and hmac.compare_digest(_hotp(secret_b32, step), code):
            return step
    return None


def provisioning_uri(secret_b32: str, username: str) -> str:
    issuer = quote(OS_NAME or "Void OS")
    return (f"otpauth://totp/{issuer}:{quote(username)}?secret={secret_b32}"
            f"&issuer={issuer}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")


def qr_svg(data: str) -> str | None:
    """An inline SVG QR code, or None if the optional `segno` package isn't
    installed (the page then shows the setup key to type in by hand)."""
    try:
        import segno
    except ImportError:
        return None
    try:
        return segno.make(data, error="m").svg_inline(scale=5, border=2, dark="#000000", light="#ffffff")
    except Exception:
        return None


def spaced(secret_b32: str) -> str:
    return " ".join(secret_b32[i:i + 4] for i in range(0, len(secret_b32), 4))


# ── backup codes ──────────────────────────────────────────────────────
def _norm_backup(code: str) -> str:
    return "".join(ch for ch in str(code).lower() if ch.isalnum())


def _hash_backup(code: str) -> str:
    return hashlib.sha256(_norm_backup(code).encode("utf-8")).hexdigest()


def new_backup_codes() -> list[str]:
    """Plain codes to show ONCE, as xxxx-xxxx."""
    out = []
    for _ in range(BACKUP_COUNT):
        raw = "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(8))
        out.append(raw[:4] + "-" + raw[4:])
    return out


# ── reading a user's state ────────────────────────────────────────────
def is_enabled(user: dict | None) -> bool:
    return bool(user and (user.get("twofa") or {}).get("enabled"))


def backup_remaining(user: dict | None) -> int:
    return len((user or {}).get("twofa", {}).get("backup", []) or [])


def _open_secret(stored) -> str:
    if secret_store.is_sealed(stored):
        return secret_store.unseal(stored)          # ValueError if the key changed
    return str(stored)                              # stored plain (cryptography missing)


# ── per-account wrong-code throttle ───────────────────────────────────
def locked_for(uid: str) -> int:
    with _fails_lock:
        _c, until = _fails.get(uid, (0, 0.0))
    return max(0, int(until - time.time())) if until else 0


def _code_failed(uid: str) -> None:
    with _fails_lock:
        c, until = _fails.get(uid, (0, 0.0))
        if until and time.time() >= until:
            c = 0
        c += 1
        _fails[uid] = (0, time.time() + CODE_LOCK_S) if c >= MAX_CODE_FAILS else (c, 0.0)


def _code_ok(uid: str) -> None:
    with _fails_lock:
        _fails.pop(uid, None)


# ── checking a code at sign-in / before a sensitive change ────────────
def check_code(uid: str, code: str) -> tuple[bool, str]:
    """Verify an authenticator code or a backup code for `uid`, consuming it.
    Returns (ok, reason): reason is "" on success, else "invalid", "locked"
    or "unreadable" (the saved secret can't be decrypted — the server's secret
    key changed; the Super User has to reset this account's 2FA)."""
    wait = locked_for(uid)
    if wait:
        return False, "locked"
    code = str(code or "").strip()
    if not code:
        return False, "invalid"
    with _users_lock:
        users = _users_load()
        u = next((x for x in users if x.get("id") == uid), None)
        if not u or not is_enabled(u):
            return False, "invalid"
        tf = u["twofa"]
        digits = "".join(ch for ch in code if ch.isdigit())
        used_backup = False
        ok = False
        if len(digits) == DIGITS and len(_norm_backup(code)) == DIGITS:
            try:
                secret = _open_secret(tf.get("secret"))
            except ValueError:
                return False, "unreadable"
            step = match_totp(secret, digits, int(tf.get("last_step", 0)))
            if step is not None:
                tf["last_step"] = step
                ok = True
        else:
            h = _hash_backup(code)
            hit = next((b for b in tf.get("backup", []) if hmac.compare_digest(b, h)), None)
            if hit is not None:
                tf["backup"] = [b for b in tf["backup"] if b != hit]
                ok = used_backup = True
        if ok:
            _users_save(users)
    if not ok:
        _code_failed(uid)
        return False, "invalid"
    _code_ok(uid)
    return True, "backup" if used_backup else ""


# ── turning it on / off ───────────────────────────────────────────────
def begin_setup(uid: str) -> str | None:
    """Make (and remember, until activated) a fresh secret. None if no such user."""
    secret = new_secret()
    with _users_lock:
        users = _users_load()
        u = next((x for x in users if x.get("id") == uid), None)
        if not u:
            return None
        u["twofa_pending"] = secret_store.seal(secret)
        _users_save(users)
    return secret


def pending_secret(user: dict) -> str | None:
    p = user.get("twofa_pending")
    if not p:
        return None
    try:
        return _open_secret(p)
    except ValueError:
        return None


def activate(uid: str, code: str) -> list[str] | None:
    """Check the first code from the pending secret; if right, turn 2FA on and
    return the backup codes to show once. None if the code is wrong."""
    with _users_lock:
        users = _users_load()
        u = next((x for x in users if x.get("id") == uid), None)
        if not u or is_enabled(u):
            return None
        secret = pending_secret(u)
        if not secret:
            return None
        step = match_totp(secret, code, 0)
        if step is None:
            return None
        codes = new_backup_codes()
        u["twofa"] = {
            "enabled": True, "secret": secret_store.seal(secret), "last_step": step,
            "backup": [_hash_backup(c) for c in codes],
            "enabled_at": datetime.now(timezone.utc).isoformat(),
        }
        u.pop("twofa_pending", None)
        _users_save(users)
    return codes


def regenerate_backup(uid: str) -> list[str] | None:
    with _users_lock:
        users = _users_load()
        u = next((x for x in users if x.get("id") == uid), None)
        if not u or not is_enabled(u):
            return None
        codes = new_backup_codes()
        u["twofa"]["backup"] = [_hash_backup(c) for c in codes]
        _users_save(users)
    return codes


def remove(uid: str) -> bool:
    """Turn 2FA off (also discards a half-finished setup). True if anything was removed."""
    with _users_lock:
        users = _users_load()
        u = next((x for x in users if x.get("id") == uid), None)
        if not u:
            return False
        had = is_enabled(u) or "twofa_pending" in u
        u.pop("twofa", None)
        u.pop("twofa_pending", None)
        if had:
            _users_save(users)
    _code_ok(uid)
    return had
