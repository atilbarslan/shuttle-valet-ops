// Service worker for the driver and valet PWAs (sofor.html, vale.html).
// API calls always go to the network; app files are served cache-first.

// Bump this on every deploy that changes any front-end file. Cached app files are only
// replaced when the version changes (the old cache is deleted on activate), so without a
// bump the PWAs keep running old code. The staff panels have no service worker but read
// this value too (js/surum-kontrol.js) to show a "new version" banner, so a missing bump
// also leaves open panel tabs unaware of the deploy. The driver screen displays it as the
// app version.
const CACHE_VERSION = 'app-v1';

// Files cached at install time so the apps open even when offline.
const CACHE_ASSETS = [
    '/sofor.html',
    '/vale.html',
    '/js/sofor.js',
    '/js/vale.js',
    '/libs/purify.min.js',
    '/libs/purify.min.js.map',
    '/config.js',
    '/fonts/InterVariable.woff2',
    '/manifest.json',
    '/icon-192.png',
    '/icon-512.png',
];

// Install: pre-cache the files above.
// `cache: 'reload'` is required. By default cache.addAll() goes through the browser's own
// HTTP cache, which may hand back an old copy of a file. The new service worker would then
// store that old file under the new version, and since app files are served cache-first it
// would stay in use until the next version: the version number says "up to date" while the
// code is old. `cache: 'reload'` bypasses the HTTP cache and fetches from the network. The
// server also sends `Cache-Control: no-cache` for code files, but keep both guards. If this
// is removed the bug comes back silently, as "I deployed but nothing changed".
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_VERSION)
            .then((cache) => cache.addAll(
                CACHE_ASSETS.map((url) => new Request(url, { cache: 'reload' }))
            ))
            .catch((err) => console.warn('Cache pre-load hatası:', err))
    );
    self.skipWaiting();
});

// Activate: delete caches from older versions.
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((isimler) => Promise.all(
                isimler
                    .filter((isim) => isim !== CACHE_VERSION)
                    .map((isim) => caches.delete(isim))
            ))
            .then(() => clients.claim())
    );
});

// Fetch: decide between network and cache.
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Leave cross-origin requests (Mapbox tiles, styles, glyphs, workers) to the browser.
    // Handling them cache-first could leave map tiles blank. Only our own origin (app files
    // and API) is managed here.
    if (url.origin !== self.location.origin) return;

    // Never cache writes (POST/PUT/DELETE).
    if (event.request.method !== 'GET') {
        event.respondWith(
            fetch(event.request).catch(() => 
                yeni_offline_cevap('İşlem çevrimdışıyken yapılamaz.')
            )
        );
        return;
    }
    
    // Network only: API endpoints and a few files that must never be served stale.
    // API routes are not under a common prefix, so they are listed by name. Any new endpoint
    // whose GET response changes over time must be added here, or it will be served from
    // the cache.
    // /sw.js is on the list because js/surum-kontrol.js reads the version with fetch('/sw.js').
    // That request goes through this handler; if it were cached, the version would never seem
    // to change and the "new version" banner would never appear. (The browser's own update
    // check for the service worker script is a separate path and is not affected.)
    // The invitation page (sifre.html, js/sifre.js) is network only as well. A stale copy
    // would submit an outdated form, the server would reject it and the person could not see
    // why. The flow needs the network anyway, so caching it only adds risk.
    if (url.pathname.startsWith('/api/') ||
        url.pathname.match(/^\/(sw\.js|sifre|js\/sifre\.js|sofor-|aktif-talepler|yolcu-|rota-|arac-|firma-|sube-|admin\/|talep-detay|canli-sira|vale-|riza-)/)) {
        event.respondWith(
            fetch(event.request).catch(() => 
                yeni_offline_cevap('Şu an çevrimdışısınız. İşlem bağlandığınızda gönderilecek.')
            )
        );
        return;
    }
    
    // App files: cache first. A cached file is returned at once without touching the network,
    // which keeps the apps fast and usable offline. Freshness comes from bumping CACHE_VERSION:
    // the new version's activate step deletes the old cache and files are fetched once again.
    event.respondWith(
        caches.match(event.request).then((cachedResponse) => {
            if (cachedResponse) return cachedResponse;
            return fetch(event.request)
                .then((networkResponse) => {
                    if (networkResponse && networkResponse.status === 200) {
                        const responseClone = networkResponse.clone();
                        caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, responseClone));
                    }
                    return networkResponse;
                })
                .catch(() => yeni_offline_cevap('Bu dosya çevrimdışı kullanılamıyor.'));
        })
    );
});

// Plain-text 503 returned when the network is unavailable.
function yeni_offline_cevap(mesaj) {
    return new Response(mesaj, {
        status: 503,
        statusText: 'Service Unavailable',
        headers: new Headers({ 'Content-Type': 'text/plain; charset=utf-8' })
    });
}

// Messages from the pages.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
    // A page can ask for the active version to display it; CACHE_VERSION is the single source.
    if (event.data && event.data.type === 'GET_VERSION' && event.ports && event.ports[0]) {
        event.ports[0].postMessage({ version: CACHE_VERSION });
    }
});