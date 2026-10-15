/**
 * cloudflare-worker-example.js — a stable address for a Void OS Quick Tunnel.
 *
 * Cloudflare Quick Tunnel URLs (https://xxxx.trycloudflare.com) change every
 * time the tunnel restarts. Void OS writes the current URL into a Workers KV
 * namespace each time (see CF_KV_NAMESPACE in .env.example); this Worker
 * reads it and forwards every request there, so the Worker's own address
 * (https://<name>.<account>.workers.dev, or a custom route) never changes.
 *
 * Setup (Cloudflare dashboard):
 *   1. Workers & Pages → Create → Worker → paste this file → Deploy.
 *   2. Worker → Settings → Bindings → add a KV namespace binding:
 *        Variable name: TUNNEL      Namespace: the one in CF_KV_NAMESPACE
 *   3. Optional, for an individual app tile's tunnel instead of the
 *      dashboard: Settings → Variables → add  KEY_NAME = app_<tile id>
 *      (one separate Worker per app; leave unset for the dashboard).
 *
 * Two details matter, or the in-browser Terminal (and any app that uses
 * WebSockets) breaks while everything else seems fine:
 *   - It PROXIES rather than redirects — browsers won't follow a redirect
 *     for a WebSocket handshake.
 *   - On a 101 (WebSocket upgrade) it returns the upstream response's
 *     `webSocket`; rebuilding the response without it tells the browser
 *     "switching protocols" with no connection behind it.
 */
export default {
  async fetch(request, env) {
    const key = env.KEY_NAME || "current";
    let target;
    try {
      target = await env.TUNNEL.get(key);
    } catch (e) {
      return offline("The TUNNEL KV binding is missing — add it under Settings → Bindings.", 500);
    }
    if (!target) {
      return offline(`No address stored under "${key}" yet — start Void OS with its tunnel enabled.`, 503);
    }

    const incoming = new URL(request.url);
    const upstream = new URL(incoming.pathname + incoming.search, target);

    let response;
    try {
      // Same method, headers and body; fetch() sets Host from the new URL.
      response = await fetch(new Request(upstream.toString(), request), { redirect: "manual" });
    } catch (e) {
      return offline("Void OS isn't reachable right now — it may be restarting.", 502);
    }

    if (response.status === 101) {
      return new Response(null, { status: 101, webSocket: response.webSocket });
    }

    // Rewrite redirects that point at the tunnel back to this Worker's own
    // address, so visitors never see the rotating trycloudflare.com URL.
    const location = response.headers.get("Location");
    if (location) {
      try {
        const loc = new URL(location, upstream);
        if (loc.host === upstream.host) {
          const headers = new Headers(response.headers);
          headers.set("Location", incoming.origin + loc.pathname + loc.search + loc.hash);
          return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
        }
      } catch (e) { /* leave unusual Location values untouched */ }
    }
    return response;
  },
};

function offline(message, status) {
  const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Void OS — offline</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b12;color:#e8e6f5;
font:15px/1.5 system-ui,sans-serif;text-align:center;padding:24px}h1{font-size:20px;margin:0 0 8px;color:#b99cff}
p{margin:0;opacity:.75;max-width:420px}</style>
<div><h1>Void OS is offline</h1><p>${message}</p><p style="margin-top:14px;font-size:13px">This page retries automatically.</p></div>
<script>setTimeout(()=>location.reload(),10000)</script>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Retry-After": "10" } });
}
