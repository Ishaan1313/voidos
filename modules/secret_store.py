"""
modules/secret_store.py — encryption at rest for small secrets Void OS has to
keep (currently the Dropbox / Google Drive login tokens in cloud_tokens.json).

The key is derived from VOID_SECRET_KEY (in .env), so a copy of the data
folder, a backup of it, or a stray file listing is not enough to use the
tokens — the .env file is needed too. Someone who can read .env on the same
machine can still decrypt them; this is protection against partial leaks, not
against a full compromise of the server.

Uses the `cryptography` package (Fernet: AES-128-CBC + HMAC-SHA256). If it is
somehow not installed, nothing breaks: seal() returns the data unchanged and
reading plain data keeps working, so cloud features never stop because of it
(requirements.txt installs it, and voidos.py installs missing packages).

If VOID_SECRET_KEY is changed, previously sealed data can no longer be read —
callers treat that as "nothing stored" and the user simply reconnects.
"""
from __future__ import annotations

import base64
import json

from modules.config import SECRET_KEY

_fernet = None
_missing = False


def _f():
    global _fernet, _missing
    if _fernet is not None or _missing:
        return _fernet
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        _missing = True
        return None
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"voidos-secret-store-v1").derive(str(SECRET_KEY).encode("utf-8"))
    _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


_warned = False


def _warn_plain():
    """Said once: without the `cryptography` package, saved logins are written unencrypted."""
    global _warned
    if not _warned:
        _warned = True
        import logging
        logging.getLogger("voidos").warning(
            "The 'cryptography' package is missing, so saved cloud/network logins are stored UNENCRYPTED. "
            "Install it: pip install cryptography")


def available() -> bool:
    """True when encryption is actually possible on this install."""
    return _f() is not None


def is_sealed(obj) -> bool:
    return isinstance(obj, dict) and obj.get("voidos_enc") == 1 and isinstance(obj.get("data"), str)


def seal(obj):
    """JSON-able `obj` -> a small JSON-able envelope (or `obj` itself when
    encryption isn't available)."""
    f = _f()
    if f is None:
        _warn_plain()
        return obj
    token = f.encrypt(json.dumps(obj, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return {"voidos_enc": 1, "data": token}


def unseal(envelope):
    """Inverse of seal(). Raises ValueError if it can't be read (wrong key,
    corrupted, or the cryptography package is missing)."""
    f = _f()
    if f is None:
        raise ValueError("the 'cryptography' package is not installed")
    try:
        return json.loads(f.decrypt(envelope["data"].encode("ascii")).decode("utf-8"))
    except Exception as e:  # InvalidToken, bad JSON, ...
        raise ValueError("stored data can't be decrypted with this server's secret key") from e
