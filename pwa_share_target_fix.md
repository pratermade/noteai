# PWA Share Target Fix — Service Worker Manifest Intercept

## Root Cause Recap

Chrome tracks an installed PWA by the manifest URL recorded **at install time** and re-fetches that same URL (~24h) for updates. DOM `<link>` changes, cookies, and blob URLs have no effect on already-installed apps. The only mechanism that can transparently rewrite what Chrome receives when it fetches `/manifest.json` in the background is a **Service Worker fetch intercept**.

The previous SW attempt (#4) likely failed due to one or both of:

1. **Conditional `respondWith` after `await`** — if `event.respondWith()` isn't called synchronously (before the handler returns), Chrome falls through to the network and bypasses the SW entirely.
2. **SW not active/controlling** — without `skipWaiting()` + `clients.claim()`, a newly registered or updated SW sits in `waiting` state and never handles fetches for the current session.

---

## Fix: Three Files to Change

### 1. Service Worker (`sw.js`)

Add `skipWaiting` and `clients.claim` to force immediate activation. Fix the manifest fetch intercept to **always** call `respondWith` synchronously for `/manifest.json`, with the async IndexedDB lookup inside the promise.

```js
// --- Lifecycle: force immediate activation ---

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

// --- Manifest intercept ---

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (url.pathname === '/manifest.json') {
    // CRITICAL: call respondWith() synchronously — do ALL async
    // work inside the promise, not before the call.
    event.respondWith(
      (async () => {
        try {
          const db = await openDB();
          const shareKey = await getKey(db, 'share_key');
          if (shareKey) {
            const resp = await fetch(`/manifest/${shareKey}.json`);
            if (resp.ok) return resp;
          }
        } catch (e) {
          console.warn('[SW] manifest intercept error:', e);
        }
        // Fallback: return the base manifest from network
        return fetch(event.request);
      })()
    );
    return; // don't fall through to other fetch handling
  }

  // ...existing fetch handlers (cache strategies, etc.)...
});

// --- IndexedDB helpers (no external deps in SW scope) ---

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('noterai-sw', 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('config');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function getKey(db, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction('config', 'readonly');
    const req = tx.objectStore('config').get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
```

> **Why raw IndexedDB instead of `idb` or another wrapper?** Service workers can't import arbitrary npm modules at runtime. Keep it dependency-free.

### 2. Frontend (`app.js`) — Post-Login SW Setup

After a successful login, write `share_key` to IndexedDB **and** force a SW update so the new code activates immediately.

```js
async function updatePwaManifest() {
  try {
    // Fetch the user's share_key from the authenticated endpoint
    const resp = await fetch('/api/manifest', {
      headers: { 'Authorization': `Bearer ${getToken()}` }
    });
    if (!resp.ok) return;
    const { share_key } = await resp.json();

    // Write to IndexedDB (same DB/store the SW reads)
    const db = await openNoteraDB();
    const tx = db.transaction('config', 'readwrite');
    tx.objectStore('config').put(share_key, 'share_key');
    await tx.complete || await new Promise(r => tx.oncomplete = r);

    // Force SW to re-check for updates and activate immediately
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) {
      await reg.update();
      console.log('[PWA] SW update triggered after login');
    }

    // Also swap the DOM <link> for fresh installs happening right now
    const link = document.querySelector('link[rel="manifest"]');
    if (link) {
      link.href = `/manifest/${share_key}.json`;
    }

  } catch (e) {
    console.warn('[PWA] manifest update failed:', e);
  }
}

function openNoteraDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('noterai-sw', 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('config');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
```

Call `updatePwaManifest()` immediately after login succeeds and the JWT is stored.

### 3. Logout Cleanup

On logout, clear the share key so the SW stops injecting a stale user's share target:

```js
async function clearPwaManifest() {
  try {
    const db = await openNoteraDB();
    const tx = db.transaction('config', 'readwrite');
    tx.objectStore('config').delete('share_key');
  } catch (e) {
    // Non-critical
  }
}
```

Call from your existing `logout()` function.

---

## Backend — No Changes Required

The existing routes are correct as-is:

- `GET /manifest.json` — base manifest, no `share_target` (serves as fallback)
- `GET /manifest/<share_key>.json` — per-user manifest with `share_target`
- `GET /api/manifest` — returns `{ share_key }` to authenticated frontend

---

## Deployment Steps

1. Apply the SW, app.js, and logout changes above.
2. **Bump `APP_VERSION`** (or change any byte in `sw.js`) so browsers detect a new SW.
3. Redeploy / restart the container.

---

## User-Side Migration (Existing Installs)

Existing PWA installs have the old SW (or no SW intercept). Users need to reinstall once:

1. Log in via Chrome on Android.
2. Long-press the NoterAI home screen icon → **Uninstall**.
3. Open the app URL in Chrome, log in again.
4. Three-dot menu → **Add to Home Screen**.

After this one-time reinstall, the SW intercept handles all future manifest checks — no further reinstalls needed even if the share key changes (e.g., key rotation, password reset).

---

## Verification Checklist

- [ ] After login, confirm `share_key` is in IndexedDB: DevTools → Application → IndexedDB → `noterai-sw` → `config`
- [ ] Confirm SW is active and controlling: DevTools → Application → Service Workers → Status should show "activated and is running"
- [ ] Trigger a manual manifest check: DevTools → Application → Manifest → click the manifest URL — it should return the per-user manifest with `share_target` present
- [ ] Share a URL from Chrome to the Android share sheet — NoterAI should appear as a target
- [ ] After logout, confirm `share_key` is removed from IndexedDB
- [ ] Test a fresh install (not an update): uninstall PWA completely, log in, install — share target should work immediately

---

## If It Still Doesn't Work

If Chrome's background manifest fetcher still bypasses the SW on some versions:

- **Nuclear option**: Make `/manifest.json` itself user-aware by encoding the share key in the `start_url` at install time. The manifest URL stays `/manifest.json`, but the server can read a query param or path segment from the referrer/start_url to identify the user. This is more invasive but avoids the SW dependency entirely.
- **Check Chrome version**: The SW fetch intercept for manifest checks became reliable around Chrome 100+. Older versions may not route background manifest fetches through the SW.
- **`chrome://flags/#update-pwa-dialog-on-name-change`**: Enabling this flag can force Chrome to re-process manifest changes more aggressively.
