"""
modules/state.py — values that change while Void OS runs and that several
modules read.

Most module-level names never change after startup, so modules simply
import them. These few are reassigned at runtime (the tunnel comes up and
gets a new address, Docker starts, a cache refreshes). A plain
`from x import NAME` would leave each importer holding a stale copy, so they
live here and everything reads and writes them as `state.NAME`.

Each value is first set by the module that owns it, when that module loads:
    _docker, DOCKER_OK, DOCKER_ERROR    modules/config.py   Docker client and its status
    _cf_ranges_cache                    modules/auth.py     parsed Cloudflare IP ranges
    _np                                 modules/system.py   previous network counters (for speeds)
    TUNNEL_URL, TUNNEL_STATUS,
    TUNNEL_PROC                         modules/tunnels.py  the main Cloudflare tunnel
    KV_SYNC_STATUS, KV_SYNC_ERROR,
    KV_LAST_SYNCED_URL,
    KV_LAST_SYNCED_AT                   modules/tunnels.py  Workers KV address sync
    _gate_audit_pos                     modules/tunnels.py  Access Gate audit-log read position
"""
