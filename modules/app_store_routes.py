"""
modules/app_store_routes.py — App Store HTTP routes: catalog, preview, install, update, uninstall.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import jsonify, request, send_file
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from modules.config import APP_DATA_DIR, CATALOG_DIR
from modules.app_store import (
    INSTALL_JOBS, MAX_INSTALL_JOBS_PER_USER, _IMAGE_LINE_RE, _apply_arm_compat_profile,
    _ComposeProgress, _apply_options, _friendly_names, stack_project, _extract_image_tags, _install_worker, _normalize_env_quotes,
    _option_states, _preflight, _job_begin, _job_finish, _job_prune,
    _job_public, _job_set, _load_catalog, _resolve_latest_tag,
    _scan_compose_risks, _st_load, _st_save, _substitute_latest_tags,
)
from modules.file_helpers import _aload, _asave
from modules.web import _apps_lock, _stacks_lock, app
from modules.activity import _audit
from modules.error_log import _can_use_terminal, _current_user, _log_exception
from modules import diagnose, errors, winplat
from modules.decorators import require_auth, require_docker_access


def _report_app_job(worker, job_id: str, user: dict, label: str, verb: str):
    """Runs an install/update worker, then reports the outcome once: an
    in-app notification for whoever started it (they may well have closed
    the App Store tab by then), plus an app_install_failed audit entry on
    failure so admins are told and the external channels can fire."""
    def run(*args):
        job = INSTALL_JOBS.get(job_id) or {}
        try:
            _job_begin(job, job_id, user, label, "update" if verb == "update" else "install")
            worker(*args)
        finally:
            job = INSTALL_JOBS.get(job_id) or {}
            if job.get("error"):
                _audit("app_install_failed", user, target=label, detail=f"{verb}: {str(job['error'])[:200]}")
            if job:
                job["done"] = True      # even if the worker died without saying so
                _job_finish(job)
    return run

def _update_worker(job_id: str, tile_id: str, user: dict):
    """Part C: re-resolves the latest image tag(s) for an already-installed
    app and recreates only the containers whose image actually changed.
    Deliberately does NOT touch instance_id, DATA_DIR, or any secret —
    _substitute_latest_tags operates as a targeted swap of just the image:
    lines in the EXISTING compose file already on disk, so nothing else in
    it (including secrets baked into env values) is regenerated or
    re-rendered from the template. Shares INSTALL_JOBS/the install status
    endpoint with the install path (kind: "update"), per Part C's own
    recommendation, rather than duplicating the polling machinery."""
    job = INSTALL_JOBS[job_id]
    try:
        stacks = _st_load()
        stack = next((s for s in stacks if s["tile_id"] == tile_id), None)
        if not stack:
            job["error"] = "This app's install record was not found"; job["done"] = True; return
        compose_path = Path(stack["compose_path"])
        if not compose_path.exists():
            job["error"] = f"Compose file not found at {compose_path} — was it moved or deleted?"; job["done"] = True; return
        current_text = compose_path.read_text(encoding="utf-8")
        new_text, tag_notes = _substitute_latest_tags(current_text)
        for note in tag_notes:
            job["log"].append(f"Image: {note}")
        if new_text == current_text:
            job["log"].append("Already up to date — no image tags changed.")
        else:
            compose_path.write_text(new_text, encoding="utf-8")
            job["log"].append(f"Updated {compose_path}")

        instance_id = stack["instance_id"]
        project = stack_project(stack)
        _job_set(job, 8, "Checking the latest version")
        for step in (["pull"], ["up", "-d"]):
            try:
                proc = subprocess.Popen(["docker", "compose", "-f", str(compose_path), "-p", project, *step],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace", **winplat.no_window())
            except Exception as e:
                job["error"] = f"Could not run docker compose {' '.join(step)}: {e}"; job["done"] = True; return
            cprog = _ComposeProgress(job, new_text, lo=10 if step[0] == "pull" else 60,
                                     hi=60 if step[0] == "pull" else 95, pulling=step[0] == "pull")
            _job_set(job, 10 if step[0] == "pull" else 60, "Downloading the new version" if step[0] == "pull" else "Restarting the app")
            for line in proc.stdout:
                job["log"].append(line.rstrip())
                cprog.feed(line)
            rc = proc.wait()
            if rc != 0:
                job["error"] = f"docker compose {' '.join(step)} failed — see log"; job["done"] = True; return

        with _stacks_lock:
            stacks = _st_load()
            for s in stacks:
                if s["tile_id"] == tile_id:
                    s["image_tags"] = _extract_image_tags(new_text)
                    s["last_updated_at"] = time.time()
            _st_save(stacks)

        job["tile_id"] = tile_id
        job["done"] = True
        cat = next((c for c in _load_catalog() if c["id"] == stack["catalog_id"]), None)
        _audit("appstore_update", user, target=(cat or {}).get("name", stack["catalog_id"]),
               detail=f"instance {instance_id}")
    except Exception as e:
        job["error"] = f"{errors.describe(e)} (ref {_log_exception(e, source='job', context='update ' + tile_id)})"
        job["done"] = True

@app.route("/api/appstore/installed")
@require_docker_access
def api_appstore_installed():
    stacks = _st_load()
    catalog_by_id = {c["id"]: c for c in _load_catalog()}
    apps_by_id = {a["id"]: a for a in _aload()}
    out = []
    for s in stacks:
        tile = apps_by_id.get(s["tile_id"])
        cat = catalog_by_id.get(s["catalog_id"])
        out.append({
            "tile_id": s["tile_id"], "catalog_id": s["catalog_id"],
            "name": (cat or {}).get("name", s["catalog_id"]), "icon": (cat or {}).get("icon", "📦"),
            "instance_id": s["instance_id"], "installed_at": s["installed_at"],
            "tile_exists": tile is not None,
            "url": (tile or {}).get("url"),
            "image_tags": s.get("image_tags") or {},
            "last_updated_at": s.get("last_updated_at"),
            "edited": bool(s.get("edited")),
        })
    out.sort(key=lambda x: x["installed_at"], reverse=True)
    return jsonify(out)

_ICON_MIME = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "webp": "image/webp", "gif": "image/gif", "ico": "image/x-icon"}

@app.route("/api/appstore/icon/<app_id>")
@require_auth
def api_appstore_icon(app_id):
    """An app's icon from the catalog package. Any signed-in account may load it, since installed
    tiles show it on the dashboard for everyone."""
    from modules import catalog_pack
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", app_id):
        return "", 404
    ic = catalog_pack.icon_file(CATALOG_DIR / app_id)
    if ic is None:
        return "", 404
    resp = send_file(ic, mimetype=_ICON_MIME.get(ic.suffix.lstrip("."), "application/octet-stream"))
    resp.headers["Cache-Control"] = "private, max-age=86400"
    if ic.suffix == ".svg":
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"  # no scripts from an svg
    return resp

@app.route("/api/appstore/catalog")
@require_docker_access
def api_appstore_catalog():
    """The catalog, each entry marked with whether it is already installed (`installed`,
    plus `tile_ids` of the installed copies) and whether the caller has an install
    running right now (`installing`: the job's id, progress and stage)."""
    user = _current_user()
    tiles: dict = {}
    for s in _st_load():
        tiles.setdefault(s["catalog_id"], []).append(s["tile_id"])
    running = {}
    for jid, j in INSTALL_JOBS.items():
        if j.get("kind") == "install" and not j.get("done") and j.get("user_id") == user["id"]:
            running[j.get("catalog_id")] = {"job_id": jid, "progress": j.get("progress"), "stage": j.get("stage") or ""}
    out = []
    for c in _load_catalog():
        out.append({**c, "installed": c["id"] in tiles, "tile_ids": tiles.get(c["id"], []),
                    "installing": running.get(c["id"])})
    return jsonify(out)

@app.route("/api/appstore/jobs")
@require_docker_access
def api_appstore_jobs():
    """The caller's running installs/updates plus ones that finished in the last half
    hour. The page uses it to pick them up again after a reload."""
    user = _current_user()
    _job_prune()
    mine = [(jid, j) for jid, j in INSTALL_JOBS.items() if j.get("user_id") == user["id"]]
    mine.sort(key=lambda x: x[1].get("created") or 0)
    return jsonify({"jobs": [_job_public(jid, j) for jid, j in mine]})

def _clean_options(entry: dict, raw) -> dict:
    """The install options the client sent, limited to ids this entry declares, as booleans."""
    known = {str(o.get("id")) for o in entry.get("options") or []}
    if not isinstance(raw, dict):
        return {}
    return {k: bool(v) for k, v in raw.items() if k in known}

@app.route("/api/appstore/preview", methods=["POST"])
@require_docker_access
def api_appstore_preview():
    """Part A: renders a catalog entry's template with real values (a
    fresh instance_id, real secret values — same substitution
    _install_worker itself does) and returns it for review before install.
    Also resolves the latest image tag(s) (Part B) so the admin sees
    exactly which version will actually be installed, not just whatever's
    pinned in the template on disk.
    can_edit tells the frontend whether to offer the textarea as editable
    at all — a docker-only account (no terminal) can still preview/read
    this, matching "plain install from catalog stays gated on docker
    alone," but submitting an edited compose_override to /install is
    enforced server-side there regardless of what this flag says."""
    user = _current_user()
    body = request.get_json(silent=True) or {}
    catalog_id = str(body.get("catalog_id", ""))
    entry = next((c for c in _load_catalog() if c["id"] == catalog_id), None)
    if not entry:
        return jsonify({"error": "Unknown catalog entry"}), 404
    chosen = _clean_options(entry, body.get("options"))
    instance_id = uuid.uuid4().hex[:12]
    data_dir = APP_DATA_DIR / instance_id
    tmpl = (CATALOG_DIR / catalog_id / "docker-compose.yml.tmpl").read_text(encoding="utf-8")
    subs = {"INSTANCE_ID": instance_id, "DATA_DIR": data_dir.as_posix()}  # forward slashes: what Docker Desktop's compose expects on Windows
    secret_values = {}
    for secret_name in entry.get("secrets", []):
        val = secrets.token_hex(16)
        subs[secret_name] = val
        secret_values[secret_name] = val
    rendered, _project = _friendly_names(entry, tmpl)
    for k, v in subs.items():
        rendered = rendered.replace("{{" + k + "}}", v)
    rendered, _nq = _normalize_env_quotes(rendered)
    rendered, opt_notes = _apply_options(entry, rendered, chosen)
    rendered, tag_notes = _substitute_latest_tags(rendered)
    rendered, arm_notes = _apply_arm_compat_profile(catalog_id, rendered)
    tag_notes = [*opt_notes, *tag_notes, *arm_notes]
    warnings_found = _scan_compose_risks(rendered, data_dir)
    opts = _option_states(entry, chosen, diagnose.host_profile())
    h = diagnose.host_profile()
    return jsonify({
        "options": [{k: o.get(k) for k in ("id", "label", "description", "enabled", "recommended_off")} for o in opts],
        "preflight": _preflight(entry, data_dir, opts),
        "host": {k: h.get(k) for k in ("model", "machine", "ram_mb", "swap_mb", "page_size", "docker_arch", "os_bits")},
        "compose": rendered, "instance_id": instance_id,
        "secrets_generated": secret_values or None,
        "tag_notes": tag_notes, "warnings": warnings_found,
        "can_edit": _can_use_terminal(user),
    })

@app.route("/api/appstore/install", methods=["POST"])
@require_docker_access
def api_appstore_install():
    user = _current_user()
    d = request.get_json(silent=True) or {}
    catalog_id = str(d.get("catalog_id", ""))
    entry = next((c for c in _load_catalog() if c["id"] == catalog_id), None)
    if not entry:
        return jsonify({"error": "Unknown catalog entry"}), 404

    compose_override = d.get("compose_override")
    chosen = _clean_options(entry, d.get("options"))
    preview_instance_id = d.get("instance_id")
    preview_secrets = d.get("secrets_generated") if isinstance(d.get("secrets_generated"), dict) else None
    # instance_id becomes a folder name under the app-data folder, and the secrets are
    # written into the compose file *after* the safety scan - so accept only the shapes
    # the preview step generates (12 hex characters; short alphanumeric secrets named by
    # this catalog entry). Anything else could escape the folder or add YAML.
    if preview_instance_id is not None and not re.fullmatch(r"[0-9a-f]{12}", str(preview_instance_id)):
        return jsonify({"error": "That install session isn't valid — start the install again from the preview."}), 400
    if preview_secrets is not None:
        allowed = set(entry.get("secrets", []))
        if (set(preview_secrets) - allowed
                or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{8,64}", v) for v in preview_secrets.values())):
            return jsonify({"error": "The generated credentials in that request aren't valid — start the install again from the preview."}), 400
    if compose_override is not None:
        # Part A's security note: free-form compose editing is a materially
        # different capability than "install from a fixed, project-authored
        # catalog" — requiring docker AND terminal here means an account can
        # only reach this path if it could already get an equivalent outcome
        # via the terminal anyway. The plain "install unedited" path below
        # stays gated on docker alone, exactly as it was before this feature.
        if not _can_use_terminal(user):
            return jsonify({"error": "Editing the compose file requires Terminal access as well as Docker access"}), 403
        if not preview_instance_id:
            return jsonify({"error": "Missing instance_id — install with an edited compose file must follow a preview call"}), 400
        instance_id = str(preview_instance_id)
        data_dir = APP_DATA_DIR / instance_id
        risky = _scan_compose_risks(compose_override, data_dir)
        if risky and not d.get("confirm_risky"):
            # Warn-and-confirm, not a silent block — a legitimate template might
            # reasonably need one of these; the Super User, who understands the
            # tradeoff, can still proceed by resubmitting with confirm_risky.
            return jsonify({"error": "risky_yaml", "warnings": risky}), 409
        if risky and user.get("role") != "superuser":
            return jsonify({"error": "Only the Super User can approve an app that asks for elevated host access.",
                            "warnings": risky}), 403
    else:
        instance_id = str(preview_instance_id) if preview_instance_id else uuid.uuid4().hex[:12]
        # Plain catalog installs used to skip this scan entirely, on the
        # assumption every catalog entry was project-authored and already
        # vetted. That assumption stopped holding the moment the App Store
        # catalog started including community-contributed apps
        # (privileged containers, host networking, Docker-socket mounts —
        # all real, all legitimate for what those specific apps do, but
        # none of it something to install silently without the admin
        # seeing it coming). Scanning here closes that gap for every
        # catalog entry uniformly, not just community-sourced ones — matches
        # exactly the warn-and-confirm shape the compose_override path
        # above already established, just applied one step earlier.
        tmpl_path = CATALOG_DIR / catalog_id / "docker-compose.yml.tmpl"
        try:
            rendered_for_scan = tmpl_path.read_text(encoding="utf-8") \
                .replace("{{INSTANCE_ID}}", instance_id).replace("{{DATA_DIR}}", str(APP_DATA_DIR / instance_id))
            risky = _scan_compose_risks(rendered_for_scan, APP_DATA_DIR / instance_id)
        except OSError:
            risky = []
        blockers = [p["text"] for p in _preflight(entry, APP_DATA_DIR / instance_id) if p["level"] == "block"]
        if blockers:
            return jsonify({"error": "Can't install on this machine: " + " ".join(blockers)}), 400
        if risky and not d.get("confirm_risky"):
            return jsonify({"error": "risky_yaml", "warnings": risky}), 409
        if risky and user.get("role") != "superuser":
            # A container with the Docker socket, a privileged container or a host mount is root on this
            # machine, so an Admin must not be able to approve one on their own.
            return jsonify({"error": "Only the Super User can approve an app that asks for elevated host access.",
                            "warnings": risky}), 403

    in_flight = sum(1 for j in INSTALL_JOBS.values() if j.get("user_id") == user["id"] and not j.get("done"))
    if in_flight >= MAX_INSTALL_JOBS_PER_USER:
        return jsonify({"error": f"You already have {in_flight} install(s)/update(s) in progress — wait for one to finish"}), 429
    _job_prune()
    job_id = uuid.uuid4().hex
    INSTALL_JOBS[job_id] = {"log": [], "done": False, "error": None, "user_id": user["id"],
                             "catalog_id": catalog_id, "created": time.time(), "kind": "install",
                             "label": next((c.get("name") for c in _load_catalog() if c.get("id") == catalog_id), catalog_id),
                             "tile_id": None, "host_port": None, "secrets_generated": None}
    _cat_name = next((c.get("name") for c in _load_catalog() if c.get("id") == catalog_id), catalog_id)
    threading.Thread(target=_report_app_job(_install_worker, job_id, user, _cat_name, "install"),
                      args=(job_id, catalog_id, instance_id, user, compose_override, preview_secrets, chosen),
                      daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/appstore/update/<tile_id>", methods=["POST"])
@require_docker_access
def api_appstore_update(tile_id):
    """Part C: updates an already-installed app to the latest image
    tag(s) — never regenerates secrets or touches data paths, only pulls
    and recreates the containers whose image actually changed. Shares
    INSTALL_JOBS and the install status endpoint with the install path
    (kind: "update"), including the same per-user concurrency cap, so
    update jobs can't bypass the ceiling the install path already has."""
    user = _current_user()
    stacks = _st_load()
    if not any(s["tile_id"] == tile_id for s in stacks):
        return jsonify({"error": "Not found"}), 404
    in_flight = sum(1 for j in INSTALL_JOBS.values() if j.get("user_id") == user["id"] and not j.get("done"))
    if in_flight >= MAX_INSTALL_JOBS_PER_USER:
        return jsonify({"error": f"You already have {in_flight} install(s)/update(s) in progress — wait for one to finish"}), 429
    _job_prune()
    job_id = uuid.uuid4().hex
    INSTALL_JOBS[job_id] = {"log": [], "done": False, "error": None, "user_id": user["id"],
                             "catalog_id": None, "created": time.time(), "kind": "update",
                             "label": next((a.get("name") for a in _aload() if a.get("id") == tile_id), "the app"),
                             "tile_id": tile_id, "host_port": None, "secrets_generated": None}
    _tile_name = next((a.get("name") for a in _aload() if a.get("id") == tile_id), "the app")
    threading.Thread(target=_report_app_job(_update_worker, job_id, user, _tile_name, "update"),
                     args=(job_id, tile_id, user), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/appstore/updates")
@require_docker_access
def api_appstore_updates_check():
    """'Update available' check (Part C) — lazy and on-demand only, never
    a recurring background poll: called once when the App Store/Containers
    tab is opened, or via a manual 'Check for updates' click. Reuses
    _resolve_latest_tag's own 10-minute cache, so this is cheap even
    called repeatedly — resolving the same handful of images across
    several installed apps within the cache window costs zero extra
    registry calls beyond the first."""
    stacks = _st_load()
    out = []
    for s in stacks:
        compose_path = Path(s["compose_path"])
        if not compose_path.exists():
            continue
        try:
            current_text = compose_path.read_text(encoding="utf-8")
        except Exception:
            continue
        latest_tags = {}
        for m in _IMAGE_LINE_RE.finditer(current_text):
            ref = m.group("ref")
            last_segment = ref.rsplit("/", 1)[-1]
            if ":" not in last_segment or "@" in ref:
                continue
            repo, pinned_tag = ref.rsplit(":", 1)
            resolved_tag, ok = _resolve_latest_tag(repo, pinned_tag)
            if ok:
                latest_tags[repo.rsplit("/", 1)[-1]] = resolved_tag
        current_tags = s.get("image_tags") or {}
        update_available = any(latest_tags.get(svc) and latest_tags[svc] != tag
                                for svc, tag in current_tags.items())
        out.append({"tile_id": s["tile_id"], "update_available": update_available,
                     "current_tags": current_tags, "latest_tags": latest_tags})
    return jsonify(out)

@app.route("/api/appstore/install/<job_id>")
@require_docker_access
def api_appstore_install_status(job_id):
    job = INSTALL_JOBS.get(job_id)
    if not job or job.get("user_id") != _current_user()["id"]:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"done": job["done"], "error": job["error"], "log": job["log"][-80:],
                     "kind": job.get("kind", "install"), "progress": job.get("progress"),
                     "stage": job.get("stage") or "", "label": job.get("label", ""),
                     "tile_id": job["tile_id"], "host_port": job["host_port"],
                     "secrets_generated": job["secrets_generated"] if job["done"] else None})

@app.route("/api/appstore/install/<job_id>/ack", methods=["POST"])
@require_docker_access
def api_appstore_install_ack(job_id):
    """The page has shown the generated credentials: forget them (they're only ever
    shown once, and only kept in memory until then)."""
    job = INSTALL_JOBS.get(job_id)
    if job and job.get("user_id") == _current_user()["id"]:
        job["secrets_generated"] = None
    return jsonify({"ok": True})

@app.route("/api/appstore/uninstall/<tile_id>", methods=["POST"])
@require_docker_access
def api_appstore_uninstall(tile_id):
    """docker compose down by default. -v (also delete data — e.g. every
    Immich photo, the whole Vaultwarden vault) is only ever added when the
    caller explicitly sets remove_data — never bundled into the default path."""
    user = _current_user()
    remove_data = bool((request.get_json(silent=True) or {}).get("remove_data"))
    with _stacks_lock:
        stacks = _st_load()
        stack = next((s for s in stacks if s["tile_id"] == tile_id), None)
        if not stack:
            return jsonify({"error": "This tile isn't an App Store install"}), 404
        _st_save([s for s in stacks if s["tile_id"] != tile_id])

    cmd = ["docker", "compose", "-f", stack["compose_path"], "-p", stack_project(stack), "down"]
    if remove_data:
        cmd.append("-v")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace", **winplat.no_window())
        down_ok = result.returncode == 0
    except Exception as e:
        down_ok = False
        result = None

    _stop_app_tunnel(tile_id)
    with _apps_lock:
        _asave([a for a in _aload() if a.get("id") != tile_id])

    if remove_data:
        try:
            shutil.rmtree(APP_DATA_DIR / stack["instance_id"], ignore_errors=True)
        except Exception:
            pass

    _audit("appstore_uninstall", user, target=stack["catalog_id"],
           detail=f"instance {stack['instance_id']}, remove_data={remove_data}, compose_down_ok={down_ok}")
    return jsonify({"ok": True, "compose_down_ok": down_ok, "data_removed": remove_data})


def _bind_later_modules():
    """Names this module uses from modules that load AFTER it. They are only
    needed inside functions, at call time, so voidos.py calls this once every
    module has loaded (importing them at load time would be circular)."""
    global _stop_app_tunnel
    from modules.tunnels import _stop_app_tunnel
