// Service worker removal.
//
// The app used to ship a caching service worker to get new JS onto the phone.
// It did the opposite often enough to not be worth it: a cached app shell
// serves stale JS after a deploy, and in the desktop shell it served a stale
// bundle for a whole session while the dev server sat there with the current
// one. Nothing registers a worker any more.
//
// This runs on every boot rather than only in dev, because a worker already
// installed on a phone or in a webview outlives the code that registered it.
// static/sw.js is now a self-destructing worker for the same reason: a client
// whose worker is still in control picks that up on its next update check and
// the worker removes itself.

export async function removeServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map(r => r.unregister().catch(() => {})));
  } catch (_) {}
  try {
    if ('caches' in globalThis) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k).catch(() => {})));
    }
  } catch (_) {}
}
