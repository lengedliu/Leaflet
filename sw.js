const CACHE = 'pages-between-shell-v13';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg', '/cover-placeholder.svg'];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(SHELL)).catch(() => {})
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => 
      Promise.all(keys.filter(key => key !== CACHE && key !== 'pages-offline-books').map(key => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;

  // Book reader & media endpoints support offline cache fallback
  if (url.pathname.startsWith('/api/read') || url.pathname.startsWith('/api/epub-chapter') || url.pathname.startsWith('/api/epub-toc') || url.pathname.startsWith('/cover')) {
    event.respondWith(
      fetch(event.request).then(response => {
        if (response.ok && url.origin === self.location.origin) {
          const clone = response.clone();
          caches.open('pages-offline-books').then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(async () => {
        const offlineCache = await caches.open('pages-offline-books');
        const match = await offlineCache.match(event.request);
        if (match) return match;
        const generalMatch = await caches.match(event.request);
        if (generalMatch) return generalMatch;
        return new Response('离线内容暂不可用', { status: 503, statusText: 'Offline Unavailable' });
      })
    );
    return;
  }

  // Skip other mutating or private API requests
  if (url.pathname.startsWith('/api/')) return;

  // HTML navigation requests: always network-first to get freshest page
  if (event.request.mode === 'navigate' || event.request.destination === 'document' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(event.request).then(response => {
        if (response.ok && url.origin === self.location.origin) {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => caches.match(event.request).then(cached => cached || caches.match('/')))
    );
    return;
  }

  // Other assets: cache fallback to network
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        if (response.ok && url.origin === self.location.origin) {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});
