# Push relay

The server is open source; the Apple Push Notification service (APNs) key for
the Clarp app is not. A self-hosted Computer therefore does not sign pushes
itself. It hands them to **Clarp Audio Central**, the developer-run Cloudflare
Worker that already provides managed voice, and the Worker signs and delivers
them with a key held only as a Worker secret.

## Phone-issued push grants

A Host needs neither an APNs key nor an Audio Central account to push. The
phone attests itself to Audio Central with Apple App Attest and issues a
**push grant** for each Host it pairs with. It sends the grant to the Host
with its device registration (`POST /devices`, field `push_grant`), and the
Host pushes to that phone by calling the relay with the grant. The grant
names the phone, so no device token or credential of the Host's is involved.

- A device row with a grant always uses it, whatever else is configured
  (except `mode = "direct"` or `"off"`).
- A grant the phone revoked (the user removed this Host in the app) makes
  the relay answer 401. The Host forgets the grant and uses its own path
  for that token if it has one (credential relay or local key); otherwise
  it disables the token.
- Re-registering a token with a grant retires any other row holding the
  same grant, so a rotated token does not push twice.
- `409 device_token_missing` means Apple retired the phone's token; the
  phone registers a new one on its next launch, so the row is kept.
- Re-registering without `push_grant` keeps the stored grant, so older app
  builds and phones without App Attest keep working through the other paths.
- The Host advertises feature `push_grants` (Host contract 39); the app only
  mints grants for Hosts that do.

### Threat model (Host side)

- **A stolen grant** lets its holder push Clarp notifications to that one
  phone, within the relay's per-grant and per-device rate limits, until the
  user unpairs the Host or the Host drops it. It does not reveal the phone's
  token, other phones, or anything on this Host. Grants are stored in the
  Host database like the device tokens beside them; protect the database as
  you protect the pairing tokens in it.
- **A leaked device token** gives nothing through grants: the relay never
  pushes to a raw token on a grant request.
- **The relay endpoint alone** gives nothing: pushing needs a grant (made by
  a genuine Clarp app on the phone) or a Computer credential.

## Which path a Computer uses

`[apns] mode` decides, and `clarp-admin doctor` reports the result:

| mode | Behaviour | doctor |
|---|---|---|
| `auto` (default) | Phone grants where present; other devices relay when `[audio_central] credential` is set, else direct when a `.p8` is configured | `push: relay via Audio Central (…)`, `push: direct (local APNs key)`, or `push: phone grants via Audio Central` |
| `relay` | Relay only (grants and credential); never touches a local key | `FAIL` when the url is not https |
| `direct` | Local `.p8` only (development) | `FAIL` when the key is missing |
| `off` | No pushes | `push: off` |

In `auto`, a Computer that has both a credential and a `.p8` relays, and falls
back to the `.p8` for a batch only when the relay path is down: Audio Central
unreachable, HTTP 5xx (including `push_unconfigured`), an older Worker
without the relay route, or the relay reporting it could not reach Apple
(`RelayDeliveryFailed`). Apple rejecting one token never triggers the
fallback. Forced `relay` never falls back. After one outage the rest of the
batch skips the relay instead of waiting out the timeout again.

## How relaying works

- Authentication is the Computer's existing Audio Central credential
  (`cav1.<account>.<key-id>.<secret>`), sent as a bearer token.
- Before the first push to a device token, the server binds it to this
  Computer (`POST /v1/push/devices`) with the environment the phone reported
  (`production` or `sandbox`). The relay only delivers to bound tokens and
  picks the APNs host from the binding. Bindings are cached per process;
  after a restart each token is re-bound once. If the relay has dropped a
  binding, the server binds again and retries once.
- Each push is `POST /v1/push/send` with the same payload direct mode sends
  (finished-turn alerts, decisions, and silent `background_sync` pushes with
  `apns-push-type: background` and priority 5). The relay passes Apple's
  status and reason back, so dead tokens are disabled exactly as in direct
  mode.
- The relay caps payloads at 4096 bytes, fixes the topic to the app's bundle
  id, and rate-limits each Computer (30 per minute, 2000 per day by default).
- Pushes stay best-effort: they run on a daemon thread with a 20-second HTTP
  timeout (longer than the Worker's 10-second Apple deadline, so a slow
  relay answers before a fallback could double-send), and every failure is logged and counted, never raised into a turn.

The relay contract is documented in the Audio Central repository
(`docs/push-relay.md`).

A Computer can only be claimed by an Audio Central account, which today
starts with a Clarp Voice purchase. A Computer with no account has no
credential, but still reaches every phone that issued it a push grant.

## Migrating a Computer off its local `.p8`

1. Make sure the Computer has an Audio Central credential: `[audio_central]
   credential = "cav1.…"` in `~/.config/clarp/config.toml`. The credential is
   what Audio Central's `POST /v1/computers/claim` returns for a claim code
   made in the Clarp app; this repository does not run that claim yet, so it
   is placed in the config by hand.
2. Restart the server, or wait for it to pick up the config change, and run
   `clarp-admin doctor`. It should print `push: relay via Audio Central` and
   note that the local `.p8` is kept as a fallback.
3. Finish a turn with the phone locked and confirm the push arrives. The
   server log shows `apnsSendResult … via=relay`.
4. Remove `key_path`, `key_id`, and `team_id` from `[apns]`, delete the
   `AuthKey_*.p8` file, and run `clarp-admin doctor` again: the fallback note
   disappears.

To stay on direct mode for development, set `[apns] mode = "direct"`.
