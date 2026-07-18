const CACHE_NAME = 'vende-facil-v16';
const CORE = ['/static/css/app.css', '/static/js/app.js', '/static/manifest.json'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(CORE)).catch(() => null));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(key => key !== CACHE_NAME ? caches.delete(key) : null))));
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  // Páginas do app precisam ser frescas: dashboard, matches, formulários etc.
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/static/manifest.json')));
    return;
  }

  // Arquivos estáticos podem usar cache.
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
      const copy = response.clone();
      if (event.request.url.includes('/static/')) {
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy)).catch(() => null);
      }
      return response;
    }))
  );
});
