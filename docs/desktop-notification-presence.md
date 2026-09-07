# Desktop activity and mobile message alerts

The desktop setting **Pause phone alerts while active on desktop** defaults on.
It suppresses Clarp's iOS message alerts while the desktop window is foreground,
the current Linux logind session is active/unlocked, and input or activation
occurred within two minutes. Minimizing, losing focus, locking, sleep, disabling
the setting, or losing the Host connection releases activity. Resume requires
fresh interaction. This does not mute desktop alerts or change unread state.

A desktop process has a random instance UUID and increasing sequence counter.
It renews active presence every ten seconds via authenticated full-access
`POST /desktop-presence`, with `instance_id`, `sequence`, boolean `active`, and `sent_at_ms`. Reports older than
45 seconds or more than five seconds in the future are rejected. Lease expiry
is capped relative to sending time; a delayed packet cannot renew an old activity
window. The desktop request has a five-second transfer timeout.
The Host fixes expiry at 45 seconds; clients cannot select their own TTL.
Inactive reports retain a sequence tombstone so a delayed active request cannot
reverse a release. Keys are bound to the authenticated principal and instance.
Multiple desktops retain independent leases; releasing one cannot clear another.
Limited or unauthenticated clients cannot publish presence. Revoked paired
devices no longer suppress alerts.

Leases live in the existing SQLite settings table under `desktop-presence:`.
This is shared across HTTP/runtime/notification processes and needs no migration.
Lease entries are capped at 256 and stale tombstones are pruned after five
minutes. Missing, expired, malformed, future-clock or unknown-device leases do
not suppress. App crashes and failed release requests recover through expiry.

The APNs transport checks presence before preparing an alert and again before
each token send. Notification classification, unread counts, SSE and transcript
sync remain unchanged. Suppressed alerts are not queued for later replay: future
messages notify normally once the user is away. A push already sent to APNs
cannot be recalled by this mechanism. Calls and other notification transports
are outside this message-alert policy.

The Linux monitor uses asynchronous login1 GetSession/GetUser Display discovery,
Active/LockedHint updates and PrepareForSleep. Query generations fence stale
responses. Unknown or unsupported session/lock monitoring fails open (phone
alerts continue); this implementation does not claim macOS/Windows monitoring.
Screenshots never enable real desktop presence. Presence failures on older Hosts
are non-disruptive and expire naturally; matching Host and desktop deployments
are required for suppression.

Verification (from repo root):

```sh
uv run pytest tests/unit/test_desktop_presence.py tests/unit/test_apns.py \
  tests/integration/test_desktop_presence_endpoint.py -q -n 2
QT_FORCE_STDERR_LOGGING=1 ctest --test-dir desktop/build/release --output-on-failure
cmake --build desktop/build/release --target all_qmllint
QT_QPA_PLATFORM=offscreen QT_FORCE_STDERR_LOGGING=1 \
  desktop/build/release/tests/clarp-desktop-presence-tests --probe-session
```

The probe only reads session state and never publishes presence or changes the
visible desktop. The CTest lane injects state into an isolated offscreen window;
APNs tests use a fake HTTP transport. Do not send real phone pushes merely to
exercise this policy. Verify a deployed Host endpoint with a fresh instance and
an inactive report, so verification itself cannot silence the user's phone.
