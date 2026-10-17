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

// The page shown when the tunnel is down. Same purple Void OS look as the dashboard's own 404 page; it carries
// everything inline (a Worker can't serve static files), tries again by itself every 10 seconds, and escapes the message.
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function offline(message, status) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>Void OS is offline</title>
<style>
:root{--bg:#0a0a12;--pu:#6d4aff;--pu-rgb:109,74,255;--ps:#9b85ff;--pd:#4a3aa8;--tx:#f3f1fb;--td:#9a95a8;--tm:#9690a6}
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:24px 20px;overflow-x:hidden;
  background:var(--bg);color:var(--tx);font:15px/1.6 "Sora","Segoe UI",system-ui,-apple-system,sans-serif;text-align:center}
.glowA,.glowB{position:fixed;border-radius:50%;filter:blur(90px);opacity:.3;pointer-events:none;z-index:0}
.glowA{width:480px;height:480px;top:-14%;left:-10%;background:radial-gradient(circle,var(--pu),transparent 70%)}
.glowB{width:400px;height:400px;bottom:-16%;right:-8%;background:radial-gradient(circle,var(--ps),transparent 70%)}
main{position:relative;z-index:1;max-width:520px;width:100%}
svg{width:clamp(110px,30vw,150px);height:auto;display:block;margin:0 auto 14px;overflow:visible;color:var(--tx)}
.pulse{transform-origin:32px 32px;animation:pulse 5s ease-in-out infinite alternate}
@keyframes pulse{from{opacity:.75;transform:scale(.97)}to{opacity:1;transform:scale(1.04)}}
h1{font-size:clamp(22px,5vw,28px);font-weight:650;letter-spacing:-.02em;margin-bottom:10px}
p{color:var(--td);max-width:430px;margin:0 auto}
.code{display:inline-block;margin-top:14px;padding:4px 12px;border-radius:999px;font:12.5px ui-monospace,Menlo,Consolas,monospace;
  color:var(--ps);background:rgba(var(--pu-rgb),.1);border:1px solid rgba(var(--pu-rgb),.22)}
button{margin-top:26px;font:inherit;font-size:14.5px;font-weight:600;color:#fff;cursor:pointer;padding:11px 24px;border-radius:12px;border:0;
  background:linear-gradient(135deg,var(--pu),var(--ps));box-shadow:0 8px 26px rgba(var(--pu-rgb),.35)}
button:focus-visible{outline:2px solid var(--ps);outline-offset:3px}
.retry{margin-top:14px;font-size:13px;color:var(--tm)}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
</style></head><body>
<div class="glowA"></div><div class="glowB"></div>
<main>
<svg viewBox="0 0 64 64" role="img" aria-label="Void OS"><g class="pulse"><path fill-rule="evenodd" fill="var(--pu)" fill-opacity=".5" d="M11.1 32a20.9 20.9 0 1 0 41.8 0a20.9 20.9 0 1 0 -41.8 0Z M15.9 32a16.1 16.1 0 1 0 32.2 0a16.1 16.1 0 1 0 -32.2 0Z"/></g>
<g transform="translate(32 32) rotate(-10)"><clipPath id="far"><path clip-rule="evenodd" d="M-80 -80H80V80H-80Z M-16.9 0a16.9 16.9 0 1 0 33.8 0a16.9 16.9 0 1 0 -33.8 0Z"/></clipPath><path d="M-27 0A27 6.4 0 0 1 27 0Z" fill="var(--pu)" fill-opacity=".55" clip-path="url(#far)"/></g>
<circle cx="32" cy="32" r="13" fill="none" stroke="currentColor" stroke-width="3.8"/>
<g transform="translate(32 32) rotate(-10)"><path d="M-27 0A27 6.4 0 0 0 27 0Z" fill="var(--pu)"/></g></svg>
<h1>Void OS is offline</h1>
<p>${esc(message)}</p>
<span class="code">${Number(status)}</span><br>
<button type="button" onclick="location.reload()">Try again now</button>
<p class="retry" id="r">Trying again in 10 seconds&hellip;</p>
</main>
<script>var n=10,el=document.getElementById('r');setInterval(function(){n--;if(n<=0){location.reload();return}el.textContent='Trying again in '+n+' second'+(n===1?'':'s')+'\u2026'},1000)</script>
</body></html>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Retry-After": "10" } });
}
