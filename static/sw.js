// Self-destructing worker. VERSION is rewritten by the server from the newest
// mtime under static/, which is what makes an already-installed worker fetch
// this file and replace itself with it.
//
// The app no longer uses a service worker. This exists only to remove the ones
// that are already installed: it takes control immediately, drops every cache,
// unregisters itself, and reloads its clients onto the network. Once no client
// has a registration left, it never runs again.
const VERSION = 'claude-pwa-v1';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    await Promise.all((await caches.keys()).map(key => caches.delete(key)));
    await self.registration.unregister();
    for (const client of await self.clients.matchAll({ type: 'window' })) {
      client.navigate(client.url).catch(() => {});
    }
  })());
});

// No fetch handler: every request goes to the network untouched.
