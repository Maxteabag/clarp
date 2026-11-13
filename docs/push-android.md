# Android push (FCM)

A Clarp Host pushes to the Clarp Android app through Firebase Cloud
Messaging (FCM HTTP v1). The Host decides what deserves a push exactly as it
does for iPhones (finished turns, decisions, agent ring alerts, background
sync); only the transport differs. iPhones keep the paths in
[push-relay.md](push-relay.md).

Phase 1, described here, is the **direct path**: the Host signs with its own
Google service account. A relay path with phone-issued grants (Play
Integrity, the Android counterpart of [push grants](push-relay.md#phone-issued-push-grants))
is a later phase; see [Phase 2](#phase-2-relay-with-play-integrity-grants).

## Configuration

```toml
[fcm]
service_account = "~/.config/clarp/fcm-service-account.json"
```

- The file is the JSON key of a Google service account in the Firebase
  project of the Android app (Firebase console > Project settings > Service
  accounts > Generate new private key). It needs permission to send FCM
  messages (`cloudmessaging.messages.create`, part of the Firebase Admin SDK
  service agent role the console's account already has).
- The project id, client email, key and token endpoint are read from the
  file. Nothing else is configured.
- `FCM_SERVICE_ACCOUNT` in the environment is used when the config has no
  path.
- The file is a secret; keep it out of git.
- `clarp-admin doctor` prints `OK    Android push: FCM direct (project …,
  …)` when the file reads as a service account, or `FAIL  Android push:`
  with the reason.

`[apns] background_sync = true` turns background sync on for Android as well
as iPhones. `[apns] mode` only affects iPhones; a Host with `mode = "off"` and
a service account pushes to Android phones only.

## Capability

The Host advertises feature **`push_fcm`** (Host contract **88**) in
`GET /server-info` (`capabilities.features` and `contract.features`) only
while the service account file exists and reads as a Google service account.
Only the running Host answers it: a script or `clarp-admin` that reads
`/server-info` data in-process never claims `push_fcm`. The app registers its token only with Hosts that advertise it. Whether
Google accepts the account is known at the first send, not before.

## Registration

The app sends its FCM registration token on `POST /devices`, authenticated
with its paired-device credential, at launch and whenever FCM rotates the
token (`onNewToken`):

```json
{
  "token": "<FCM registration token>",
  "platform": "android",
  "provider": "fcm",
  "session": "<focused session, optional>",
  "base_url": "<the Host origin the app uses, optional>"
}
```

- `provider` defaults to `"fcm"` when `platform` is `"android"` (and to
  `"apns"` for every other platform), so it may be left out; send it anyway.
- `token` must look like an FCM token: 20 to 4096 characters of
  `A-Z a-z 0-9 _ - :`. Anything else, or an unknown `provider`, answers
  `400 {"ok": false, "error": "device_token_invalid"}` and writes nothing.
- `base_url` is the origin the phone reaches this Host on (HTTPS, or HTTP on
  a private, link-local, CGNAT/Tailscale or `.local` address). The Host uses
  it to build `avatar_url`. `environment` is accepted and stored but means
  nothing for FCM.
- Answer: `200 {"ok": true}`.
- The row records `provider = "fcm"` and the paired device that registered
  it (the authenticated `device_…` principal; never a body field).
- Re-registering the same token is idempotent and re-enables a token the
  Host had disabled.
- A new token from the same paired device disables that device's previous
  Android token, so a rotation never pushes twice. Another phone's token
  is never touched.
- A token another paired device already registered answers
  `409 {"ok": false, "error": "device_token_owned_elsewhere"}`. That
  includes the same phone paired again (a new device id) while its earlier
  pairing is still valid. Once the earlier pairing is revoked, the phone's
  new pairing takes the token over on its next registration.
- A token is registered for one push service for good. Registering a token
  the Host already holds as an APNs row (an iPhone's, or an unbound legacy
  row) as Android/FCM, or an FCM token as anything else, answers
  `400 {"ok": false, "error": "device_token_invalid"}` and changes nothing.
- A registration made with the administrator token (not a paired device)
  is stored unbound, as for iPhones; it rotates only by `session`. An old
  token FCM has dropped is disabled at the next push (below).

iOS registration is unchanged.

## Unregistering and revocation

Pushes to a token stop through the Host, never by editing its database
(feature `push_unregister`, Host contract 93):

- **The phone itself**: `DELETE /devices` with `{"token": "<its token>"}`,
  authenticated as the paired device that registered it. Answers
  `200 {"ok": true, "disabled": 1}`, or `404 device_token_not_found` when
  that device has no live row for the token. Send it when the user signs out
  of the Host or turns notifications off. Registering again re-enables it.
- **The administrator**: `DELETE /devices` with exactly one of `{"token"}`,
  `{"token_prefix": "<12+ characters of one live token>"}` or
  `{"device_id": "device_…"}`; `GET /devices/push-tokens` lists every row
  with a 12-character prefix, never the full token. On the Host:
  `clarp-admin push list` and `clarp-admin push disable --token-prefix …` or
  `--device device_…`.
- **Revoking a pairing** (`clarp-admin pair revoke`, or removing the phone in
  the app) disables every push row of that device at once, iPhone and
  Android alike. Pushes also check the owner when they are sent, so a row
  that outlived a revocation (from an older release) or belongs to a device
  that is no longer full-scope gets nothing. Unbound rows (registered with
  the administrator token, or from before device binding) cannot be
  attributed and keep their pushes; remove them with `clarp-admin push
  disable`.
- A paired full-scope device is trusted: one that knows a revoked phone's
  token can register it under its own pairing, and pushes to that token
  resume.

## The message

Every push is a **data-only** message (no `notification` block), so the app
always receives it in `FirebaseMessagingService.onMessageReceived` and draws
the notification itself:

```json
{
  "message": {
    "token": "<registration token>",
    "data": { "...": "strings only, listed below" },
    "android": { "priority": "HIGH" }
  }
}
```

- `android.priority` is `HIGH` for every alert (finished turn, decision,
  ring) and `NORMAL` for background sync.
- `android.collapse_key` is set only for background sync
  (`sync-<session>`), so a phone that was offline gets the newest hint.
- No TTL is set (FCM keeps a message up to four weeks); a ring carries
  `expires_at` for the app to check.

### Data keys

Every value is a string. Booleans are `"true"` / `"false"`, numbers are
decimal strings, and a key whose value is unknown is left out. The encoded
map is kept within 4096 bytes: if it would be larger, the free text is
shortened with a trailing `…`, in this order: `body`, `opening_line`,
`subtitle`, `title` (and a ring's `reason`). Identifiers are never cut; a
push whose identifiers alone exceed the limit is not sent and is logged
(`fcmPayloadTooLarge`).

On every message:

| Key | Meaning |
|---|---|
| `payload_version` | `"1"`. Bumped only if a key changes meaning; new keys do not bump it. |
| `kind` | `user-notification` (a finished turn or a decision), `agent-ring`, or `sync`. |
| `session` | The conversation (agent session slug) to open. |
| `agent_id` | The agent's id, when known. |
| `collapse_id` | Tag for the drawn notification: a later push with the same `collapse_id` replaces it (a retried turn, a ring that became "missed"). |
| `sent_at` | When the Host sent it, milliseconds since the epoch. |
| `server_instance_id` | This Host's `server_id`, for an app paired with several Hosts (not on `sync`). |

A finished turn (`kind = user-notification`):

| Key | Meaning |
|---|---|
| `title` | The agent's name. |
| `body` | Preview of the reply. |
| `persona` | The agent's name (as `title`). |
| `notification_id` | Stable id of this notification (`pn-…`); retries of the same turn carry the same id. Dedupe on it. Equals `collapse_id`. |
| `needs_response` | `"true"` when the reply leaves a question or approval open. |
| `badge`, `unread` | The Host's verdict: badge the app icon / mark the conversation unread. |
| `interruption_level` | `active` (ordinary message; respect Do Not Disturb). |
| `sound` | `default` only when `needs_response` is `"true"`; absent means silent. |
| `thread_id` | Grouping key (the session). |
| `avatar_url` | Absolute URL of the agent's avatar on this Host, when one is reachable; `avatar_custom` is `"true"` for a generated portrait. |

A decision (`kind = user-notification`, `reason = decision`) has the keys of
a finished turn except `badge` and `unread`, plus:

| Key | Meaning |
|---|---|
| `reason` | `decision`. |
| `decision_id`, `artifact_id` | The request to show; the card is pinned in the conversation. |
| `notification_id` | `decision-<decision_id>`. |
| `body` | The question; `subtitle` is the request's title when it differs. |
| `interruption_level` | `time-sensitive` when the agent is blocked on a time-sensitive request, else `active`. |
| `sound` | Always `default`. `needs_response` is always `"true"`. |
| `category`, `response_type`, `revision`, `expires_at` | For a text-input request: `input-request`, `text_input`, the revision, and the expiry (ms) when set. |

An agent ring (`kind = agent-ring`):

| Key | Meaning |
|---|---|
| `title` | `Mike is calling`, `Missed call from Mike`, `Declined call from Mike`, `Call with Mike`, `Mike hung up`. |
| `body` | `Incoming call`, or the reason when the owner allows showing it. |
| `ring_id`, `ring_state`, `persona`, `reason`, `opening_line`, `opening_audio_url`, `expires_at` | As in the `agent-ring` SSE event ([agent-rings.md](agent-rings.md)). `conversation_mode` on an agent-live ring. |
| `notification_id` | `ring-<ring_id>`, also the `collapse_id`. |
| `interruption_level` | `time-sensitive` for an incoming ring, `active` for missed, `passive` otherwise. |
| `sound`, `category` | `default` and `agent-call` on an incoming ring. |
| `thread_id` | `ring-<session>`. |
| `avatar_url`, `avatar_custom` | As above. |

Background sync (`kind = sync`, only with `[apns] background_sync = true`):
`session` and `agent_id`; no alert. Budgeted like iPhones' (three an hour,
ten minutes apart per session).

## Agents working (feature `live_activity_fcm`, Host contract 95)

The iPhone keeps its "agents working" Live Activity current by ActivityKit
push (docs/live-items.md §8). An Android phone gets the same content state by
FCM, on its own FCM push row, at the same moments. The feature is advertised
only while the Host has a usable service account and live events flow (as
`live_activity_push`).

```
POST   /devices/live-activity  {"platform": "fcm", "kind": "agents-working"}
                               -> 200 {"ok": true, "platform": "fcm", "kind": "agents-working"}
DELETE /devices/live-activity  {"platform": "fcm"}   (or POST with "ended": true)
                               -> 200 {"ok": true, "removed": 1 | 0}
```

- Only a paired, full-scope, unrevoked device asks, for itself (the
  administrator and a limited device: `403`). It must have registered its FCM
  token first (`POST /devices`), else `409 fcm_push_token_required`. `kind` is
  `agents-working` (left out: the same); another `400`. One registration per
  device; asking again is idempotent.
- The registration records the device; the messages go to that device's live
  FCM push rows. So a rotated token is followed without asking again, and
  `DELETE /devices`, a token taken over by another pairing (the new pairing
  asks for itself), revocation and a limited scope stop them, by the push
  row's rules above.

The message is data-only, `android.collapse_key` `agents-working` (FCM keeps
only the latest for an offline phone):

| Key | Meaning |
|---|---|
| `kind` | `agents-working`. |
| `event` | `update`, or `end` for the quiet state (`working` 0 and `needs_you` 0). |
| `state` | The APNs push's `content-state` as one JSON string: `working`, `needs_you`, `decisions`, `lead`, `agents`, `updated_at_ms` (docs/live-items.md §8). |
| `payload_version`, `server_instance_id`, `collapse_id` (`agents-working`), `sent_at` | As on every message. |

- **Pace**: the Live Activity pusher's: routine changes at most every 15 s, a
  structural change (an agent starting or ending work, a new lead) at least
  10 s after the last push, a rising `needs_you` at once. Latest wins all the
  way to Google: the pusher only hands the newest state over, one worker
  sends, and each device holds at most one state. After a hung Google only
  the newest goes out, never a backlog of old ones.
- **Priority**: an update is `HIGH` for a structural change or a rising
  `needs_you` (it changes what the notification shows), `NORMAL` otherwise. A
  state that replaced a `HIGH` one before it went out stays `HIGH`. An end is
  always `NORMAL`. Google's page on message priority
  (firebase.google.com/docs/cloud-messaging/android-message-priority, read on
  13 Nov 2026) says: "High priority messages on Android are meant for time
  sensitive, user visible content", and "If FCM detects a pattern in which
  messages don't result in user-facing notifications, your messages may be
  deprioritized to normal priority". It does not say whether a message that
  removes a notification counts as user-visible, so the end never risks the
  app's rings and alerts. While the phone is in Doze a `NORMAL` end may arrive
  late. The notification's own 15-minute timeout still applies.
- **Budget**: per device, counted apart from the iPhone's, and kept in the
  database (`live_activity_fcm.budget.v1`), so a Host restart neither refills
  nor forgets it. Updates use a burst of 10 refilled evenly to 300 a day, and
  at most 300 a UTC day per device. An end takes nothing from the burst and
  goes out even when the day's updates are spent. It is counted, and there is
  at most one end per update, so a day stays within about twice the budget.
  Over 24 hours one phone gets (updates/ends): 309/0 for 200 agents that are
  never quiet, 96/96 for a 2-minute turn every 15 minutes, 153/153 for a
  1-minute turn every 5 minutes, 158/158 for a 40 s turn every 2 minutes, and
  163/163 for start and stop every 10 s.
- **Failures**: a send Google does not take (a 5xx, a 429 after the
  transport's own retries, a network error, no access token) keeps that state
  owed. It is retried after 60 s, doubling to 30 min, until it is delivered or
  a newer state replaces it. A delivered message resets the back-off. A token
  Google reports gone is disabled.
- **The end**: when the fleet goes quiet the quiet state goes out with
  `event: "end"`, but only to a device that was sent an update since its last
  end. A suspended Android app cannot end its notification itself, and a
  phone that was never shown the work needs no end.
- **A Host start**: a device whose last message was an update may still show
  agents at work. If no fleet change is offered within 30 s of the start, it
  gets the present state: an end when nothing works.
- **Start of a stretch**: there is no start push. The first message of a
  working stretch is an ordinary `update`, and the app starts its notification
  from it.
- **Revocation** removes the device's request and its budget.

## Delivery and failures

Sending happens on the same background threads, in the same batch and under
the same policy as APNs: per-agent mute, quiet agents and an active desktop
suppress Android pushes exactly as they suppress iPhone pushes, and a push
never raises into a turn.

A batch sends to the iPhones first, under the conversation's send lock
(which keeps two notifications of one agent in order on iPhones). The
Android phones follow after that lock is released, in the agent's Android
lane. Every attempt to reach Google (the token exchange, each send) has a
total deadline of 10 s, covering connect, TLS, writing and every read, so a
Google that answers a byte at a time is cut off like a silent one. One
Android send therefore takes at most about 45 s (three attempts, the 1 s and
2 s back-off, a token exchange), and that wait never holds the same agent's
next iPhone push. Android sends of one agent go out one at a time, first in,
first out. While Google answers, nothing is dropped. Only when the send in
progress has been running longer than 5 s is the queue behind it held to two:
the oldest ordinary alert is dropped first, a decision only when no ordinary
alert is left to drop, and the agent's newest waiting decision never. A drop
is logged as `apnsAndroidDropped … reason=google-slow` and counted as failed;
its iPhone push has already gone.

- **Auth.** The Host signs an RS256 JWT with the service account key
  (`iss` the client email, `scope`
  `https://www.googleapis.com/auth/firebase.messaging`, `aud` the token
  endpoint, one hour), exchanges it for an access token and caches that
  until five minutes before it expires. A `401` from FCM mints a new token
  once.
- **`UNREGISTERED`** (or a bare `404`): the token is disabled. The app
  re-enables it by registering again.
- **`400 INVALID_ARGUMENT`**: Google's error-code page
  (firebase.google.com/docs/cloud-messaging/error-codes) shows an invalid
  registration token as a `400 INVALID_ARGUMENT` with only the `FcmError`
  detail, and an invalid value in a data message as a `400 INVALID_ARGUMENT`
  whose `google.rpc.BadRequest` names the field (`message.data[0].value`). So
  a `400 INVALID_ARGUMENT` that names no field other than `message.token`
  disables the token, and one naming another field (the payload) keeps it and
  counts as a failed push. The message text is never read for this.
- **`QUOTA_EXCEEDED`, `429`, `5xx`, a network error**: up to three attempts,
  waiting 1 s then 2 s, or the `Retry-After` the server sends (capped at
  10 s). Then the push counts as failed and is logged; the token is kept. A
  retried request whose first attempt did reach Google can arrive twice:
  dedupe on `notification_id`.
- **A missing, unreadable or rejected service account**, or `403` from FCM
  for the account: logged once as `fcmAuthFail` with the reason (again
  only when the reason changes, and `fcmAuthRecovered` when sends work
  again). The push counts as failed; tokens are kept.
- **`SENDER_ID_MISMATCH`** (the token belongs to another Firebase project)
  and any other `INVALID_ARGUMENT` (about the message, not the token) are
  logged as `fcmSendReject` and count as failed; the token is kept.

Each send is logged like an APNs one (`apnsSendResult … via=fcm`).

## Rolling back to a Host before contract 88

Schema step 123 is additive, so an older release runs on the upgraded
database. But older code knows nothing of providers: it lists every live
row, Android rows included, as an APNs target. Its first push sends the FCM
token to Apple (or to the Audio Central relay). Apple answers
`BadDeviceToken` and the older code disables the row. Through the relay,
the token may instead be refused at binding and stay live, counted as a
failed push each time. The token string goes to Apple or the relay in either
case; nothing reaches the phone.

Nothing in an older release can be changed to prevent this. After rolling
forward again, each Android phone gets push back when it registers again:
the app does so on its next launch against a Host that advertises
`push_fcm`, which re-enables its row. To avoid sending FCM tokens to Apple
during a rollback, disable the Android rows first (set `disabled_at` on the
rows with `provider = 'fcm'`); the phones re-enable them the same way.

## Phase 2: relay with Play Integrity grants

Not built yet. A Host without its own service account would push through
Clarp Audio Central with a grant the phone issues after a Play Integrity
attestation, as iPhones do with App Attest. The device row already names its
provider (`device_tokens.provider = "fcm"`) beside the existing
`push_grant` column, so the relay path can be added as another route for
`fcm` rows without changing registration for phones that do not send a
grant.
