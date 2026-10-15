"""
modules/decorators.py — Route decorators: require_auth, require_admin, require_superuser and friends.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify
from flask import redirect
from flask import request
from functools import wraps
from modules.error_log import _can_use_intercom, _current_user, _is_admin_level


# ── Decorators ────────────────────────────────────────────────────────
def require_auth(fn):
    @wraps(fn)
    def w(*a, **k):
        if not _current_user():
            return (jsonify({"error": "unauthorized"}), 401) \
                if request.path.startswith("/api/") else redirect("/login")
        return fn(*a, **k)
    return w

def require_admin(fn):
    """Gate for general admin-level features (admin panel, guest management,
    audit log). Both admin and superuser pass — superuser is a superset."""
    @wraps(fn)
    def w(*a, **k):
        user = _current_user()
        if not user:
            return (jsonify({"error": "unauthorized"}), 401) \
                if request.path.startswith("/api/") else redirect("/login")
        if not _is_admin_level(user):
            return (jsonify({"error": "admin access required"}), 403) \
                if request.path.startswith("/api/") else redirect("/")
        return fn(*a, **k)
    return w

def require_superuser(fn):
    """Gate for rank-management features: only the single Super User passes."""
    @wraps(fn)
    def w(*a, **k):
        user = _current_user()
        if not user:
            return (jsonify({"error": "unauthorized"}), 401) \
                if request.path.startswith("/api/") else redirect("/login")
        if user.get("role") != "superuser":
            return (jsonify({"error": "superuser access required"}), 403) \
                if request.path.startswith("/api/") else redirect("/")
        return fn(*a, **k)
    return w

def require_docker_access(fn):
    """Admin-level gate, plus the per-admin 'docker' permission flag.
    Superuser is never restricted by the flag."""
    @wraps(fn)
    def w(*a, **k):
        user = _current_user()
        if not user:
            return (jsonify({"error": "unauthorized"}), 401) \
                if request.path.startswith("/api/") else redirect("/login")
        if not _is_admin_level(user):
            return (jsonify({"error": "admin access required"}), 403) \
                if request.path.startswith("/api/") else redirect("/")
        if user.get("role") == "admin" and not user.get("permissions", {}).get("docker", True):
            return jsonify({"error": "Docker access has been restricted for this account"}), 403
        return fn(*a, **k)
    return w

def require_intercom_access(fn):
    """Gate for every /api/intercom/* HTTP route — _can_use_intercom
    already covers superuser-always-on / per-user-flag-otherwise, this
    just wires it into the same decorator shape as the other gates above."""
    @wraps(fn)
    def w(*a, **k):
        user = _current_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if not _can_use_intercom(user):
            return jsonify({"error": "Intercom access has not been granted for this account"}), 403
        return fn(*a, **k)
    return w
