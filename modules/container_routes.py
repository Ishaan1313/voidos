"""
modules/container_routes.py — Live stats endpoint and Docker container management.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify
from flask import request
from modules import diagnose, state
from modules.activity import _audit
from modules.system import _ci, _stats
from modules.error_log import _current_user, _err
from modules.web import app
from modules.decorators import require_auth, require_docker_access


# ══════════════════════════════════════════════════════════════════════
# ROUTES — STATS
# ══════════════════════════════════════════════════════════════════════
@app.route("/api/stats")
@require_auth
def api_stats(): return jsonify(_stats())

# ══════════════════════════════════════════════════════════════════════
# ROUTES — DOCKER (admin only)
# ══════════════════════════════════════════════════════════════════════
@app.route("/api/containers")
@require_docker_access
def api_containers():
    if not state.DOCKER_OK: return jsonify({"error":state.DOCKER_ERROR,"containers":[]}), 200
    try: return jsonify([_ci(c) for c in state._docker.containers.list(all=True)])
    except Exception as e: return _err(e)

@app.route("/api/containers/<cid>/action", methods=["POST"])
@require_docker_access
def api_ctr_act(cid):
    if not state.DOCKER_OK: return jsonify({"error":"Docker not available"}), 503
    act = (request.get_json(silent=True) or {}).get("action","")
    if act not in ("start","stop","restart"): return jsonify({"error":"Invalid action"}), 400
    try:
        c = state._docker.containers.get(cid); getattr(c,act)(); c.reload()
        _audit("container_"+act, _current_user(), target=c.name)
        return jsonify({"ok":True,"status":c.status})
    except Exception as e: return _err(e)

@app.route("/api/containers/<cid>/logs")
@require_docker_access
def api_ctr_logs(cid):
    if not state.DOCKER_OK: return jsonify({"error":"Docker not available"}), 503
    try:
        c = state._docker.containers.get(cid)
        logs = c.logs(tail=300,timestamps=True).decode("utf-8",errors="replace")[:500_000]
        return jsonify({"logs":logs,"name":c.name})
    except Exception as e: return _err(e)

@app.route("/api/containers/<cid>/diagnose")
@require_docker_access
def api_ctr_diagnose(cid):
    """Plain-language reason a container is restarting or stopped (modules/diagnose.py)."""
    if not state.DOCKER_OK: return jsonify({"error":"Docker not available"}), 503
    try:
        c = state._docker.containers.get(cid)
    except Exception as e: return _err(e, 404)
    out = diagnose.diagnose_container(c)
    h = diagnose.host_profile()
    out["host"] = {k: h.get(k) for k in ("model","machine","ram_mb","swap_mb","page_size","docker_arch","os_bits")}
    return jsonify(out)

@app.route("/api/containers/<cid>/delete", methods=["POST"])
@require_docker_access
def api_ctr_delete(cid):
    if not state.DOCKER_OK: return jsonify({"error":"Docker not available"}), 503
    remove_image = bool((request.get_json(silent=True) or {}).get("remove_image", False))
    try:
        c = state._docker.containers.get(cid)
    except Exception as e: return _err(e, 404)
    name = c.name
    # Capture the image identity *before* removing the container — this is
    # what makes the follow-up image removal reliable rather than trusting
    # anything the client sent: the server looks up the image straight from
    # the container object it just resolved, not from a client-supplied id.
    image_id = c.image.id
    image_tags = c.image.tags or [image_id[:19]]
    try:
        c.remove(force=True)  # force=True stops it first if still running, same as a manual `docker rm -f`
    except Exception as e: return _err(e)

    image_removed, image_error = False, None
    if remove_image:
        try:
            state._docker.images.remove(image_id, force=False)
            image_removed = True
        except Exception as e:
            # Most commonly: another container still uses this image, or it's
            # tagged under more than one repository. Not a failure of the
            # container removal itself (which already succeeded above) —
            # report it back so the UI can say so, rather than a 500.
            image_error = str(e)

    _audit("container_deleted", _current_user(), target=name,
           detail=f"image={', '.join(image_tags)}, remove_image={remove_image}, image_removed={image_removed}"
                  + (f", image_error={image_error}" if image_error else ""))
    return jsonify({"ok": True, "image_removed": image_removed, "image_error": image_error})
