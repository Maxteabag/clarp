# Host not answering: shared spec (all Clarp apps)

Owner: clarp-host-down. Peter, 28 Nov 2026: "Egen side når Host ikke svarer: Enig.
La oss fikse det." UX report idea 7 (Happy's offline page is the reference).
Applies to iOS, Android, desktop-rs and web.

**Language (clarp-i18n, 28 Nov):** follow /var/tmp/clarp-i18n.XUMGuw/CONVENTIONS.md.
Write every new string in English only, through the platform's normal mechanism
(iOS String Catalog / `String(localized:defaultValue:comment:)`, Android
`values/strings.xml`, Slint `@tr(...)` / the crate's tr helper, web
`web/src/lib/i18n/en.json` + `t()` or an English literal until infra lands), with
the semantic keys below. No Norwegian in code, no `if nb` branches. Never hand-format
dates, durations or counts: use the platform's (relative) date/duration formatter
with the app locale. Product names stay untranslated (Clarp, Host, Tailscale).
clarp-i18n translates afterwards and messages when each platform's catalog lands.
The reason is classified **on the client** (the Host is down, so it cannot send
one); the client keeps `reason` (code) and `down_since`/`last_ok` (epoch ms) and
renders words from its catalog.

## What the user must get

When a Host does not answer, the app shows a calm panel instead of spinners,
stale lists or raw error text, with:

1. which Host (its label);
2. since when ("Last answered 2 hours ago", platform relative formatter);
3. the most likely reason, one plain sentence (table below);
4. what the user can do: Try again, plus the reason-specific action, plus
   Switch Host when another Host is configured;
5. quiet automatic reconnect with a small status line (no spinner wall);
6. a «Kopier diagnose» / "Copy diagnostics" button that copies the short private-data-free text below.

## Multi-Host rule (the hard requirement)

- The down state lives **per Host** and is shown **only where that Host's content
  is**: that Host's chats, that Host's section/rows in a combined list, and that
  Host's settings row. Every other Host keeps working: lists, sends, streaming,
  images/media, avatars, notifications.
- Nothing about Host B may route through Host A. In particular, media/image/avatar
  URLs, auth tokens and proxies for a chat must come from the chat's own Host,
  never from the "primary"/first/selected Host. (desktop-rs today: the primary
  Host laptopstudio being offline broke image loading on EliteBook Host chats; fix
  that root cause.)
- A down Host never blocks app start, the chat list or switching chats. Requests to
  it have a short timeout and run off the main path.
- Cached content of the down Host stays readable, read-only: show saved messages,
  disable the composer (or keep the draft but disable send) with the read-only note.

## Where it shows

- **Chat of a down Host**: a compact panel at the top of the chat (title, reason,
  since, status line, actions); saved messages below; composer send disabled with
  the read-only note as its placeholder/hint.
- **Combined chat list (all Hosts)**: that Host's rows stay, visibly dimmed or with
  a small offline dot, plus one compact row/header
  `host_down.list_row` that opens the full panel. No per-row
  error text.
- **Only Host / selected-Host view with nothing cached**: the full page (same
  content, centered, larger).
- **Device has no network at all**: one app-wide quiet banner
  (`host_down.banner`) instead of N Host panels.

## Reasons (classifier)

Decide from the last failed attempt plus context. First match wins.

| id | detect | gist (exact text: see Strings) | extra action |
|---|---|---|---|
| `device_offline` | OS says no network | This device is offline. | (none; app-wide banner) |
| `auth_expired` | HTTP 401/403 from the Host | This device is no longer signed in to {Host}. | Pair again |
| `wrong_address` | DNS name not found, TLS name mismatch, or an HTTP answer that is not this Host (wrong host id / not Clarp / 404 on the health route) | {Host} isn't at this address. | Edit address |
| `server_stopped` | connection refused (RST) | The computer is on, but Clarp isn't running on it. | (none) |
| `tailscale_off` | Host address is a tailnet address (100.64.0.0/10, fd7a:115c:a1e0::/48, `*.ts.net`) and the attempt timed out / no route, and either this device has no Tailscale/VPN interface (where the OS lets you check: Android VPN transport, desktop interface list) or no other tailnet Host answers either | Tailscale doesn't seem to be connected on this device. | Open Tailscale (mobile) / Check Tailscale |
| `host_asleep` | timeout / no route while another Host on the same network (or the internet) answers | The computer seems to be off or asleep. | (none) |
| `unknown` | anything else | {Host} isn't responding right now. | (none) |

Never show the raw error as the message. Like clarp-errors' turn error cards, the
raw last-attempt detail may sit behind a closed «Detaljer» / "Details" disclosure in
small selectable monospace; it is always in the diagnostics text.

Severity colours shared with clarp-errors (error red, warning yellow, info grey):
the Host-down panel is **info grey** for device_offline, server_stopped,
tailscale_off, host_asleep, unknown, and **warning yellow** for auth_expired and
wrong_address (the user must act). Never red. Repeats are not stacked: one panel
per Host, updated in place.

## Strings: keys and English source

Keys use dots; Android uses `_` for `.` (`host_down_host_asleep_title`).
Placeholders positional (`%1$@` iOS, `%1$s` Android, `{host}` web/desktop). `{when}` is
always produced by the platform relative-time formatter in the app locale (e.g.
"2 hours ago"); `{in}` by the platform duration/relative formatter ("in 30 seconds").

Per reason (`<reason>` = device_offline, auth_expired, wrong_address, server_stopped,
tailscale_off, host_asleep, unknown):

- `host_down.device_offline.title` "This device is offline" · `.body` "Clarp will reconnect when the network is back."
- `host_down.auth_expired.title` "Signed out of {host}" · `.body` "This device is no longer signed in to {host}."
- `host_down.wrong_address.title` "{host} isn't at this address" · `.body` "The address may have changed."
- `host_down.server_stopped.title` "{host} isn't answering" · `.body` "The computer is on, but Clarp isn't running on it."
- `host_down.tailscale_off.title` "{host} isn't answering" · `.body` "Tailscale doesn't seem to be connected on this device."
- `host_down.host_asleep.title` "{host} isn't answering" · `.body` "The computer seems to be off or asleep."
- `host_down.unknown.title` "{host} isn't answering" · `.body` "{host} isn't responding right now."

Shared:

- `host_down.last_seen` "Last answered {when}"
- `host_down.status.reconnecting` "Reconnecting automatically…"
- `host_down.status.retry_in` "Trying again {in}"
- `host_down.status.back_online` "Back online" (shown ~2 s on recovery, then the panel goes away)
- `host_down.read_only` "Showing saved messages. You can send again when {host} is back." (composer placeholder/hint)
- `host_down.list_row` "{host} isn't answering · {when}" (combined chat list)
- `host_down.banner` "This device is offline" (app-wide banner)
- `host_down.action.retry` "Try again"
- `host_down.action.check_tailscale` "Check Tailscale" (desktop/web)
- `host_down.action.open_tailscale` "Open Tailscale" (iOS/Android: the Tailscale app if installed, else its store page)
- `host_down.action.pair_again` "Pair again"
- `host_down.action.edit_address` "Edit address"
- `host_down.action.switch_host` "Switch Host"
- `host_down.copy_diagnostics` "Copy diagnostics"
- `host_down.copied` "Diagnostics copied" (toast)
- `host_down.details` "Details" (disclosure; reuse `error.details` if the platform already has it)

The copied diagnostics text itself is always English and not localized (it is for an
agent or support); do not put it in the catalog.

Reference translation for clarp-i18n (do not put in code): title «{host} svarer ikke»,
copy button «Kopier diagnose» (Peter's own words), «Prøv igjen», «Detaljer».

## Files owned by clarp-errors: do not edit

Turn errors (inside a turn) are clarp-errors'. Stay out of these files; put the
Host-down panel in new files. If one line of mounting in one of them is truly
unavoidable, message clarp-errors first, keep it to that line, and rebase on their work.

- server-rs: clarp-kernel/src/failures.rs, clarp-kernel/src/turn_error.rs, clarp-host/src/wire.rs, clarp-host/src/edge.rs
- iOS: LiveItems.swift, ClarpModels.swift (hostNotice), LivePresentation.swift (failureRow), ConversationView.swift (TurnNoticeRow), TurnErrorCard.swift
- Android: core LivePresentation.kt, Models.kt, TurnEndReason.kt; app ui/LiveTurn.kt, ConversationScreen.kt (HostNoticeRow), TurnErrorCard.kt
- desktop-rs: core/src/protocol.rs, turn_reason.rs, live_present.rs; slint-app/src/view.rs, panes.rs; ui/transcript.slint (TranscriptRow); turn_error.*
- web: static/lib/live-present.js, TurnNotice.svelte, LiveTurn.svelte, Turn.svelte

Do not reuse these keys for turn errors. clarp-errors confirmed (28 Nov): all text
about the app's connection to a Host (offline, unreachable, reconnecting) is ours;
its turn codes `connection` and `network_gone` are about the Host's connection to
a provider and stay theirs. Existing UI strings belong to clarp-copy-cleanup.
Add **new** string keys/constants for this page; do not rewrite existing strings
in the same change. Where an existing raw "unreachable"/connection error is shown
in a place this panel now covers, remove that display in favour of the panel and
mention it in the PR so clarp-errors / clarp-copy-cleanup know.

## Reconnect

- Backoff per Host: 2 s, 5 s, 10 s, 30 s, then every 60 s. Reset and probe at once
  on: app foreground / window focus, OS network change, Tailscale/VPN change where
  observable, and the Try again button.
- Probe = the Host's existing cheap health/identity route with a short timeout
  (≤ 8 s). A probe never blocks the UI thread and never stalls other Hosts.
- Record `down_since` (first failure after the last success) and `last_ok` per
  Host; persist `last_ok` so "since" survives an app restart.
- On recovery: status `Back online` ~2 s, panel collapses, live data refreshes,
  composer re-enabled. No modal, no sound.

## Copy diagnostics (private-data-free)

Plain text, ≤ 15 lines. Never include: tokens, pairing codes, full URLs, IPs,
hostnames/domains, user names, e-mail, file paths, chat or agent names, message
content. The Host is identified by its label hash only.

```
Clarp diagnostics
App: {platform} {version} ({build})
Host: #{first 6 hex of sha256(host id or base URL)} · address kind: {tailnet|lan|loopback|public|unknown} · scheme: {http|https}
Host version: {last known Host version} · contract {n}   (or "unknown")
State: not answering since {ISO-8601 UTC} ({duration})
Likely reason: {reason id} — {sentence without the Host label}
Last attempt: {timeout after 8 s | connection refused | name not found | TLS error | HTTP 401 | HTTP 503 | not this Host | …}
Attempts since down: {n} · next in {n} s
Other Hosts answering: {k} of {m}
Device network: {online (wifi|cellular|ethernet|vpn) | offline | unknown} · Tailscale/VPN on device: {yes|no|unknown}
Time: {ISO-8601 UTC now}
```

## Visual tone

Calm, not alarming: neutral/secondary colours (no red), a simple icon (cloud-off or
moon for asleep, plug for server stopped, key for auth, signpost for wrong
address, network for Tailscale), no spinner larger than the status-line dot.
Follow each platform's existing design system and dark mode.

## Testing

- Use a disposable test Host (`~/dotfiles/skills/test-clarp-host/SKILL.md`), or a
  local Host process you started, and stop it to produce each state: stop the
  container/process (server_stopped), unroutable address such as
  `http://100.100.100.1:1` or a blackholed port (timeout → tailscale_off/host_asleep),
  a bogus DNS name (wrong_address), a revoked/garbage token (auth_expired).
- Never stop, restart or reconfigure Peter's real Hosts (laptopstudio, EliteBook).
  Pointing an app at an address that simply does not answer is fine.
- Two-Host case is mandatory: one live Host + one down Host; show that the live
  Host's chats, images and sends keep working while the down Host's chat shows the
  panel with cached messages.
- Screenshots of each milestone go to the coordinator (clarp-host-down) as file
  paths under `/home/peter/Shared/Agents/clarp-host-down-d75e88b5/host-down/screens/ (one subfolder per platform)`.
