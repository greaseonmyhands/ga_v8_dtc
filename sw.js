// Offline-first: everything (including the data) is in index.html, so cache it once and serve it from cache.
const CACHE = 'gen5-dtc-d7a03c8a';
const FILES = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/icon-maskable-512.png', 'icons/apple-touch-icon.png'];
self.addEventListener('install', (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith('gen5-dtc-') && k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
// Page loads are network-first (so an update shows on the very next open when online) with a 4 s timeout and
// the cached copy as the offline fallback. Icons and other static files are cache-first.
function networkFirst(req) {
  return new Promise((resolve) => {
    let done = false;
    const fallback = () => caches.match(req, { ignoreSearch: true }).then((hit) => hit || caches.match('index.html'));
    const timer = setTimeout(() => { if (!done) fallback().then((r) => { if (r && !done) { done = true; resolve(r); } }); }, 4000);
    fetch(req, { cache: 'no-cache' }).then((res) => {
      clearTimeout(timer);
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      if (!done) { done = true; resolve(res); }
    }).catch(() => { clearTimeout(timer); if (!done) fallback().then((r) => { done = true; resolve(r || Response.error()); }); });
  });
}
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.mode === 'navigate' || /\/(index\.html)?$/.test(url.pathname) || /\.(webmanifest)$/.test(url.pathname)) { e.respondWith(networkFirst(e.request)); return; }
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((hit) => hit || fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return res;
  }).catch(() => caches.match('index.html'))));
});
