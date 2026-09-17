/*
 * Minimal service worker — only job is making the app shell (the static
 * HTML/CSS/icons, not live data) installable and available offline.
 *
 * Deliberately does NOT cache or intercept anything under /api/ or /ws/ —
 * conversation data, history, and auth must always hit the real backend.
 * Caching those would mean showing stale or wrong data, which is worse
 * than showing nothing. This is app-shell caching only, the standard
 * minimal PWA pattern, not an attempt at full offline conversation support
 * (that's real Phase 4 "offline degraded mode" work — a different,
 * bigger problem involving on-device ASR/MT models, not just caching).
 */

const CACHE_NAME = 'voxbuddy-shell-v3';
const SHELL_FILES = [
  '/app',
  '/static/style.css',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_FILES))
      .catch((err) => console.warn('VoxBuddy SW: shell caching failed', err))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) {
    return; // let these hit the network untouched, always
  }

  // Navigation requests (the /app HTML document itself) use network-first,
  // not cache-first. This is the actual bug fix: the previous version
  // cached /app once and then served that same stale HTML forever,
  // ignoring every subsequent code change on the server — exactly what
  // was seen live (phone-login-removal and Google Sign-In both existed
  // on the server but the browser never re-fetched the page to see them).
  // Cache is now only a fallback for genuinely offline use, never allowed
  // to shadow a live network response.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      // cache: 'no-store' bypasses the browser's own HTTP cache layer for
      // this fetch, not just the service worker's Cache Storage — without
      // this, "network-first" service worker logic can still receive a
      // stale response from the browser's disk cache underneath it, since
      // fetch() itself is cache-aware by default unless told not to be.
      // This was the actual second half of the stale-content bug: the SW
      // logic was already correct, but this wasn't, and the two combined
      // to still serve old content after a real deployment.
      fetch(event.request, { cache: 'no-store' })
        .then((response) => {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets (CSS, icons) still cache-first — these change rarely,
  // benefit from instant load, and CACHE_NAME bumps (like v1 -> v2 here)
  // still force a clean break from old cached copies when they do change.
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
