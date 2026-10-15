"""
modules/auth.py — Sign-in, session cookies, the session store, login lockout, current-user lookup.

Part of Void OS's server, split out of voidos.py one major feature per file.
Imports at the top come from modules that load earlier; imports at the
bottom are names from modules that load later and are only used inside
functions, at call time (the standard way for two modules to refer to
each other).
"""

from flask import request
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from modules import state
from modules.config import LOCKOUT_S, MAX_ATTS, SESSIONS_FILE, SESSION_HRS
from modules.web import _atomic_write_json, _fails, _sessions_lock


# ══════════════════════════════════════════════════════════════════════
# SESSION & AUTH
# ══════════════════════════════════════════════════════════════════════
# Cloudflare's published edge IP ranges — fetched from https://www.cloudflare.com/ips/ (page
# itself last updated Sep 28, 2023, still current as of this snapshot). Used only to decide
# whether to trust CF-Connecting-IP when Cloudflare is reached directly (a "Proxied" DNS record
# pointed straight at this server's exposed port) rather than through a local cloudflared Tunnel
# process — see _ip()'s docstring for the full reasoning. Cloudflare updates this list rarely;
# VOID_CF_IP_RANGES_URL can point at a live copy (e.g. a cron-refreshed local mirror of
# https://www.cloudflare.com/ips-v4 + /ips-v6) instead of this bundled snapshot if that matters
# for a given deployment — see _load_cf_ranges().
_CF_IP_RANGES_SNAPSHOT = [
    "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "104.16.0.0/13", "104.24.0.0/14",
    "108.162.192.0/18", "131.0.72.0/22", "141.101.64.0/18", "162.158.0.0/15", "172.64.0.0/13",
    "173.245.48.0/20", "188.114.96.0/20", "190.93.240.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
]
state._cf_ranges_cache = None  # parsed once, lazily — see _load_cf_ranges()

def _load_cf_ranges() -> list:
    """Parses _CF_IP_RANGES_SNAPSHOT (or a VOID_CF_IP_RANGES_URL override, read once at first
    use — not on a timer, since this is a short, rarely-changing list, not something that needs
    live refreshing for a self-hosted app) into ip_network objects. Bad/unparseable entries are
    skipped rather than raising — a malformed range from an operator's custom URL should degrade
    to "that one range isn't trusted," not take down the whole app."""
    pass  # (shared value now lives in modules/state.py)
    if state._cf_ranges_cache is not None:
        return state._cf_ranges_cache
    lines = _CF_IP_RANGES_SNAPSHOT
    url = os.environ.get("VOID_CF_IP_RANGES_URL")
    if url:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                lines = [l.strip() for l in resp.read().decode("utf-8").splitlines() if l.strip()]
        except Exception:
            lines = _CF_IP_RANGES_SNAPSHOT  # fall back to the bundled snapshot rather than trusting nothing
    ranges = []
    for line in lines:
        try:
            ranges.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            continue
    state._cf_ranges_cache = ranges
    return ranges

def _is_cloudflare_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in _load_cf_ranges())

def _ip():
    """The real visitor IP — correct whether the request arrives directly
    (LAN), through the nginx reverse-proxy setup, through a Cloudflare
    Tunnel, or through Cloudflare's edge proxying this server's port
    directly (a "Proxied" DNS record pointed straight at it, no cloudflared
    Tunnel process in between).

    CF-Connecting-IP is checked first: Cloudflare's edge sets it to the
    real visitor IP and strips any client-supplied version of that header
    before it's forwarded, which is why it's authoritative whenever the
    immediate connection genuinely comes from Cloudflare — plain
    X-Forwarded-For alone isn't reliable here, since depending on how a
    request gets relayed it can end up holding a proxy's own address
    instead of the actual visitor's.

    Forwarded-IP headers are only trusted when the immediate TCP peer is
    either loopback (a local process like cloudflared or nginx) or one of
    Cloudflare's own published edge IPs (Cloudflare reaching this server's
    port directly) — the only two ways these headers can legitimately
    appear in this app's supported deployment models. X-Forwarded-For
    specifically stays loopback-only even after this change: it's the
    weaker of the two signals (see above), so widening its trust to the
    Cloudflare-range case too would trade away more than the direct-proxy
    fix is worth — CF-Connecting-IP already covers that case on its own.
    Otherwise something reached the Flask port directly (e.g. it's also
    exposed on the LAN) and could forge either header to spoof its logged
    IP or split a brute-force attempt across fake addresses to dodge the
    per-IP lockout — in that case the raw socket address is what's trusted
    instead."""
    peer_is_loopback = request.remote_addr in ("127.0.0.1", "::1")
    if _is_cloudflare_ip(request.remote_addr):
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
    if peer_is_loopback:
        # Loopback peer = cloudflared or a local reverse proxy. Both APPEND
        # the address they actually saw to X-Forwarded-For, so the trusted
        # value is the RIGHTMOST entry; the leftmost is whatever the client
        # chose to send. (Taking [0] here let anyone behind nginx rotate a
        # fake X-Forwarded-For per attempt and never trip the login lockout.)
        # CF-Connecting-IP is only believed when it agrees with that, or when
        # there's no X-Forwarded-For at all -- through nginx, a client can
        # simply send its own CF-Connecting-IP header.
        xff = [x.strip() for x in request.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
        cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
        if xff:
            return xff[-1]
        if cf_ip:
            return cf_ip
    return request.remote_addr

def _request_is_https() -> bool:
    """True when this visitor reached us over HTTPS: directly, or through a proxy/tunnel on this machine or
    at Cloudflare (the only peers whose forwarded headers are believed) that says so."""
    if request.is_secure:
        return True
    peer = request.remote_addr or ""
    if peer in ("127.0.0.1", "::1") or _is_cloudflare_ip(peer):
        if request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https":
            return True
        if '"scheme":"https"' in request.headers.get("CF-Visitor", "").replace(" ", ""):
            return True
    return False

def _lock_key(ip: str) -> str:
    """What a failed sign-in is counted against. An IPv6 visitor controls a whole /64 (billions of
    addresses), so counting each address separately would make the lockout meaningless: they are
    counted by /64. An IPv4-mapped IPv6 address counts as the IPv4 address."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return ip

def _prune_fails(table: dict, limit: int = 4096) -> None:
    """Failure trackers are keyed by whatever address shows up, so they must not grow without bound."""
    if len(table) <= limit:
        return
    now = time.time()
    for k in [k for k, (c, u) in table.items() if (u and now >= u) or (not u and c <= 1)]:
        table.pop(k, None)
    if len(table) > limit:                      # still big (a flood of live lockouts): drop the oldest half
        for k in list(table)[: len(table) // 2]:
            table.pop(k, None)

def _is_locked(ip):
    c, u = _fails.get(_lock_key(ip), (0, 0.))
    if u and time.time() < u: return True, int(u - time.time())
    return False, 0

def _fail(ip):
    k = _lock_key(ip)
    c, u = _fails.get(k, (0, 0.))
    if u and time.time() >= u: c = 0
    c += 1
    _fails[k] = (0, time.time() + LOCKOUT_S) if c >= MAX_ATTS else (c, 0.)
    _prune_fails(_fails)

def _clear_fails(ip): _fails.pop(_lock_key(ip), None)

# A separate budget per ACCOUNT: the per-address lockout alone lets a guesser who spreads attempts over
# many addresses (a botnet, rotating proxies) try one account's password as often as they like. Ten wrong
# passwords in 15 minutes pause sign-in to that account for 5 minutes, whoever is asking. The pause is
# short on purpose: it caps guessing at ~1 attempt every 30 s without letting an attacker lock the real
# owner out for long.
ACCT_WINDOW_S = 900
ACCT_MAX_FAILS = 10
ACCT_LOCK_S = 300
_acct_fails: dict = {}
_acct_lock = threading.Lock()

def _acct_locked(username: str) -> tuple[bool, int]:
    key = str(username or "").strip().lower()[:64]
    with _acct_lock:
        st = _acct_fails.get(key)
        if st and time.time() < st["until"]:
            return True, int(st["until"] - time.time())
    return False, 0

def _acct_fail(username: str) -> None:
    key = str(username or "").strip().lower()[:64]
    if not key:
        return
    now = time.time()
    with _acct_lock:
        st = _acct_fails.setdefault(key, {"times": [], "until": 0.0})
        st["times"] = [t for t in st["times"] if now - t < ACCT_WINDOW_S] + [now]
        if len(st["times"]) >= ACCT_MAX_FAILS:
            st["until"] = now + ACCT_LOCK_S
            st["times"].clear()
        if len(_acct_fails) > 4096:
            for k in [k for k, v in _acct_fails.items() if now >= v["until"] and not v["times"]][:2048]:
                _acct_fails.pop(k, None)

def _acct_clear(username: str) -> None:
    with _acct_lock:
        _acct_fails.pop(str(username or "").strip().lower()[:64], None)

_DUMMY_PW_HASH = generate_password_hash(secrets.token_hex(16))
def _password_ok(user, pw: str) -> bool:
    """Always runs exactly one password-hash check, so a wrong username takes
    as long as a wrong password -- otherwise the response time alone told an
    attacker which usernames exist (the hash is the slow part)."""
    if not user:
        check_password_hash(_DUMMY_PW_HASH, pw)
        return False
    return check_password_hash(user.get("password_hash", ""), pw)

# Share-link password guesses get their own lockout tracker, same shape as the
# login one above but deliberately separate — a public visitor hammering a
# share link's password shouldn't also lock that IP out of the login page
# (plausible on a shared/NAT'd IP — a household member's login attempt
# shouldn't be collateral damage from a stranger probing a share link).
_share_fails = {}
def _share_is_locked(ip):
    c, u = _share_fails.get(_lock_key(ip), (0, 0.))
    if u and time.time() < u: return True, int(u - time.time())
    return False, 0
def _share_fail(ip):
    k = _lock_key(ip)
    c, u = _share_fails.get(k, (0, 0.))
    if u and time.time() >= u: c = 0
    c += 1
    _share_fails[k] = (0, time.time() + LOCKOUT_S) if c >= MAX_ATTS else (c, 0.)
    _prune_fails(_share_fails)
def _share_clear_fails(ip): _share_fails.pop(_lock_key(ip), None)

# One link's own budget, across every visitor: per-IP lockout alone lets a
# guesser with many addresses walk a 6-digit PIN. Same idea as the Access Gate's
# gate-wide limit (see gate_proxy.py).
SHARE_LINK_WINDOW_S = 600
SHARE_LINK_MAX_FAILS = int(os.environ.get("VOID_SHARE_LINK_MAX_FAILS", "30"))
SHARE_LINK_LOCK_S = 300
_share_link_fails = {}  # share id -> {"times": [...], "until": float}
_share_link_lock = threading.Lock()

def _share_link_locked(sid: str) -> tuple[bool, int]:
    with _share_link_lock:
        st = _share_link_fails.get(sid)
        if st and time.time() < st["until"]:
            return True, int(st["until"] - time.time())
    return False, 0

def _share_link_fail(sid: str) -> None:
    now = time.time()
    with _share_link_lock:
        st = _share_link_fails.setdefault(sid, {"times": [], "until": 0.0})
        st["times"] = [t for t in st["times"] if now - t < SHARE_LINK_WINDOW_S] + [now]
        if len(st["times"]) >= SHARE_LINK_MAX_FAILS:
            st["until"] = now + SHARE_LINK_LOCK_S
            st["times"].clear()

# Separate from the password-guess lockout above — this limits *download
# calls themselves*, which matters because most shares have no password at
# all (or are already unlocked), so nothing was otherwise stopping the same
# visitor from hitting /s/<token>/download in a tight loop. Deliberately
# global-per-IP rather than per-token, so someone can't dodge it by cycling
# through several share links they hold.
SHARE_DL_RATE_LIMIT = int(os.environ.get("VOID_SHARE_DL_RATE_LIMIT", "10"))
SHARE_DL_RATE_WINDOW_S = int(os.environ.get("VOID_SHARE_DL_RATE_WINDOW_S", "60"))
_share_dl_hits = {}  # ip -> [timestamps within the current rolling window]
_share_dl_hits_lock = threading.Lock()

def _share_dl_rate_check(ip: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Rolling window, not a fixed
    bucket, so a burst right at a window boundary can't double up."""
    now = time.time()
    with _share_dl_hits_lock:
        hits = [t for t in _share_dl_hits.get(ip, []) if now - t < SHARE_DL_RATE_WINDOW_S]
        if len(hits) >= SHARE_DL_RATE_LIMIT:
            _share_dl_hits[ip] = hits
            return False, max(1, int(SHARE_DL_RATE_WINDOW_S - (now - hits[0])))
        hits.append(now)
        _share_dl_hits[ip] = hits
        return True, 0

# Concurrent *folder* downloads specifically (the resource this protects is
# _share_zip_semaphore's 3 global slots) — one visitor should never be able
# to occupy more than one of them at a time, regardless of how many parallel
# requests or tabs they open. This is the direct fix for "one visitor parks
# all 3 slots and every other share link on the server goes unavailable."
MAX_SHARE_ZIP_DOWNLOADS_PER_IP = 1
_share_zip_inflight_by_ip = {}  # ip -> count
_share_zip_inflight_lock = threading.Lock()

# Same rate-limit shape as the share-download path above, applied to the
# Bluetooth Intercom scan/pair/connect endpoints and call starts (FR §9) —
# a compromised or buggy client shouldn't be able to hammer `bluetoothctl
# scan` or spin up call sessions in a loop. Per-account (not per-IP, unlike
# the share-link limiter) since every intercom caller is already
# authenticated — there's no anonymous-visitor case to worry about here.
INTERCOM_RATE_LIMIT = int(os.environ.get("VOID_INTERCOM_RATE_LIMIT", "10"))
INTERCOM_RATE_WINDOW_S = int(os.environ.get("VOID_INTERCOM_RATE_WINDOW_S", "60"))
_intercom_hits = {}  # user_id -> [timestamps within the current rolling window]
_intercom_hits_lock = threading.Lock()

def _intercom_rate_check(user_id: str) -> tuple[bool, int]:
    now = time.time()
    with _intercom_hits_lock:
        hits = [t for t in _intercom_hits.get(user_id, []) if now - t < INTERCOM_RATE_WINDOW_S]
        if len(hits) >= INTERCOM_RATE_LIMIT:
            _intercom_hits[user_id] = hits
            return False, max(1, int(INTERCOM_RATE_WINDOW_S - (now - hits[0])))
        hits.append(now)
        _intercom_hits[user_id] = hits
        return True, 0

# ── Session store (backs the "Sessions" tab: view/revoke active logins) ─
def _geolocate_ip(ip: str) -> tuple:
    """Best-effort IP -> (country, country_code) lookup via a free public API.
    Never raises, and times out fast — a failed lookup just means the
    Sessions tab shows no country for that entry, a cosmetic degradation,
    never a functional one. Runs off the request thread (see _set_session)
    so a slow/unreachable API can't add latency to login itself.
    ip-api.com's free tier is HTTP-only (HTTPS needs their paid plan) —
    acceptable here since this is a server-to-server lookup of a
    non-sensitive value (just a country name for display), not something
    carrying credentials or user data."""
    if os.environ.get("VOID_GEOLOCATE", "on").strip().lower() in ("off", "false", "0", "no"):
        return ("", "")  # owner opted out: visitor IPs never leave this server
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return ("Local Network", "")
    except ValueError:
        return ("", "")
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("status") == "success":
            return (data.get("country", ""), data.get("countryCode", ""))
    except Exception:
        pass
    return ("", "")

def _geolocate_and_update_session(sid: str, ip: str):
    """Runs in a background thread after login already responded."""
    country, code = _geolocate_ip(ip)
    if not country: return
    with _sessions_lock:
        sessions = _sessions_load()
        for s in sessions:
            if s.get("id") == sid:
                s["country"] = country
                s["country_code"] = code
                _sessions_save(sessions)
                return
        # session already revoked/pruned before the lookup finished — nothing to update

def _sessions_load() -> list:
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []

def _sessions_save(sessions: list):
    _atomic_write_json(SESSIONS_FILE, sessions)

def _sessions_prune(sessions: list) -> list:
    """Drop sessions past their max age so the store doesn't grow forever.
    (The signed cookie itself still enforces expiry independently.)"""
    cutoff = time.time() - SESSION_HRS * 3600
    return [s for s in sessions if s.get("created_at_ts", 0) >= cutoff]

def _parse_user_agent(ua: str) -> dict:
    """Best-effort, dependency-free UA breakdown for the session-details view.
    Never raises — worst case, fields come back as 'Unknown'."""
    ua = ua or ""
    try:
        # OS + version
        os_name, os_version = "Unknown", ""
        m = re.search(r"Windows NT ([\d.]+)", ua)
        if m:
            os_name = "Windows"
            os_version = {"10.0":"10/11","6.3":"8.1","6.2":"8","6.1":"7"}.get(m.group(1), m.group(1))
        elif "iPhone" in ua or "iPad" in ua or "iPod" in ua:
            os_name = "iOS"
            m = re.search(r"OS ([\d_]+) like Mac OS X", ua)
            if m: os_version = m.group(1).replace("_", ".")
        elif "Mac OS X" in ua or "Macintosh" in ua:
            os_name = "macOS"
            m = re.search(r"Mac OS X ([\d_]+)", ua)
            if m: os_version = m.group(1).replace("_", ".")
        elif "Android" in ua:
            os_name = "Android"
            m = re.search(r"Android ([\d.]+)", ua)
            if m: os_version = m.group(1)
        elif "Linux" in ua:
            os_name = "Linux"

        # Browser + version — order matters (Edge/Opera UAs also contain "Chrome")
        browser, browser_version = "Unknown", ""
        if "Edg/" in ua:
            browser = "Edge"; m = re.search(r"Edg/([\d.]+)", ua)
        elif "OPR/" in ua:
            browser = "Opera"; m = re.search(r"OPR/([\d.]+)", ua)
        elif "Firefox/" in ua:
            browser = "Firefox"; m = re.search(r"Firefox/([\d.]+)", ua)
        elif "Chrome/" in ua:
            browser = "Chrome"; m = re.search(r"Chrome/([\d.]+)", ua)
        elif "Safari/" in ua and "Version/" in ua:
            browser = "Safari"; m = re.search(r"Version/([\d.]+)", ua)
        else:
            m = None
        if m: browser_version = m.group(1)

        # Engine
        if "Gecko/" in ua and "like Gecko" not in ua: engine = "Gecko"
        elif "AppleWebKit" in ua and "Chrome" not in ua and "Edg" not in ua: engine = "WebKit"
        elif "AppleWebKit" in ua: engine = "Blink" if ("Chrome/" in ua or "Edg/" in ua or "OPR/" in ua) else "WebKit"
        else: engine = "Unknown"

        # Device type
        if "Mobi" in ua or "iPhone" in ua: device = "Mobile"
        elif "iPad" in ua or ("Android" in ua and "Mobile" not in ua): device = "Tablet"
        else: device = "Desktop"

        return {"os": os_name, "os_version": os_version, "browser": browser,
                "browser_version": browser_version, "engine": engine, "device_type": device}
    except Exception:
        return {"os":"Unknown","os_version":"","browser":"Unknown",
                "browser_version":"","engine":"Unknown","device_type":"Unknown"}

def _describe_ua(ua: str) -> str:
    """Small, dependency-free User-Agent -> friendly label."""
    ua = ua or ""
    if "Windows" in ua: os_lbl = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua: os_lbl = "macOS"
    elif "Android" in ua: os_lbl = "Android"
    elif "iPhone" in ua or "iPad" in ua: os_lbl = "iOS"
    elif "Linux" in ua: os_lbl = "Linux"
    else: os_lbl = "Unknown device"
    if "Edg/" in ua: browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua: browser = "Opera"
    elif "Chrome" in ua: browser = "Chrome"
    elif "Firefox" in ua: browser = "Firefox"
    elif "Safari" in ua: browser = "Safari"
    else: browser = "Browser"
    return f"{browser} on {os_lbl}"
