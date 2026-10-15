// Void OS — minimal service worker.
// This app is a live, authenticated dashboard, so we deliberately do NOT
// cache HTML/API responses (that could show stale data or leak content
// across accounts on a shared device). Its only job is to satisfy the
// browser's installability requirement for "Add to Home Screen" / PWA
// install prompts, plus a light cache for static assets so icons/fonts
// don't get re-fetched every load.

const STATIC_CACHE = "voidos-static-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== STATIC_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  const isStaticAsset = url.pathname.startsWith("/static/");
  if (!isStaticAsset) return; // let everything else (HTML/API) hit the network normally

  event.respondWith(
    caches.open(STATIC_CACHE).then(async (cache) => {
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => {
          if (res.ok) cache.put(req, res.clone());
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
