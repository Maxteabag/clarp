# Oracle handoff: putting the user through to an agent

Status: **proposed wire contract, not yet implemented.** It is written so the
iOS owner can agree the wire before either side changes behaviour. Host
contract 21, feature `oracle_handoff`.

## What it is

In an Oracle v2 call the user may want to talk to an agent directly ("put me
through to Mike"). Putting them through does **not** make GPT-Live imitate the
agent. The call is handed over to Clarp's existing hands-free voice mode with
that agent: the user's speech goes to the agent's own Clarp session as normal
user messages, and the agent's replies are spoken in its own configured TTS
voice (Cartesia/ElevenLabs), exactly as in always-on mode today. Oracle steps
out of the loop until someone hands the user back.

Nobody on the Host decides this from the user's words. A handoff starts only
when something calls the Host capability:

- `POST /oracle/connect {"agent": "<session or persona>" | "oracle"}`
  (full-device auth; the same function backs the CLI), or
- `clarp-admin oracle connect <agent|oracle>`, which any Clarp agent runs with
  its own judgement (managed skill `clarp-oracle-contact`).

In direct-to-primary mode the user's request reaches the primary (e.g. Theo)
like any other turn; Theo decides to run `clarp-admin oracle connect mike`.
Mike, once the user is talking to him, runs `clarp-admin oracle connect oracle`
when the user wants Oracle back.

## Channel: the Host event stream, mirrored on the Oracle socket

Handoffs travel on **`GET /events` (SSE)**, event type `oracle-handoff`. That
stream is the one connection the app keeps in every mode: it is open during an
Oracle call and during always-on hands-free, it is durable (SSE `id:`,
`Last-Event-ID` replay), and it does not depend on the Oracle socket, which
leaving Oracle closes. An agent→Oracle handoff therefore always has a channel.
A separate control socket would duplicate what SSE already provides and would
need its own reconnect and auth story.

For the Oracle→agent direction only, the Host also sends the identical payload
on the live Oracle v2 WebSocket as `{"type": "oracle_v2.handoff", ...}`, so a
phone whose SSE is momentarily reconnecting still sees it at once. The two
copies carry the same `handoff_id`; the client acts on whichever arrives first
and drops the other.

## Event payload

Both copies carry these fields (SSE adds its usual `event_id`, `ts`):

| Field | Meaning |
|---|---|
| `handoff_id` | Unique id (`hof_<32 hex>`), minted by the Host per handoff. The idempotency and ack key. |
| `host_id` | This Host's `server_instance_id` (as in `/server-info`). A client with several Hosts scopes everything below to it. |
| `principal` | The paired device the handoff is for (`device_…`). A client ignores handoffs for another principal. |
| `generation` | Per-principal integer, persisted on the Host, strictly increasing with every new handoff (both directions, across restarts). |
| `direction` | `oracle_to_agent` or `agent_to_oracle`. |
| `mode` | `hands_free` (the only mode in contract 21). |
| `state` | `offered`, `active`, `failed`, `cancelled` or `ended` (below). The client acts only on `offered`. |
| `reason` | Empty, or why the state changed: `no_ack`, `client_failed`, `superseded`, `client_returned`, `returned`, `call_closed`. |
| `agent` | `{session, agent_id, persona}` of the agent the user is (or was) put through to. |
| `oracle` | `{voice_session_id, provider_session, thread_id}`: the Oracle call the user leaves (`oracle_to_agent`) or the Oracle thread to resume (`agent_to_oracle`; `voice_session_id` and `provider_session` then name the call that was left). |
| `issued_at`, `expires_at` | Epoch ms. An `offered` event past `expires_at` is dead; the client must not act on it. |

## Fencing duplicates and retired events

A client keeps, per `(host_id, principal)`, the highest `generation` it has
seen and the ids of handoffs it has already acted on. It drops an event when:

1. `principal` is not its own device, or `host_id` is not the Host it is
   connected to;
2. `generation` is lower than the highest seen;
3. its `handoff_id` was already acted on (the WS/SSE duplicate, or an SSE
   replay after reconnect);
4. `state` is `offered` but `expires_at` has passed;
5. `direction` is `oracle_to_agent` and `oracle.voice_session_id` is not the
   Oracle call the client currently has open. A retired call's handoff can
   never retarget a successor call.

When in doubt after a reconnect or relaunch, `GET /oracle/handoff` returns the
Host's current record for the caller (`{"handoff": {...} | null}`); it is the
truth, events are wake-ups.

## Capability negotiation

- The Host advertises feature `oracle_handoff` (contract 21) in `/server-info`.
- A client that implements this document opens Oracle v2 with the query
  parameter `handoff=hands_free` on `/oracle/v2`. The Host offers a handoff
  only to a call that advertised it. `POST /oracle/connect` for a call that did
  not answers `409 {"error": "handoff_unsupported"}` and the call is left
  untouched, so an older app is never hung up on.
- `agent_to_oracle` is only issued for a handoff this Host created, so the
  client that receives it has already negotiated.

## Oracle → agent

1. `connect(agent)` resolves the agent (below), finds the caller's live Oracle
   call, creates the record in state `offered` with a new generation, and emits
   the event (SSE and the Oracle socket). The `connected` earcon, tinted for
   that agent, is sent on the Oracle socket just before it when earcons are on.
2. From the offer on, the Host sends Oracle nothing new: no routing of later
   turns, no relayed parts, no context. It keeps forwarding the audio Oracle is
   already producing.
3. The client stops sending microphone audio to Oracle, lets its playback queue
   drain (it may cap the drain at 3 s), then calls
   `POST /oracle/handoffs/ack {"handoff_id", "generation", "result": "accepted"}`,
   sends `session.close` and closes the Oracle socket, and starts always-on
   hands-free with `agent.session` as the recipient.
4. On `accepted` the Host sets the server focus to `agent.session` (as
   `POST /select` does), marks the record `active` and emits it. `connect()`
   returns `{"ok": true, "handoff_id", "state": "active"}`.
5. The client sends the user's speech as ordinary `POST /send` turns to
   `agent.session` and plays the agent's clips as in always-on mode.

Failure, timeout and reconnect:

- The client cannot switch (no microphone, the recipient is gone, a local
  error): it acks `{"result": "failed", "reason": "…"}` and keeps the Oracle
  call. The Host marks `failed` (`client_failed`), plays `switch_failed`,
  resumes the call exactly where it was, and gives Oracle one neutral data item
  (`{"host_event": "handoff_failed", "agent": "Mike", "reason": "client_failed"}`);
  Oracle decides what to say. `connect()` returns an error.
- No ack within 10 s (`expires_at`): the same, with `reason: "no_ack"`.
- The phone opens a new Oracle call while the offer is pending: the new call
  supersedes it (`cancelled`, `superseded`), per fence rule 5.
- The Oracle socket drops after the offer but before the ack: the offer stays
  valid until `expires_at`; an `accepted` ack still completes it.
- While `active`, the phone opens an Oracle call on its own (the user tapped
  Oracle): the Host ends the handoff (`ended`, `client_returned`). No agent is
  needed to come back.

Oracle's final audio and transcript: drained, not cut. Whatever Oracle said up
to the close is already in the Oracle thread's durable memory (the same
checkpoint as any call end). Nothing is added to the agent's conversation by
the handoff; the agent sees only what the user then says. The handoff record
(`GET /oracle/handoff`) and its terminal state persist on the Host.

## Agent → Oracle

1. `connect("oracle")` finds the caller's `active` handoff, creates an
   `agent_to_oracle` record in state `offered` with a new generation and the
   Oracle `thread_id` to resume, and emits it on SSE.
2. The client leaves always-on and opens Oracle v2 with
   `thread_id=<oracle.thread_id>&handoff_id=<handoff_id>&handoff=hands_free`.
   Opening that call is the ack; an explicit
   `POST /oracle/handoffs/ack {"result": "accepted"}` first is also accepted.
3. The Host marks the return `ended` (`returned`), plays `back_to_oracle` on
   the new call and gives Oracle one neutral data item
   (`{"host_event": "returned_from_agent", "agent": "Mike"}`).
4. No call within 10 s, or a `failed` ack: the return is `failed`, the
   original handoff stays `active` (the user is still with the agent) and
   `connect("oracle")` returns an error to the agent.

## Resolving the target

`agent` may be a session id or a persona name. A session id resolves among
agents that are not deleted, not archived and not Janitors. A persona name
resolves only among the contacts the app shows: the same rows, excluding helper
sub-agents. An unknown name, or a name that matches more than one visible
contact, is an error returned to the caller (`unknown agent`, `ambiguous
agent`); the caller decides what to tell the user. `oracle` (any case) means
back to Oracle.

## Which call

A paired device's request acts on that device's call. The administrator
credential (`clarp-admin` and the agents it runs for) acts on the one live
Oracle call or active handoff on this Host; when there are several it must
name the device with `"principal"` and otherwise gets `409 several live calls`.
No live call: `404 no live Oracle call`.

## Expected iOS handling (for agreement with the iOS owner)

- Add `handoff=hands_free` when opening `/oracle/v2`, only when the Host
  advertises `oracle_handoff`.
- A handoff coordinator consumes `oracle-handoff` (SSE) and `oracle_v2.handoff`
  (WS), applies the fence rules above, and acts only on `state: "offered"`.
- `oracle_to_agent`: stop Oracle capture, drain playback (≤3 s), ack, close the
  Oracle socket, then `AppModel.startAlwaysOn(carModeToken:physicalController:)`
  with the recipient set to `agent.session`. This is transient: it must not
  overwrite the saved Oracle, AfterISpeak, addressing or delegation
  preferences. Set the recipient explicitly rather than through
  `toggleCarModeRecipient`, which toggles.
- `agent_to_oracle`: `stopAlwaysOn`, then `startOracleMode` opening `/oracle/v2`
  with the given `thread_id` and `handoff_id`.
- On failure at any step, ack `failed` with a short reason and stay where the
  user is.
- After an SSE reconnect or relaunch mid-handoff, reconcile with
  `GET /oracle/handoff` before acting on replayed events.
