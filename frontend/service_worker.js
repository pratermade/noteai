const APP_VERSION = '__APP_VERSION__';
const STATIC_CACHE = `noterai-static-${APP_VERSION}`;
const STATIC_ASSETS = [
  '/', '/app.js', '/style.css',
  '/share.html', '/share.js',
  '/icons/icon-192.png', '/icons/icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== STATIC_CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // API calls and manifest: always go to network, never cache
  if (url.pathname.startsWith('/api/') || url.pathname === '/manifest.json') {
    return;
  }
  // Static assets: cache-first, fall back to network
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
