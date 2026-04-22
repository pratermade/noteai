const APP_VERSION = '__APP_VERSION__';
const STATIC_CACHE = `noterai-static-${APP_VERSION}`;
const STATIC_ASSETS = [
  '/', '/app.js', '/style.css',
  '/share.html', '/share.js',
  '/icons/icon-192.png', '/icons/icon-512.png',
];

async function _getIdbShareKey() {
  return new Promise(resolve => {
    const req = indexedDB.open('noterai-sw', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => {
      const r = req.result.transaction('kv', 'readonly').objectStore('kv').get('share_key');
      r.onsuccess = () => resolve(r.result || null);
      r.onerror = () => resolve(null);
    };
    req.onerror = () => resolve(null);
  });
}

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

const NETWORK_FIRST = ['/', '/app.js', '/style.css'];

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/')) return;

  if (url.pathname === '/manifest.json') {
    event.respondWith((async () => {
      const shareKey = await _getIdbShareKey();
      if (shareKey) {
        try { return await fetch(`/manifest/${shareKey}.json`); } catch {}
      }
      return fetch(event.request);
    })());
    return;
  }

  if (NETWORK_FIRST.some(p => url.pathname === p)) {
    event.respondWith(
      fetch(event.request)
        .then(res => {
          const clone = res.clone();
          caches.open(STATIC_CACHE).then(c => c.put(event.request, clone));
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
