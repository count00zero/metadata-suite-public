// Minimal service worker: makes /nearby installable and keeps the page shell available offline.
// API calls (/sights ...) always go to the network.
const SHELL = "nearby-shell-v1";
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.add("/nearby")).catch(() => {})); self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname !== "/nearby") return;
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(x => x.put("/nearby", c)); return r; }).catch(() => caches.match("/nearby")));
});
