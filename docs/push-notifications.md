# Push notifications (tracked — not yet built)

Follow-up to the auto-reconnect / `interrupted` work. Today, when a turn is
cut short and not recovered, the agent flips to `AgentState.INTERRUPTED` and
the PWA + native app show a badge. That only notifies you **if the app is
open**. Real push would reach the device when it isn't.

This is deliberately deferred — it's a separate, larger project. Notes so the
shape is captured.

## What "notify" means today

`turn_dispatch._mark_interrupted()` records an `INTERRUPTED` state with a
`reason` (`connection` / `transient` / `interrupted`) and a human `message`.
That drives:

- PWA: `bannerFor()` in `web/src/stores/app.svelte.js`, rendered by
  `web/src/lib/AgentBanner.svelte` (+ `.agent-banner.interrupted`)
- Native: `AgentStatePill`, `AgentAvatar`, `AppEventReducer` (`interrupted` case)

So the server already has the single choke point (`_mark_interrupted`) where a
push send would hang off.

## What real push needs

Two independent transports, both fed from `_mark_interrupted`:

### iOS native — APNs
- Apple Developer APNs auth key (`.p8`) + key id + team id, stored as server secrets.
- `UNUserNotificationCenter` permission request + `registerForRemoteNotifications`
  in the native app; POST the device token to a new `/devices` endpoint.
- Server: store tokens per agent/owner; on interrupt, send an APNs request
  (e.g. via `aioapns` or a tiny JWT + HTTP/2 client).

### PWA — Web Push (VAPID)
- Generate a VAPID keypair; expose the public key to the client.
- Service worker (`static/sw.js`) already exists — add a `push` event handler
  + `pushManager.subscribe()` from `web/src/lib/sw.js`; POST the subscription
  to `/devices`.
- Server: `pywebpush` (or hand-rolled) to send to the subscription on interrupt.

## Decisions to make before building
- Which events push, beyond `interrupted`? (Likely `waiting` too — a permission
  prompt is the other "needs you" state.)
- Dedup / rate-limit: don't fire three pushes if 3 retries each notify.
- Per-owner routing: which device(s) belong to which agent/session.
