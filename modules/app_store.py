"""
modules/app_store.py — App Store engine: image tags, compose safety scan, ARM tweaks, install worker.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from pathlib import Path
import json
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import yaml
from modules import catalog_pack, diagnose, errors, state, winplat
from modules.config import (
    APP_DATA_DIR, BIND_MOUNT_MODE, CATALOG_DIR, STACKS_FILE, _PROTECTED_HOST_PATHS,
)
from modules.file_helpers import _aload, _asave
from modules.web import _apps_lock, _atomic_write_json, _stacks_lock
from modules.activity import _audit, _inapp_notify, _inapp_update
from modules.error_log import _log_exception


# ══════════════════════════════════════════════════════════════════════
# APP STORE CATALOG — a small, bundled, read-only catalog under
# app_catalog/<id>/ (manifest.json + docker-compose.yml.tmpl per entry).
# Every entry is authored and pinned by this project itself — this is NOT
# a marketplace, there is no third-party or community content path, and
# nothing here ever fetches, syncs, or depends on any external catalog or
# service at runtime. Same install/uninstall permission gate as the
# Containers tab (_can_use_docker via @require_docker_access) — deploying
# a stack is exactly the kind of Docker capability that flag exists for.
#
# Every container's UI port binds to "127.0.0.1::<port>" in the shipped
# templates — loopback-only, host-port left for Docker to assign — which
# sidesteps two problems at once: no manual port-conflict handling is
# needed, and nothing an install creates is accidentally exposed on the
# LAN by default (matching the exact gotcha App Tunnels already had to
# document: a port not published to host loopback is unreachable to
# cloudflared, which runs on the host, not inside the Docker network).
# The actual assigned host port is detected after the fact via the Docker
# SDK, never assumed from the manifest's default_port.
# ══════════════════════════════════════════════════════════════════════
INSTALL_JOBS = {}  # job_id -> {log, done, error, user_id, catalog_id, tile_id, host_port, secrets_generated, created, kind}
MAX_INSTALL_JOBS_PER_USER = 2

# ── Job progress ───────────────────────────────────────────────────────
# Every install/update job carries `progress` (0-100, or None while we can't tell yet
# -- the page shows that as an animated "working" bar) and `stage` (what it's doing).
# The same numbers feed the live notification in the bell, so the user can close the
# install window and still watch it advance (and the page puts it in the tab title).
_RE_PULLING = re.compile(r"(?i)\bimage\s+(\S+)\s+pulling\b")
_RE_PULLED = re.compile(r"(?i)(?:\bimage\s+(\S+)\s+(?:pulled|exists)\b|^\W*(\S+)\s+pulled\s*$)")
_RE_CONTAINER = re.compile(r"(?i)\bcontainer\s+(\S+)\s+(?:started|healthy|running)\b")
_RE_LAYER = re.compile(r"(?i)^\W*([0-9a-f]{6,64})\s+(?:downloading|extracting)\b.*?"
                       r"(\d+(?:\.\d+)?)\s*([kKMGT]?i?B)\s*/\s*(\d+(?:\.\d+)?)\s*([kKMGT]?i?B)")
_UNIT = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def _job_notify(job: dict, force: bool = False) -> None:
    """Push the job's current numbers into its bell notification (at most about once a second)."""
    nid = job.get("notif_id")
    if not nid:
        return
    now = time.time()
    if not force and now - job.get("_nt", 0) < 1.0:
        return
    job["_nt"] = now
    pct = job.get("progress")
    verb = "Updating" if job.get("kind") == "update" else "Installing"
    body = job.get("stage") or ""
    if pct is not None and not job.get("done"):
        body = f"{body} \u2014 {int(pct)}%" if body else f"{int(pct)}%"
    _inapp_update(job["user_id"], nid, title=f"{verb} {job.get('label', 'app')}\u2026", body=body,
                  data={"ongoing": True, "progress": pct, "stage": job.get("stage") or ""})


def _job_set(job: dict, progress="keep", stage: str | None = None) -> None:
    """Record progress (never goes backwards) and/or a new stage description."""
    changed = False
    if progress != "keep" and progress is not None:
        p = max(float(job.get("progress") or 0), min(99.0, float(progress)))
        if int(p) != int(job.get("progress") or -1):
            changed = True
        job["progress"] = p
    if stage is not None and stage != job.get("stage"):
        job["stage"] = stage
        changed = True
    if changed:
        _job_notify(job)


def _job_begin(job: dict, job_id: str, user: dict, label: str, kind: str) -> None:
    job.update(id=job_id, label=label, kind=kind, progress=None, stage="Starting\u2026", _nt=0.0)
    verb = "Updating" if kind == "update" else "Installing"
    item = _inapp_notify(user["id"], f"{verb} {label}\u2026", "Starting\u2026", level="info", kind="apps",
                         link="appstore", read=True,
                         data={"ongoing": True, "job_id": job_id, "kind": kind, "catalog_id": job.get("catalog_id"),
                               "progress": None, "stage": "Starting\u2026", "label": label})
    job["notif_id"] = item["id"]


def _job_finish(job: dict) -> None:
    """Turn the live notification into its final form (and mark it unread, so it counts)."""
    job["finished"] = time.time()
    nid = job.get("notif_id")
    if not nid:
        return
    label, kind = job.get("label", "the app"), job.get("kind", "install")
    if job.get("error"):
        _inapp_update(job["user_id"], nid, level="error", read=False, link="appstore",
                      title=f"Couldn't {'update' if kind == 'update' else 'install'} {label}",
                      body=str(job["error"])[:500], data={"ongoing": False, "progress": job.get("progress")})
    else:
        job["progress"] = 100
        _inapp_update(job["user_id"], nid, level="success", read=False,
                      link="dashboard" if kind != "update" else "appstore",
                      title=f"{label} updated" if kind == "update" else f"{label} installed",
                      body="Running the latest version now." if kind == "update" else "It's on your home screen.",
                      data={"ongoing": False, "progress": 100, "stage": "Done"})


def _job_public(job_id: str, job: dict) -> dict:
    return {"id": job_id, "kind": job.get("kind", "install"), "catalog_id": job.get("catalog_id"),
            "tile_id": job.get("tile_id"), "label": job.get("label", ""), "progress": job.get("progress"),
            "stage": job.get("stage") or "", "done": bool(job.get("done")),
            "error": str(job["error"])[:500] if job.get("error") else None,
            "has_secrets": bool(job.get("secrets_generated")), "created": job.get("created")}


def _job_prune(max_age: int = 1800) -> None:
    now = time.time()
    for jid in [j for j, v in INSTALL_JOBS.items() if v.get("done") and now - (v.get("finished") or now) > max_age]:
        INSTALL_JOBS.pop(jid, None)


class _ComposeProgress:
    """Reads `docker compose` output line by line and turns it into a percentage
    between `lo` and `hi`: image downloads (most of the time) then container starts.
    Docker prints different things in different versions, so everything is
    best-effort: if nothing recognisable shows up the job simply stays in the
    "working" state instead of showing a made-up number."""

    def __init__(self, job: dict, compose_text: str, lo: float, hi: float, pulling: bool = True):
        self.job, self.lo, self.hi, self.pulling = job, lo, hi, pulling
        self.n_images = self.n_services = 0
        try:
            svcs = (yaml.safe_load(compose_text) or {}).get("services") or {}
            self.n_services = len(svcs)
            self.n_images = len({str(s.get("image")) for s in svcs.values() if isinstance(s, dict) and s.get("image")})
        except Exception:
            pass
        self.pulled, self.pulling_seen, self.started = set(), set(), set()
        self.layers: dict = {}

    def _layer_fraction(self) -> float:
        done = sum(d for d, t in self.layers.values())
        total = sum(t for d, t in self.layers.values())
        return min(1.0, done / total) if total else 0.0

    def feed(self, line: str) -> None:
        m = _RE_LAYER.match(line)
        if m:
            def num(v, u): return float(v) * _UNIT.get(u.upper(), 1)
            self.layers[m.group(1)] = (num(m.group(2), m.group(3)), max(1.0, num(m.group(4), m.group(5))))
        m = _RE_PULLING.search(line)
        if m:
            self.pulling_seen.add(m.group(1))
        m = _RE_PULLED.search(line)
        if m:
            self.pulled.add(m.group(1) or m.group(2))
        m = _RE_CONTAINER.search(line)
        if m:
            self.started.add(m.group(1))
        self._update()

    def _update(self) -> None:
        if not self.n_images:
            return
        span = self.hi - self.lo
        ctr = min(1.0, len(self.started) / self.n_services) if self.n_services else 0.0
        if self.pulling and (self.pulling_seen or self.pulled or self.layers):
            partial = self._layer_fraction() if len(self.pulled) < self.n_images else 0.0
            img = min(1.0, (len(self.pulled) + partial) / self.n_images)
            n = min(self.n_images, len(self.pulled) + 1)
            stage = f"Downloading images ({n} of {self.n_images})" if len(self.pulled) < self.n_images else "Downloading images"
        else:
            img = 1.0 if (self.started or self.pulled) else 0.0
            stage = "Starting containers" if self.started else ("Setting things up" if (self.pulled or self.pulling_seen) else None)
        if self.started:
            stage = "Starting containers"
        if img == 0.0 and not self.started and not (self.pulling_seen or self.pulled or self.layers):
            return                      # nothing recognisable yet: stay in the indeterminate state
        _job_set(self.job, self.lo + span * (0.8 * img + 0.2 * ctr), stage)

# ── Latest-image-tag resolution (App Store Part B) ─────────────────────
# One registry API call per image, cached briefly so opening the preview
# modal twice in a row (or checking several installed apps for updates
# back to back) doesn't re-hit the registry — see _resolve_latest_tag.
_TAG_CACHE = {}       # (image repo, pinned tag) -> (resolved_tag, resolved_at)
_TAG_CACHE_TTL = 600  # 10 minutes
_TAG_CACHE_LOCK = threading.Lock()
# A plain release tag: optional "v", 2-4 dotted numbers, optional "-suffix" (alpine, ls123, ...).
_TAG_PARSE_RE = re.compile(r"^(?P<v>v?)(?P<nums>\d+(?:\.\d+){1,3})(?P<suf>-[A-Za-z0-9][A-Za-z0-9._]*)?$")

def _parse_tag(tag: str):
    m = _TAG_PARSE_RE.match(tag or "")
    if not m:
        return None
    return m.group("v"), tuple(int(x) for x in m.group("nums").split(".")), m.group("suf") or ""

def _pick_best_version_tag(tags: list, pinned: str = "") -> str:
    """The newest tag that is a safe replacement for `pinned`, or `pinned` itself.

    Safe means: a plain release of the SAME kind and the same major version -- same "v" prefix,
    same suffix (a -alpine stays -alpine; a pre-release such as -rc1 is never picked because the
    pinned tag has no such suffix), same number of version parts, same first number -- and strictly
    newer. So an install picks up patch and minor releases but never a downgrade, a dev/nightly
    build, or a jump to a new major version that needs migration steps.

    This used to take the first version-looking tag in the registry's list. GitHub's registry
    lists tags alphabetically, not newest-first, so it picked something like "v1.36.0_55-dev" for
    Immich (a 2021 build, next to a current database) and the containers then crashed in a loop.
    A pinned tag that isn't a plain version ("14-vectorchord0.4.3", "latest") is left alone."""
    cur = _parse_tag(pinned)
    if cur is None:
        return pinned
    best, best_tag = cur[1], pinned
    for t in tags:
        c = _parse_tag(t)
        if c is None or c[0] != cur[0] or c[2] != cur[2] or len(c[1]) != len(cur[1]) or c[1][0] != cur[1][0]:
            continue
        if c[1] > best:
            best, best_tag = c[1], t
    return best_tag

def _resolve_latest_tag(repo: str, pinned_tag: str = "", timeout: float = 5) -> tuple:
    """Best-effort newest safe tag for a GHCR or Docker Hub image repo (no tag suffix --
    callers split that off first). Returns (tag, ok): ok=False means the lookup failed, timed out,
    or this isn't a registry this function knows how to query, and callers fall back to whatever
    tag was already pinned. When ok, `tag` is `pinned_tag` itself if nothing newer and safe exists
    (see _pick_best_version_tag), so a result is never older than what the template pins."""
    key = (repo, pinned_tag)
    with _TAG_CACHE_LOCK:
        cached = _TAG_CACHE.get(key)
        if cached and time.time() - cached[1] < _TAG_CACHE_TTL:
            return cached[0], True
    parsed = _parse_tag(pinned_tag)
    if parsed is None:
        return pinned_tag, True  # not a plain release number: nothing to compare, keep it exactly
    prefix = f"{parsed[0]}{parsed[1][0]}."  # same "v" and major version, e.g. "v2."
    try:
        if repo.startswith("ghcr.io/"):
            # GitHub's registry can't be asked for "the newest release": its tag list is not in a
            # useful order (Immich alone has tens of thousands of commit-* and pr-* tags) and
            # cannot be searched by prefix. The catalog pins a tested version instead, and a newer
            # catalog (Void OS update) moves it forward. Better to keep the pin than to guess.
            return "", False
        elif "." not in repo.split("/")[0] or repo.startswith("docker.io/"):
            # No dot in the first path segment means it's not a custom-registry
            # hostname (registry hostnames always contain a dot, e.g. ghcr.io,
            # quay.io) -- so this is a plain Docker Hub reference. A bare name
            # with no "/" (e.g. "redis") is Docker Hub's "library/" namespace
            # for official images.
            ns_repo = repo[len("docker.io/"):] if repo.startswith("docker.io/") else repo
            if "/" not in ns_repo:
                ns_repo = f"library/{ns_repo}"
            url = (f"https://hub.docker.com/v2/repositories/{ns_repo}/tags?page_size=100&ordering=last_updated"
                   f"&name={urllib.parse.quote(prefix)}")
            tags = [t["name"] for t in json.loads(
                urllib.request.urlopen(url, timeout=timeout).read().decode()).get("results", [])]
        else:
            return "", False  # some other custom registry -- not supported by this best-effort lookup
    except Exception:
        return "", False
    tag = _pick_best_version_tag(tags, pinned_tag)
    with _TAG_CACHE_LOCK:
        _TAG_CACHE[key] = (tag, time.time())
    return tag, True

_IMAGE_LINE_RE = re.compile(r'^(?P<indent>[ \t]*)image:\s*["\']?(?P<ref>[^\s"\'#]+)["\']?\s*$', re.MULTILINE)

def _substitute_latest_tags(compose_text: str) -> tuple:
    """Finds every `image:` line, resolves the latest version tag for that
    image (Part B), and swaps just the tag portion in place — everything
    else in the file, including any secrets already baked into env values,
    is left completely untouched. This is what both the install path (on
    a freshly rendered template) and the update path (on the existing
    on-disk compose file) share, and is exactly why update is safe to
    implement as "run this on the existing file" rather than re-rendering
    the template from scratch and risking secret regeneration.
    Returns (possibly-modified text, human-readable notes for the job log —
    e.g. "immich-server: using v2.8.1 (latest, was v2.7.5 in the template)"
    or "...v2.7.5 (pinned fallback, registry lookup failed)")."""
    notes = []
    out = compose_text
    for m in _IMAGE_LINE_RE.finditer(compose_text):
        ref = m.group("ref")
        last_segment = ref.rsplit("/", 1)[-1]
        if ":" not in last_segment or "@" in ref:
            continue  # no explicit tag, or pinned by digest on purpose — nothing to compare a "latest" against
        repo, pinned_tag = ref.rsplit(":", 1)
        label = repo.rsplit("/", 1)[-1]
        resolved_tag, ok = _resolve_latest_tag(repo, pinned_tag)
        if not ok:
            notes.append(f"{label}: {pinned_tag} (kept the pinned version)")
        elif resolved_tag == pinned_tag:
            notes.append(f"{label}: {pinned_tag} (already latest)")
        else:
            out = out.replace(f"{repo}:{pinned_tag}", f"{repo}:{resolved_tag}")
            notes.append(f"{label}: using {resolved_tag} (latest, was {pinned_tag} in the template)")
    return out, notes

def _extract_image_tags(compose_text: str) -> dict:
    """Parses the final compose text into {service_name: tag} — the
    app_stacks.json data-model addition from Part 5, letting the
    'Update available' check compare against installed reality instead of
    re-parsing the compose file on disk every time."""
    try:
        doc = yaml.safe_load(compose_text) or {}
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            return {}
        out = {}
        for name, svc in services.items():
            if isinstance(svc, dict) and isinstance(svc.get("image"), str):
                ref = svc["image"]
                last_segment = ref.rsplit("/", 1)[-1]
                out[name] = ref.rsplit(":", 1)[1] if ":" in last_segment else "latest"
        return out
    except Exception:
        return {}

def _scan_compose_risks(yaml_text: str, data_dir: Path) -> list:
    """Best-effort static check for compose directives that are an
    effective path to host root — run only against the free-form
    compose-override path (see require_docker_and_terminal note on the
    install route), never a hard gate: a legitimate template might
    reasonably need one of these someday, so this is a warn-and-confirm
    speed bump for an admin who already has both docker and terminal
    access, not a boundary this app tries to enforce on its own. A
    determined admin with terminal access could achieve any of this
    directly through the terminal anyway — the point is just to make sure
    they see it coming, not to prevent it.

    Flexible Host Bind Mount Support (FR): arbitrary absolute host bind
    mounts (external drives, /etc/localtime, NAS paths — anything a stock
    upstream docker-compose.yml for Immich/Jellyfin/Nextcloud/Frigate/etc
    typically wants) are NOT flagged here by default — Docker already only
    grants a container the host paths it's explicitly given, so this isn't
    a privilege escalation, just normal Compose usage. Only two things
    still surface as a warning:
      - a bind source that IS (or is a parent of) a genuinely dangerous
        system path (_PROTECTED_HOST_PATHS) — mounting host root, /proc,
        /sys, /dev, /boot, or /run in isn't something a template should do
        by accident.
      - if VOID_BIND_MOUNT_MODE=restricted, ANY mount outside the app's own
        data folder, same as the old default behavior, for admins who
        deliberately want the stricter posture.
    Also flagged, unconditionally: unparsable bind syntax and duplicate
    container-side mount targets within one service (the second one would
    silently shadow the first)."""
    found = []
    try:
        doc = yaml.safe_load(yaml_text) or {}
    except Exception:
        return ["Could not parse this as YAML — review it carefully before proceeding."]
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return found
    try:
        data_dir_resolved = data_dir.resolve()
    except Exception:
        data_dir_resolved = data_dir
    protected_resolved = {Path(p).resolve() for p in _PROTECTED_HOST_PATHS}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if svc.get("privileged"):
            found.append(f"{name}: privileged: true — full, unconfined access to the host")
        if svc.get("cap_add"):
            found.append(f"{name}: cap_add: {svc['cap_add']} — elevated kernel capabilities beyond Docker's defaults")
        if svc.get("network_mode") == "host":
            found.append(f"{name}: network_mode: host — shares the host's network stack directly")
        if svc.get("pid") == "host":
            found.append(f"{name}: pid: host — shares the host's process namespace")
        for ns in ("ipc", "uts", "userns_mode", "cgroup"):
            if svc.get(ns) == "host":
                found.append(f"{name}: {ns}: host — shares the host's {ns} namespace")
        for opt in (svc.get("security_opt") or []):
            if "unconfined" in str(opt).lower():
                found.append(f"{name}: security_opt: {opt} — turns off Docker's default sandboxing")
        if svc.get("devices"):
            found.append(f"{name}: devices: {svc['devices']} — gives the container direct access to host hardware")
        seen_targets = set()
        for vol in (svc.get("volumes") or []):
            src = tgt = None
            if isinstance(vol, str):
                # A Windows host path carries its own drive-letter colon
                # ("C:/data:/config" or "C:\\data:/config:ro") -- split it off
                # first, or every Windows bind mount reads as unparseable.
                _m = re.match(r"^([A-Za-z]:[\\/][^:]*)(?::(.*))?$", vol)
                parts = ([_m.group(1)] + (_m.group(2).split(":") if _m.group(2) else [])) if _m else vol.split(":")
                looks_like_path = len(parts) >= 2 and (
                    parts[0].startswith("/") or parts[0].startswith(".")
                    or (len(parts[0]) >= 2 and parts[0][1] == ":"))  # Windows drive letters, e.g. "C:\..."
                if looks_like_path:
                    src = parts[0]
                    tgt = parts[1] if len(parts) > 1 else None
                elif len(parts) == 1 and parts[0]:
                    pass  # a bare named-volume reference, e.g. "pgdata" — nothing to validate
                elif len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[0]):
                    # "pgdata:/var/lib/postgresql/data": a Docker-managed named volume mounted at a path.
                    # It has no host path, so there is nothing to check; just keep the duplicate-target rule.
                    if parts[1] in seen_targets:
                        found.append(f"{name}: duplicate mount target '{parts[1]}' — a later entry will shadow the earlier one")
                    seen_targets.add(parts[1])
                    continue
                else:
                    found.append(f"{name}: could not parse volume entry '{vol}' — check the bind mount syntax")
                    continue
            elif isinstance(vol, dict) and vol.get("type") == "bind":
                src = vol.get("source")
                tgt = vol.get("target")
            if not src:
                continue
            if tgt:
                if tgt in seen_targets:
                    found.append(f"{name}: duplicate mount target '{tgt}' — a later entry will shadow the earlier one")
                seen_targets.add(tgt)
            try:
                resolved = Path(src).expanduser().resolve()
            except Exception:
                continue
            if resolved.name == "docker.sock" or "docker_engine" in str(src):
                found.append(f"{name}: bind mount source '{src}' is the Docker socket — full control of the host through Docker")
                continue
            # The path itself, or anything inside a protected folder (/proc, /sys, /dev,
            # /run - which is where /var/run/docker.sock really lives - /boot, ...).
            # A filesystem root ("/" or "C:\\") is only flagged when it is mounted exactly.
            hit = next((p for p in protected_resolved
                        if resolved == p or (p != p.parent and p in resolved.parents)), None)
            if hit is not None:
                found.append(f"{name}: bind mount source '{src}' targets a protected system path — this can affect host stability")
                continue
            if BIND_MOUNT_MODE == "restricted":
                try:
                    resolved.relative_to(data_dir_resolved)
                except ValueError:
                    found.append(f"{name}: bind mount source '{src}' resolves outside this app's own data folder")
    return found

def _is_arm_host() -> bool:
    """True on 32/64-bit ARM (Raspberry Pi and similar). Used for the
    Immich Raspberry Pi Compatibility FR — platform.machine() reports the
    kernel's reported architecture, e.g. 'aarch64'/'armv7l' on Pi OS,
    'x86_64'/'AMD64' elsewhere."""
    m = platform.machine().lower()
    return m.startswith("arm") or m.startswith("aarch")

# ARM Compatibility Profiles (Immich FR's "Future Improvements" section,
# generalized): a small per-catalog-id table of known ARM/Raspberry Pi
# quirks, applied automatically on install for whichever apps are actually
# in the catalog today. Two kinds of entry:
#   - "env": a dict of environment variables to inject into a specific
#     service, the same way Immich's VIPS_NOVECTOR=1 fix works — safe to
#     apply unattended since it's just a runtime env var, never overrides a
#     value the operator already set themselves.
#   - "note": informational only, surfaced in the install log / preview,
#     for compatibility concerns that need a *decision* from the operator
#     (which device to pass through, whether they even have the hardware)
#     rather than something safe to silently auto-apply. Home Assistant,
#     Jellyfin, and Frigate all fall in this bucket: unlike Immich's
#     libvips flag, "add this device/privilege" isn't safe to guess at
#     unattended — passing through a device that isn't there, or handing
#     out capabilities nobody asked for, is worse than doing nothing.
# Jellyfin and Frigate aren't in this catalog yet (see app_catalog/) — kept
# here so the profile is already in place the day either one is added.
ARM_COMPAT_PROFILES = {
    "immich": {
        "service": "immich-server",
        "env": {"VIPS_NOVECTOR": "1"},
        "reason": "known libvips vectorized-ops compatibility workaround for Raspberry Pi/ARM",
        # Keep the machine-learning container small on a board: one worker, few request threads,
        # models unloaded after 2 minutes idle (each worker holds its own copy of the models).
        "extra_env": {"immich-machine-learning": {"MACHINE_LEARNING_WORKERS": "1",
                                                  "MACHINE_LEARNING_REQUEST_THREADS": "2",
                                                  "MACHINE_LEARNING_MODEL_TTL": "120"}},
    },
    "homeassistant": {
        "service": "homeassistant",
        "note": ("If you're using a Zigbee/Z-Wave USB adapter, its device node needs to be "
                  "passed through manually via the compose editor (Docker + Terminal access), "
                  "e.g. `devices: - /dev/ttyUSB0:/dev/ttyUSB0` — Void OS doesn't auto-detect "
                  "connected USB hardware, so this isn't applied automatically."),
    },
    "jellyfin": {
        "service": "jellyfin",
        "note": ("Jellyfin has no broadly-supported hardware transcoding path on ARM the way "
                  "Intel/Nvidia GPUs provide on x86 — expect software transcoding unless your "
                  "Pi's VideoCore/V4L2 stack is set up for it beforehand. That setup is "
                  "hardware- and OS-image-specific, so Void OS surfaces this as a heads-up "
                  "rather than editing the compose file."),
    },
    "frigate": {
        "service": "frigate",
        "note": ("Frigate on Raspberry Pi benefits heavily from a Coral USB/PCIe TPU for "
                  "object detection — without one, expect high CPU load. If you have a Coral "
                  "attached, its device (USB) or PCIe path needs to be passed through manually "
                  "via the compose editor; Void OS doesn't auto-detect attached accelerators."),
    },
}

def _apply_arm_compat_profile(catalog_id: str, rendered: str) -> tuple:
    """Applies ARM_COMPAT_PROFILES[catalog_id] if there is one and this is
    an ARM host. Returns (possibly-modified compose text, list of log
    notes — empty if nothing applied). Env injection uses a full YAML
    round-trip rather than text substitution since we're adding a value,
    not swapping one out for another already-marked location the way
    _substitute_latest_tags does for image tags; note-only profiles never
    touch the compose text at all."""
    if not _is_arm_host():
        return rendered, []
    profile = ARM_COMPAT_PROFILES.get(catalog_id)
    if not profile:
        return rendered, []
    notes = []
    env_to_add = profile.get("env")
    if env_to_add:
        try:
            doc = yaml.safe_load(rendered)
            services = doc.get("services") if isinstance(doc, dict) else None
            svc = services.get(profile["service"]) if isinstance(services, dict) else None
            if isinstance(svc, dict):
                env = svc.get("environment")
                added_keys = []
                if isinstance(env, list):
                    existing = {str(e).split("=", 1)[0].strip() for e in env}
                    for k, v in env_to_add.items():
                        if k not in existing:
                            env.append(f"{k}={v}"); added_keys.append(k)
                    svc["environment"] = env
                elif isinstance(env, dict):
                    for k, v in env_to_add.items():
                        if k not in env:
                            env[k] = v; added_keys.append(k)
                    svc["environment"] = env
                else:
                    svc["environment"] = [f"{k}={v}" for k, v in env_to_add.items()]
                    added_keys = list(env_to_add)
                if added_keys:
                    doc["services"] = services
                    rendered = yaml.safe_dump(doc, sort_keys=False)
                    notes.append(f"ARM host detected — added {', '.join(added_keys)} to "
                                 f"{profile['service']} ({profile.get('reason', 'ARM compatibility workaround')}).")
        except Exception:
            pass
    elif profile.get("note"):
        notes.append(f"ARM host detected — {profile['note']}")
    extra = profile.get("extra_env")
    if extra:
        try:
            doc = yaml.safe_load(rendered)
            services = doc.get("services") if isinstance(doc, dict) else None
            added = []
            if isinstance(services, dict):
                for svc_name, items in extra.items():
                    added += [f"{svc_name}: {k}" for k in _env_add(services, svc_name, items)]
            if added:
                rendered = yaml.safe_dump(doc, sort_keys=False, width=1000)
                notes.append("ARM host detected — memory-friendly settings added (" + ", ".join(added) + ").")
        except Exception:
            pass
    return rendered, notes


# ══════════════════════════════════════════════════════════════════════
# Render-time fixes, install options and a pre-install check.
# ══════════════════════════════════════════════════════════════════════
# Many catalog entries wrote environment values as `- KEY="value"` list items.
# Docker Compose keeps those quotes as part of the value (PUID becomes `"1000"`, a password
# `"postgres"`), unlike a YAML mapping where they are consumed. Strip them when a compose file is
# rendered; the result is a single-quoted YAML scalar so any value stays valid.
_ENV_QUOTED_RE = re.compile(
    r"""^(?P<ind>[ \t]*-[ \t]+)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)=(?P<q>["'])(?P<val>.*)(?P=q)[ \t]*$""", re.M)

def _normalize_env_quotes(text: str) -> tuple:
    """-> (text, how many entries were fixed)."""
    n = 0
    def sub(m):
        nonlocal n
        val = m.group("val")
        if m.group("q") in val:
            return m.group(0)  # an embedded quote of the same kind: not a simple wrapper, leave it
        n += 1
        return f"{m.group('ind')}'{m.group('key')}={val.replace(chr(39), chr(39) * 2)}'"
    return _ENV_QUOTED_RE.sub(sub, text), n


def _option_states(entry: dict, chosen, host: dict) -> list:
    """The entry's install options with whether each is on. An option the host is too small for
    starts switched off (`recommend_off_below_mb`); the admin's own choice always wins."""
    out = []
    ram = host.get("ram_mb")
    for o in entry.get("options") or []:
        oid = str(o.get("id", ""))
        if not oid:
            continue
        low = o.get("recommend_off_below_mb")
        rec_off = bool(low and ram and ram < low)
        default = bool(o.get("default", True)) and not rec_off
        enabled = bool(chosen[oid]) if isinstance(chosen, dict) and oid in chosen else default
        out.append({**o, "id": oid, "enabled": enabled, "recommended_off": rec_off})
    return out


def _env_add(doc_services: dict, svc_name: str, items: dict) -> list:
    """Adds missing env vars to one service of a parsed compose file. -> keys added."""
    svc = doc_services.get(svc_name)
    if not isinstance(svc, dict):
        return []
    env = svc.get("environment")
    added = []
    if isinstance(env, dict):
        for k, v in items.items():
            if k not in env:
                env[k] = v; added.append(k)
    else:
        env = list(env) if isinstance(env, list) else []
        have = {str(e).split("=", 1)[0].strip() for e in env}
        for k, v in items.items():
            if k not in have:
                env.append(f"{k}={v}"); added.append(k)
        svc["environment"] = env
    return added


def _apply_options(entry: dict, rendered: str, chosen) -> tuple:
    """Applies the `when_off` of every option that is switched off: removes services and adds env
    values. -> (compose text, log notes). Unchanged text when everything is on."""
    off = [o for o in _option_states(entry, chosen, diagnose.host_profile())
           if not o["enabled"] and isinstance(o.get("when_off"), dict)]
    if not off:
        return rendered, []
    notes = []
    try:
        doc = yaml.safe_load(rendered)
        services = doc["services"]
        removed = set()
        for o in off:
            w = o["when_off"]
            for name in w.get("remove_services", []):
                if services.pop(name, None) is not None:
                    removed.add(name)
            for name, items in (w.get("env") or {}).items():
                _env_add(services, name, items)
            notes.append(f"Installing without: {o.get('label', o['id'])}.")
        for svc in services.values():  # nothing may wait for a service that is gone
            dep = svc.get("depends_on") if isinstance(svc, dict) else None
            if isinstance(dep, list):
                svc["depends_on"] = [d for d in dep if d not in removed]
            elif isinstance(dep, dict):
                for d in removed:
                    dep.pop(d, None)
            if isinstance(svc, dict) and svc.get("depends_on") in ([], {}):
                svc.pop("depends_on")
        return yaml.safe_dump(doc, sort_keys=False, width=1000), notes
    except Exception as e:
        return rendered, [f"Could not apply install options ({errors.describe(e)}); installed with defaults."]


_BAD_DB_FS = {"vfat", "exfat", "msdos", "ntfs", "ntfs3", "fuseblk", "cifs", "smb3", "smbfs", "nfs", "nfs4", "9p", "sshfs"}

def _fs_type(path) -> str:
    """Filesystem type of the drive `path` is on (longest matching mount point), or ''."""
    try:
        import psutil
        best, kind = "", ""
        p = str(Path(path).resolve())
        for part in psutil.disk_partitions(all=True):
            mp = part.mountpoint
            if p == mp or p.startswith(mp.rstrip("/\\") + ("/" if "/" in mp else "\\")):
                if len(mp) > len(best):
                    best, kind = mp, (part.fstype or "").lower()
        return kind
    except Exception:
        return ""


def _preflight(entry: dict, data_dir: Path, options_on: dict | None = None) -> list:
    """Checks this machine against what the app needs before anything is installed.
    -> [{"level": "block"|"warn"|"info", "text": str}]. "block" means it cannot work here."""
    out = []
    h = diagnose.host_profile()
    arch, bits = h.get("docker_arch"), h.get("os_bits")
    plats = entry.get("platforms")
    if plats and arch and arch not in plats:
        out.append({"level": "block", "text":
                    f"{entry.get('name', 'This app')} only publishes images for {' and '.join(plats)}, but Docker "
                    f"here runs {arch}{f' ({bits}-bit)' if bits else ''}. "
                    + ("On a Raspberry Pi, install the 64-bit version of Raspberry Pi OS." if h.get("is_pi") or arch == "arm" else "")})
    ram, need = h.get("ram_mb"), entry.get("min_ram_mb")
    ml_off = options_on is not None and any(not o["enabled"] for o in options_on)
    if ram and need and ram < need * 0.85:
        out.append({"level": "warn", "text":
                    f"This machine has {ram / 1024:.1f} GB of RAM; {entry.get('name', 'the app')} wants about "
                    f"{need / 1024:.0f} GB. Expect slowness or containers being killed for memory."})
    elif ram and ram < 4096 and entry.get("options") and not ml_off:
        out.append({"level": "warn", "text": f"Only {ram / 1024:.1f} GB of RAM: consider switching off the optional features below."})
    if ram and ram < 4096 and (h.get("swap_mb") or 0) < 512:
        out.append({"level": "warn", "text": "There is almost no swap space. A memory spike will kill a container instead of slowing down."})
    ps = h.get("page_size")
    if entry.get("id") == "immich" and ps and ps > 4096:
        out.append({"level": "info", "text":
                    f"This kernel uses {ps // 1024} KB memory pages (Raspberry Pi 5 default). The database image "
                    "installed here supports that. If the database still crashes, use the 4 KB kernel: add "
                    "kernel=kernel8.img to /boot/firmware/config.txt and reboot."})
    try:
        base = data_dir if data_dir.exists() else data_dir.parent
        free_mb = shutil.disk_usage(base).free // 1048576
        need_disk = entry.get("min_disk_mb")
        if need_disk and free_mb < need_disk:
            out.append({"level": "warn", "text": f"Only {free_mb / 1024:.1f} GB free where app data is stored; this app wants about {need_disk / 1024:.0f} GB."})
        fs = _fs_type(base)
        if fs in _BAD_DB_FS:
            out.append({"level": "warn", "text":
                        f"App data is stored on a {fs} drive. Linux permissions don't work there, so databases and "
                        "some apps fail. Move VOID_APP_DATA_DIR to an ext4 disk."})
    except Exception:
        pass
    return out


def _find_service_container(project: str, service: str, fallback_name: str = ""):
    """The container of a compose service, found by the labels Compose puts on it. Matching on a
    guessed container_name broke for every app whose service name has a hyphen."""
    if not state.DOCKER_OK:
        return None
    try:
        found = state._docker.containers.list(all=True, filters={"label": [
            f"com.docker.compose.project={project}", f"com.docker.compose.service={service}"]})
        if found:
            return found[0]
    except Exception:
        pass
    if fallback_name:
        try:
            return state._docker.containers.get(fallback_name)
        except Exception:
            return None
    return None


def _watch_stack(project: str, services: list, window: int = 75):
    """Watches an app's containers right after `compose up`. -> the first container that is crash
    looping or has died, else None. Returns early once everything has been running cleanly for
    ~30 s, so a healthy install is not held up for the whole window."""
    deadline = time.time() + window
    started = time.time()
    while time.time() < deadline:
        all_ok, seen = True, 0
        for svc in services:
            c = _find_service_container(project, svc)
            if c is None:
                all_ok = False
                continue
            seen += 1
            try:
                c.reload()
                st = c.attrs.get("State") or {}
                restarts = int(c.attrs.get("RestartCount") or 0)
                if st.get("Restarting") or restarts >= 3 or st.get("OOMKilled") or \
                        (st.get("Status") == "exited" and st.get("ExitCode") not in (0, None)):
                    return c
                if st.get("Status") != "running" or restarts:
                    all_ok = False
            except Exception:
                all_ok = False
        if seen and all_ok and time.time() - started >= 30:
            return None
        time.sleep(3)
    return None


# ── Readable container and project names ─────────────────────────────
# Catalog templates name containers `void_{{INSTANCE_ID}}_<service>`, which is unique but ugly in
# `docker ps`. At render time they become the app's own name instead: `immich-server`,
# `immich-database`, `komga`. A second copy of the same app gets a "-2" on every name, and a name
# already used by any Docker container (Void OS's or not) is never taken.
_CN_RE = re.compile(r"^(?P<pre>[ \t]*container_name:[ \t]*)void_\{\{INSTANCE_ID\}\}_(?P<svc>\S+)[ \t]*$", re.M)

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "app"

def _taken_names() -> set:
    """Container and project names already in use on this machine."""
    taken = set()
    if state.DOCKER_OK:
        try:
            for c in state._docker.containers.list(all=True):
                taken.add(c.name)
                p = (c.labels or {}).get("com.docker.compose.project")
                if p:
                    taken.add(p)
        except Exception:
            pass
    for s in _st_load():
        if s.get("project"):
            taken.add(s["project"])
        try:
            for m in re.finditer(r"container_name:\s*(\S+)", Path(s["compose_path"]).read_text(encoding="utf-8")):
                taken.add(m.group(1).strip("\"'"))
        except Exception:
            pass
    return taken

def _friendly_names(entry: dict, tmpl: str) -> tuple:
    """-> (template text with readable container names and a top-level project `name:`, project)."""
    app = _slug(entry.get("name") or entry.get("id") or "app")
    svcs = [m.group("svc") for m in _CN_RE.finditer(tmpl)]

    def base(svc: str) -> str:
        s = _slug(svc)
        if len(svcs) <= 1 or s == app or s.startswith(app):
            return s if len(svcs) > 1 else app
        return f"{app}-{s}"

    taken, n = _taken_names(), 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        if not ({base(s) + suffix for s in svcs} & taken) and (app + suffix) not in taken:
            break
        n += 1
    project = app + suffix
    text = _CN_RE.sub(lambda m: m.group("pre") + base(m.group("svc")) + suffix, tmpl)
    return f"name: {project}\n" + text, project

def _project_of(compose_text: str, instance_id: str) -> str:
    """The Compose project an (edited) compose file belongs to: its top-level `name:`."""
    try:
        doc = yaml.safe_load(compose_text)
        n = doc.get("name") if isinstance(doc, dict) else None
        if isinstance(n, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", n):
            return n
    except Exception:
        pass
    return f"void_{instance_id}"

def stack_project(stack: dict) -> str:
    """The Compose project name of an installed stack (older records have no `project`)."""
    return stack.get("project") or f"void_{stack['instance_id']}"

def _load_catalog() -> list:
    """Read-fresh each call — a handful of small JSON files, no real cost,
    and it means a catalog entry can be added/edited on disk without a
    restart. The folder name is always the id, regardless of what (if
    anything) manifest.json itself claims, so there's one source of truth."""
    entries = []
    if not CATALOG_DIR.exists():
        return entries
    for d in sorted(CATALOG_DIR.iterdir()):
        if not d.is_dir():
            continue
        mpath, tpath = d / "manifest.json", d / "docker-compose.yml.tmpl"
        if not (mpath.exists() and tpath.exists()):
            continue
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
            m["id"] = d.name
            ic = catalog_pack.icon_file(d)
            if ic is not None:
                # the icon ships inside the catalog package; served by /api/appstore/icon/<id>
                m["icon"] = f"/api/appstore/icon/{d.name}?v={int(ic.stat().st_mtime)}"
            elif not str(m.get("icon") or "").strip():
                m["icon"] = "\U0001F4E6"
            entries.append(m)
        except Exception:
            continue
    return entries

def _st_load() -> list:
    with _stacks_lock:
        if STACKS_FILE.exists():
            try:
                data = json.loads(STACKS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list): return data
            except Exception:
                pass
        return []

def _st_save(d):
    with _stacks_lock:
        _atomic_write_json(STACKS_FILE, d)

def _install_worker(job_id: str, catalog_id: str, instance_id: str, user: dict,
                     compose_override: str | None = None, preview_secrets: dict | None = None,
                     options: dict | None = None):
    job = INSTALL_JOBS[job_id]
    try:
        entry = next((c for c in _load_catalog() if c["id"] == catalog_id), None)
        if not entry:
            job["error"] = "Catalog entry not found"; job["done"] = True; return
        _job_set(job, 3, "Preparing")

        data_dir = APP_DATA_DIR / instance_id
        data_dir.mkdir(parents=True, exist_ok=True)
        for sub in entry.get("data_paths", []):
            (data_dir / sub).mkdir(parents=True, exist_ok=True)

        if compose_override is not None:
            # Part A: install proceeds from exactly the text the admin saw and
            # edited in the preview modal — no further rendering/substitution
            # here, otherwise "let me edit it" would be cosmetic. instance_id
            # and secret_values are the ones already generated during preview
            # (passed through by api_appstore_install), so they match what was
            # actually shown, not a second freshly-generated set.
            rendered = compose_override
            project = _project_of(compose_override, instance_id)
            secret_values = preview_secrets or {}
            job["log"].append("Using the edited compose file submitted with this install.")
        else:
            tmpl = (CATALOG_DIR / catalog_id / "docker-compose.yml.tmpl").read_text(encoding="utf-8")
            subs = {"INSTANCE_ID": instance_id, "DATA_DIR": data_dir.as_posix()}  # forward slashes: what Docker Desktop's compose expects on Windows
            secret_values = {}
            for secret_name in entry.get("secrets", []):
                val = secrets.token_hex(16)  # don't ask the admin to invent a DB/admin password
                subs[secret_name] = val
                secret_values[secret_name] = val
            rendered, project = _friendly_names(entry, tmpl)
            for k, v in subs.items():
                rendered = rendered.replace("{{" + k + "}}", v)
            rendered, _nq = _normalize_env_quotes(rendered)
            rendered, opt_notes = _apply_options(entry, rendered, options)
            for note in opt_notes:
                job["log"].append(note)
            _job_set(job, 5, "Checking the latest version")
            # Part B: resolve the actual latest image tag(s), falling back to
            # whatever the template pins if a registry lookup fails/times out.
            rendered, tag_notes = _substitute_latest_tags(rendered)
            for note in tag_notes:
                job["log"].append(f"Image: {note}")
            rendered, arm_notes = _apply_arm_compat_profile(catalog_id, rendered)
            for note in arm_notes:
                job["log"].append(note)

        compose_path = data_dir / "docker-compose.yml"
        compose_path.write_text(rendered, encoding="utf-8")
        job["log"].append(f"Wrote {compose_path}")
        _job_set(job, 10, "Starting Docker")

        try:
            proc = subprocess.Popen(["docker", "compose", "-f", str(compose_path), "-p", project, "up", "-d"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", **winplat.no_window())
        except FileNotFoundError:
            job["error"] = ("Docker Compose isn't installed — the 'docker compose' command wasn't found. Install "
                            "Docker Compose v2 (included with Docker Desktop) and try again."); job["done"] = True; return
        except Exception as e:
            job["error"] = f"Could not start Docker Compose: {errors.describe(e)}"; job["done"] = True; return
        cprog = _ComposeProgress(job, rendered, lo=10, hi=88)
        for line in proc.stdout:
            job["log"].append(line.rstrip())
            cprog.feed(line)
        rc = proc.wait()
        if rc != 0:
            job["error"] = "docker compose up failed — see log"; job["done"] = True; return
        _job_set(job, 88, "Waiting for the app to start")

        ui_service = entry.get("ui_service", "")
        ui_port = entry.get("ui_container_port")
        fallback_name = ""
        host_port = None
        if state.DOCKER_OK:
            for _i in range(30):  # first-run image pulls / DB init can take a moment even after 'up -d' returns
                _job_set(job, 88 + _i * 0.25)
                c = _find_service_container(project, ui_service, fallback_name)
                try:
                    if c is not None:
                        c.reload()
                        bindings = (c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}).get(f"{ui_port}/tcp")
                        if bindings:
                            host_port = int(bindings[0]["HostPort"]); break
                except Exception:
                    pass
                time.sleep(1)
        if not host_port:
            why = ""
            c = _find_service_container(project, ui_service, fallback_name)
            if c is not None:
                d = diagnose.diagnose_container(c)
                if d.get("code") != "unknown" or d.get("restart_count"):
                    why = f"\n\nLikely cause: {d['title']}. {d['cause']}"
            job["error"] = "Containers started, but the app's web port could not be detected." + why
            job["done"] = True
            return

        # Watch the whole stack for a startup crash loop before declaring success, so an app that is
        # actually unusable is not reported as installed. A restart loop in one container (usually the
        # database) takes the others down with it, so every service the entry lists is checked.
        watch = entry.get("watch_services") or ([ui_service] if ui_service else [])
        if watch:
            job["log"].append("Watching the app's containers for a startup crash loop before declaring success...")
            _job_set(job, 92, "Checking that it stays up")
            bad = _watch_stack(project, watch)
            if bad is not None:
                d = diagnose.diagnose_container(bad)
                svc = (bad.labels or {}).get("com.docker.compose.service", bad.name)
                msg = f"{svc} keeps crashing and did not stabilise.\n\nLikely cause: {d['title']}\n{d['cause']}"
                if d.get("fix"):
                    msg += "\n\nWhat to do:\n" + "\n".join(f"- {f}" for f in d["fix"])
                if d.get("log_tail"):
                    msg += f"\n\nLast log lines from {svc}:\n{d['log_tail']}"
                msg += "\n\nThe containers were left in place; Containers → Why? shows this again."
                job["error"] = msg
                job["done"] = True
                return

        _job_set(job, 97, "Adding it to your home screen")
        tile = {"id": secrets.token_hex(4), "name": entry.get("name", catalog_id),
                "url": f"http://localhost:{host_port}", "icon": entry.get("icon", "📦"),
                "description": entry.get("description", ""),
                "tunnel_enabled": False, "local_port": host_port,
                "gate_enabled": False, "gate_pin_hash": None, "gate_cookie_ttl_hours": 12,
                "gate_is_pin": True, "gate_pin_length": None,
                "hidden_from_guests": False, "hidden_from_admins": False}  # visible-to-everyone default, same as any other new tile
        with _apps_lock:
            apps = _aload(); apps.append(tile); _asave(apps)
        with _stacks_lock:
            stacks = _st_load()
            stacks.append({"tile_id": tile["id"], "catalog_id": catalog_id, "instance_id": instance_id,
                            "compose_path": str(compose_path), "installed_at": time.time(),
                            "project": project,
                            "image_tags": _extract_image_tags(rendered),
                            "edited": compose_override is not None})
            _st_save(stacks)

        job["tile_id"] = tile["id"]; job["host_port"] = host_port
        job["secrets_generated"] = secret_values or None
        job["done"] = True
        _audit("appstore_install", user, target=entry.get("name", catalog_id),
               detail=f"instance {instance_id}, port {host_port}" + (", compose edited by admin" if compose_override is not None else ""))
    except Exception as e:
        job["error"] = f"{errors.describe(e)} (ref {_log_exception(e, source='job', context='install ' + catalog_id)})"
        job["done"] = True
