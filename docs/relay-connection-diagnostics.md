# Relay connection loss: diagnostics and recovery

Every phone stream that rides the managed relay (SSE, audio, terminal
WebSockets) shares one outbound WebSocket from the Host to the Cloudflare
Worker `clarp-relay` (a Durable Object per host id). When that session drops,
the Host tears down all of its streams and redials. This page describes what
the Host now records about a loss, how to read it, and what the Worker does on
its side.

## What the journal and telemetry record

Each lost session writes one warning line and one `diagnostic_events` row
(source `relay`, event `relayConnectionLost`, category `network`):

```
relay connection lost (ConnectionClosedError code=1006 reason='' sent=1011 age=1834.2s streams=3)
```

Detail fields on the telemetry row:

| field | meaning |
| --- | --- |
| `error_type` | exception class; `ConnectionClosedError`, `ConnectionClosedOK`, `TimeoutError`, `gaierror`, ... |
| `code`, `reason` | close frame received from the Worker; `code` is `1006` when no frame arrived |
| `rcvd_code`, `rcvd_reason` | the same, or `null` when the peer sent nothing |
| `sent_code`, `sent_reason` | close frame the Host sent; `1011 keepalive ping timeout` means a pong was late by more than 20 s |
| `rcvd_then_sent` | `true` when the peer closed first and the Host echoed |
| `http_status` | handshake rejection status (`403` bad key, `503` and so on) |
| `errno` | OS error number for socket failures (`-3` DNS failure) |
| `session` | connection number since the service started |
| `session_age_s` | how long the lost session had been up; `null` when the dial itself failed |
| `streams_torn_down`, `http_streams`, `ws_streams` | phone streams cut by this loss |

A successful dial writes `relayConnectionOpened` with `downtime_ms`, the time
the Host had no relay session. The exception text is never logged: it can carry
the credential-bearing connect URL.

Reading the codes:

- `code=1006`, `sent_code=null`: the TCP link died without a close frame
  (network path, laptop sleep or roaming, NAT rebinding, Cloudflare edge
  restart).
- `code=1006`, `sent_code=1011`: the Host's keepalive gave up. The Worker edge
  answers protocol pings without waking the Durable Object, so this points at
  the local network or an event loop stall, not at the Worker code.
- `rcvd_code=1012 reason=replaced`: another connector logged in with the same
  host id. The Worker allows one host socket and evicts the old one. Find the
  other Clarp instance sharing `relay.json` before anything else.
- `rcvd_code=1001` or `1011`: the Worker side closed deliberately; a Worker
  deployment (including `wrangler secret put`) disconnects every WebSocket.
- `ConnectionClosedOK` (`1000`/`1001`): a clean close; still logged, since the
  Host never asks for one.

Query the last day:

```sql
SELECT ts_iso, json_extract(detail,'$.code') code, json_extract(detail,'$.reason') reason,
       json_extract(detail,'$.sent_code') sent, json_extract(detail,'$.session_age_s') age,
       json_extract(detail,'$.streams_torn_down') streams
  FROM diagnostic_events WHERE source='relay' AND event='relayConnectionLost'
 ORDER BY ts DESC LIMIT 50;
```

## Reconnect schedule

A session that was established and then lost is redialed after 0.3 s. Repeated
dial failures without a successful connection back off 1, 2, 4, ... 30 s. A
`1012 replaced` close waits 5 s so two connectors with the same host id do not
flap at full speed. `clarp-admin network relay status` shows the state.

## Stream cleanup on the phone side

When the host socket closes, the Worker fails every pending stream itself:
SSE bodies are aborted and tunneled WebSockets are closed with `1012 host
disconnected`, both retryable for the client. If the Worker later forwards a
frame for a stream the current Host session never saw (a Host restart while
the Durable Object kept its state), the Host answers `WS_CLOSE 1012 Host
stream gone` or `ABORT`, so the Worker closes that phone socket instead of
leaving it hung. Frames the Worker already ignores for unknown ids stay
silent.

## Baseline before this instrumentation (2026-09-11 to 2026-09-25)

The journal only recorded the exception class. Over fourteen days: 109
`ConnectionClosedError`, 15 `TimeoutError`, 2 `ConnectionResetError`, and 941
`gaierror` in four offline bursts (DNS failure every 40 s: the 30 s backoff cap
plus resolver timeout). Excluding the bursts, 12 to 21 losses per day at
intervals from 5 minutes to 4 hours (median 32 min) with no clustering at any
fixed interval or minute of the hour, so the Worker enforces no maximum
session lifetime and proactive rollover would not help (the Worker also
permits only one host socket at a time). Losses concentrate in working hours
(9 to 18) and are rare overnight, which fits activity-triggered causes
(network path changes, Durable Object restarts under load) rather than a
timer. Of 240 SSE connections in three days, 185 lasted under a minute but
only 4 ended within 4 s of a relay loss: short SSE sessions are mostly a
client reconnect pattern, not relay drops.

## Worker-side hardening (not deployed)

The Worker source is not in a repository; fetch it with the Cloudflare API
(`GET /accounts/<acct>/workers/scripts/clarp-relay`, multipart, `index.js`).
Two small changes would make the phone side cleaner, independent of the cause:

1. In `webSocketMessage`, when a frame arrives for an id with no `pending`
   entry, close any client socket still tagged with that id:

   ```js
   if (!p) {
     if (kind === K.WS_CLOSE || kind === K.ABORT)
       for (const c of this.ctx.getWebSockets(String(id))) safeClose(c, 1012, "host stream gone");
     else ws.send(frame(K.ABORT, id));
     return;
   }
   ```

2. Wrap `p.clientWs?.send(...)` in `WS_MSG` in `try { } catch { this.pending.delete(id); ws.send(frame(K.ABORT, id)); }`
   so a phone socket that is already closing fails only its own stream.

Deploy from the owner's machine only, after confirming the telemetry above
shows Worker-originated closes: `npx wrangler deploy` in a directory holding
`index.js` and a `wrangler.toml` with `name = "clarp-relay"`, the
`HOST_RELAY` Durable Object binding (class `HostRelay`, namespace
`07c041217a5c48bdb2ac6934f8e8fa10`) and `compatibility_date = "2026-09-01"`.
Deploying disconnects every host and phone once.
