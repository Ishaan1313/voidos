"""
Void OS server code, one major feature per file.

voidos.py (the entry point) imports the feature modules below in this order;
each one registers its own routes on the shared Flask app from web.py. A
module only imports names from modules ABOVE it when it loads; names it uses
from modules below it are bound afterwards by its _bind_later_modules(), which
voidos.py calls once everything has loaded. Keep new code in that shape: put
a feature in its own file, import what it needs from earlier modules, and add
it to _MODULES in voidos.py.

Feature modules (load order):
    config.py           Startup, environment (.env), paths and every configuration constant.
    web.py              The Flask app, WebSocket support, live-session registry, security headers, HTTP error handlers.
    users.py            User store, server-wide UI default, .env read/write, terminal password, per-user wallpapers.
    twofactor.py        Optional two-factor sign-in logic: TOTP codes, backup codes, wrong-code throttle.
    limits.py           Fair-use limits for downloads (per account / visitor address / global).
    auth.py             Sign-in, session cookies, the session store, login lockout, current-user lookup.
    activity.py         Activity (audit) log, outbound notifications, and the in-app notification feed.
    error_log.py        Error log with reference codes, friendly error responses, HTTP error handlers.
    decorators.py       Route decorators: require_auth, require_admin, require_superuser and friends.
    file_access.py      Who may access which file path (_safe), protected folders, guest quotas.
    security.py         First-run filesystem layout and the cross-site request guard.
    system.py           Server statistics (CPU, memory, drives) and Docker connection helpers.
    file_helpers.py     File-type detection, file icons, App Store icon proxy.
    account_routes.py   Page rendering, setup wizard, account settings, notification settings, health and error-log routes.
    cloud_routes.py     Dropbox / Google Drive: connect, browse, import, export, continuous sync.
    storage_routes.py   External storage: USB drives and SMB/NFS network shares.
    admin_routes.py     Passwords, sessions, user management, backup & restore, activity-log routes.
    twofa_routes.py     Two-factor sign-in routes: set up, turn off, backup codes, admin reset.
    container_routes.py Live stats endpoint and Docker container management.
    file_routes.py      Browsing, viewing, editing, renaming and uploading files.
    file_ops.py         Background copy / move / delete / import jobs with progress.
    trash_zip_routes.py Delete, Recycle Bin, search, downloads, zip downloads.
    shares.py           Public share links: create, manage, landing page, downloads.
    webdav.py           WebDAV network-drive server.
    intercom_routes.py  Intercom HTTP routes (rooms, devices, Bluetooth).
    app_tiles.py        Dashboard app tiles, tunnels/gate settings per tile, Panic button.
    app_store.py        App Store engine: image tags, compose safety scan, ARM tweaks, install worker.
    disk_monitor.py     Background disk-space monitor and alerts.
    app_store_routes.py App Store HTTP routes: catalog, preview, install, update, uninstall.
    live_sockets.py     WebSockets: in-browser terminal, live control channel, intercom audio.
    diagnose.py         'Why is this container restarting?': exit code + logs + host facts -> plain cause and fix.
    catalog_pack.py     The App Store list as a package: index, validation, build (pure Python; no server needed to use it).
    catalog_update.py   Refreshing the App Store list from the project's GitHub release, without a program update.
    domains.py          Custom domains for app tiles: Cloudflare API (named tunnel + DNS record per tile).
    tunnels.py          Cloudflare tunnels (main + per app), Access Gate supervisor, Workers KV sync.
    updater.py          In-app updates: check, download, verify, install, restart.

Supporting libraries (no routes; imported where needed):
    cloud_storage.py    Dropbox / Google Drive API client (OAuth, list, upload, download).
    cloud_sync.py       rclone bisync supervisor for continuous cloud sync.
    consolelog.py       Everything printed to the server's console: plain, consistently indented text.
    errors.py           Turns exceptions into plain-language messages with hints.
    external_storage.py USB / SMB / NFS mounting (Linux and Windows backends).
    gate_proxy.py       Access Gate: PIN-protecting reverse proxy, run as its own process.
    intercom.py         The intercom: rooms, live audio hubs, talk floor, wire protocol (v2).
    intercom_audio.py   Intercom audio layer: devices and streams on Windows/Linux, mu-law codec.
    intercom_bt.py      Bluetooth pairing for the intercom (Linux BlueZ, Windows WinRT).
    notifications.py    Email / Discord / Telegram senders.
    winplat.py          Windows compatibility helpers (safe no-ops elsewhere).
    envfile.py          The single reader/editor of .env (shared with the installer).
    state.py            Values that change at runtime and are shared between modules.
    secret_store.py     Encryption at rest for stored secrets (cloud login tokens), keyed from VOID_SECRET_KEY.
"""
