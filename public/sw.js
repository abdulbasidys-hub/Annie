/**
 * Service worker — just enough to be installable, deliberately not more.
 *
 * The temptation with a PWA is to cache API responses so it opens instantly
 * offline. That would be actively harmful here: every screen in this app is
 * a claim about a market that moves in minutes, and a cached "nothing
 * crossed a tier" from four hours ago is indistinguishable from a live one.
 * Annie has already lost a day to a message that looked current and was not.
 *
 * So: the app shell is cached so it launches from the home screen without a
 * blank white page, and API calls are never cached. Offline, you get an
 * honest failure instead of stale intelligence.
 */

const SHELL = 'annie-shell-v1'

// Only what is needed to paint something. Hashed build assets are handled by
// the fetch handler rather than listed, since their names change every build.
const SHELL_URLS = ['/', '/index.html', '/Annie.jpg', '/manifest.webmanifest']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return

  const url = new URL(request.url)

  // Never touch the API. Cross-origin too — the API is on another host, and
  // a cached brief is worse than no brief.
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) return

  // Navigations: try the network, fall back to the cached shell so a home
  // screen launch on a bad connection still renders the app rather than a
  // browser error page. React Router takes it from there.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/index.html').then((r) => r || Response.error())),
    )
    return
  }

  // Build assets are content-hashed, so a cache hit is always correct.
  event.respondWith(
    caches.match(request).then(
      (hit) =>
        hit ||
        fetch(request).then((response) => {
          if (response.ok && (url.pathname.startsWith('/assets/') || SHELL_URLS.includes(url.pathname))) {
            const copy = response.clone()
            caches.open(SHELL).then((cache) => cache.put(request, copy))
          }
          return response
        }),
    ),
  )
})
