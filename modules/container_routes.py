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
import os
import subprocess
import threading
from modules import diagnose, state, winplat
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

_REBUILDS: dict = {}               # container name -> {"done", "ok", "log"}  (last rebuild; in memory)
_rebuild_lock = threading.Lock()

def _rebuild_worker(name, cmd, cwd, user):
    job = _REBUILDS[name]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", **winplat.no_window())
        for line in proc.stdout:
            job["log"].append(line.rstrip()[:500])
            del job["log"][:-200]
        job["ok"] = proc.wait() == 0
    except Exception as e:
        job["log"].append(f"Could not run docker compose: {e}")
        job["ok"] = False
    job["done"] = True
    _audit("container_rebuilt" if job["ok"] else "container_rebuild_failed", user, target=name)

@app.route("/api/containers/<cid>/rebuild", methods=["POST"])
@require_docker_access
def api_ctr_rebuild(cid):
    """Re-create a container from its compose file, so an edited docker-compose.yml takes effect.
    `docker compose up -d --build --force-recreate <service>` on the project the container came
    from: images are rebuilt where the file builds one, volumes and data are kept."""
    if not state.DOCKER_OK: return jsonify({"error":"Docker not available"}), 503
    try:
        c = state._docker.containers.get(cid)
    except Exception as e: return _err(e, 404)
    labels = (c.attrs.get("Config") or {}).get("Labels") or {}
    files = [f for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f.strip()]
    project, service = labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")
    if not (files and project and service):
        return jsonify({"error": "This container wasn't created from a compose file, so it can't be rebuilt here. "
                                 "Remove it and create it again."}), 400
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        return jsonify({"error": f"The compose file {missing[0]} isn't there any more, so there is nothing to rebuild from."}), 400
    workdir = labels.get("com.docker.compose.project.working_dir") or os.path.dirname(files[0])
    cmd = ["docker", "compose", "-p", project]
    for f in files:
        cmd += ["-f", f]
    cmd += ["up", "-d", "--build", "--force-recreate", service]
    name = c.name
    with _rebuild_lock:
        if name in _REBUILDS and not _REBUILDS[name]["done"]:
            return jsonify({"error": "A rebuild of this container is already running."}), 409
        _REBUILDS[name] = {"done": False, "ok": None, "log": []}
    threading.Thread(target=_rebuild_worker, args=(name, cmd, workdir if os.path.isdir(workdir) else None, _current_user()),
                     daemon=True, name=f"rebuild-{name}").start()
    return jsonify({"ok": True, "name": name})

@app.route("/api/containers/rebuild/<name>")
@require_docker_access
def api_ctr_rebuild_status(name):
    j = _REBUILDS.get(name)
    if not j: return jsonify({"error": "No rebuild in progress"}), 404
    return jsonify({"done": j["done"], "ok": j["ok"], "log": "\n".join(j["log"][-60:])})

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
