# PWA Share Target — Problem & Attempts

## Problem

After adding multi-user auth, each user has a `share_key` UUID. The `share_target.action` URL in the PWA manifest must point to `/api/share?key=<share_key>` for Android's share sheet to route shares to the correct user. The base `/manifest.json` (served before login) has no `share_target`, so Android never registers the app as a share target after the multi-user changes.

## Root Cause

Chrome's Android PWA runtime:
- Tracks an installed app by the manifest URL recorded at install time
- Re-fetches that URL every ~24h to check for updates to the manifest
- Does NOT use the current DOM `<link rel="manifest">` href for update checks — only for fresh installs

This means anything that changes what URL is in the DOM after install has no effect on the already-installed PWA's share_target registration.

---

## Attempts

### 1. Blob URL (failed)
**Approach**: After login, fetch the per-user manifest JSON, create a `Blob`, set `<link rel="manifest">` href to `URL.createObjectURL(blob)`.  
**Why it failed**: Blob URLs are temporary and session-scoped. Chrome's background manifest checker does not use them. The object URL is not accessible across sessions or from the browser's background manifest fetcher.

### 2. Cookie-based `/manifest.json` (failed)
**Approach**: Login sets a `share_key` cookie; `/manifest.json` FastAPI route reads the cookie and returns a personalized manifest if valid.  
**Why it failed**: Chrome's background PWA manifest update fetcher does not reliably send cookies. The manifest fetched for the installed PWA update check arrived without the cookie, so it returned the base manifest with no `share_target`.

### 3. Stable per-user URL + href swap (failed)
**Approach**: Add `/manifest/<share_key>.json` backend route. After login, call `GET /api/manifest` to get the user's `share_key`, then set `<link rel="manifest">` href to `/manifest/<share_key>.json`. Intercept `beforeinstallprompt` and defer it until after login so new installs use the per-user URL.  
**Why it failed**: For the already-installed PWA, Chrome continues to check `/manifest.json` (the install-time URL), not the current DOM href. Server logs confirmed `/manifest/<share_key>.json` was never fetched by Chrome for the installed app.

### 4. Service Worker + IndexedDB proxy (failed)
**Approach**: SW intercepts `/manifest.json` fetch events. After login, `app.js` writes `share_key` to IndexedDB (`noterai-sw` db). SW reads the key and proxies `/manifest.json` → `/manifest/<share_key>.json`.  
**Why it failed**: TBD — Chrome's background manifest update check may not go through the SW in all cases (e.g. when the app is not open), or there may be a SW activation/caching issue preventing the new SW from handling the fetch.

---

## What Has NOT Been Tried

- **`beforeinstallprompt` + fresh install after SW approach**: The new SW may work correctly for a brand new install (not an update to an existing install). Worth testing by fully uninstalling the PWA and reinstalling while logged in.
- **Forcing SW update via `skipWaiting`**: The new SW may not have activated on the device yet. Force-updating via DevTools or by bumping `APP_VERSION` might unblock it.
- **`navigator.serviceWorker.controller` check**: Verify the SW is actually controlling the page before concluding the SW approach failed.
- **Chrome flags / manifest update trigger**: `chrome://flags/#update-pwa-dialog-on-name-change` or visiting the app while connected to force a manifest re-check.
- **Using a `ServiceWorkerRegistration.update()` call** from `app.js` after login to force the SW to re-activate with the new manifest.

---

## Current State

Backend routes:
- `GET /manifest.json` — base manifest, no `share_target`
- `GET /manifest/<share_key>.json` — per-user manifest with `share_target`
- `GET /api/manifest` — returns `{share_key}` to authenticated frontend

Frontend:
- `updatePwaManifest()` writes `share_key` to IndexedDB after login
- `logout()` clears `share_key` from IndexedDB
- SW intercepts `/manifest.json` and proxies to per-user URL if IndexedDB has a key
