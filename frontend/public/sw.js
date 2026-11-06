/* VoiceCRM service worker - hand-written, no build tooling.
 *
 * Caches the app shell (index.html + the hashed /assets/* files it references) so the Capture screen opens and
 * records while offline. Recordings themselves are queued in IndexedDB by the page, not here.
 * API calls (/api/*) are NEVER cached: they can carry customer-related data (synthetic in this demo).
 * Registered from src/main.tsx in production builds only.
 */
const VERSION = 'v1';
const SHELL_CACHE = `voicecrm-shell-${VERSION}`;
const RUNTIME_CACHE = `voicecrm-runtime-${VERSION}`;

const SHELL_URLS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/favicon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      await Promise.all(SHELL_URLS.map((url) => cache.add(url).catch(() => undefined)));
      try {
        // Hashed build assets are only known from the built index.html, so discover them at install time.
        const response = await fetch('/index.html', { cache: 'no-cache' });
        const html = await response.text();
        const assets = [...html.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((match) => match[1]);
        await Promise.all(assets.map((url) => cache.add(url).catch(() => undefined)));
      } catch {
        // Installing while offline is fine: the runtime cache fills as pages are visited.
      }
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL_CACHE, RUNTIME_CACHE]);
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name.startsWith('voicecrm-') && !keep.has(name)).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

async function navigationResponse(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const fresh = await fetch(request);
    // Single-page app: every route is served by index.html, so keep one copy of the shell.
    if (fresh.ok && fresh.headers.get('content-type')?.includes('text/html')) {
      cache.put('/index.html', fresh.clone());
    }
    return fresh;
  } catch {
    const cached = (await cache.match('/index.html')) || (await cache.match('/'));
    if (cached) return cached;
    return new Response('VoiceCRM is offline and the app shell is not cached yet. Reconnect once to enable offline use.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(RUNTIME_CACHE);
    cache.put(request, response.clone());
  }
  return response;
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);
  const refresh = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => undefined);
  return cached || (await refresh) || Response.error();
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(navigationResponse(request));
    return;
  }
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(request));
    return;
  }
  event.respondWith(staleWhileRevalidate(request));
});
