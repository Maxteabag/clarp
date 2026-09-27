# Oracle handoff: putting the user through to an agent

Status: **proposed wire contract, revision 2, not yet implemented.** Written
so the iOS owner can agree the wire before either side changes behaviour.
Host contract 21, feature `oracle_handoff`.

## What it is

In an Oracle v2 call the user may want to talk to an agent directly ("put me
through to Mike"). Putting them through does **not** make GPT-Live imitate the
agent. The call moves to Clarp's existing always-on hands-free mode with that
agent: the user's speech goes to the agent's own Clarp session as normal user
messages, and the agent's replies are spoken in its own configured TTS voice
(Cartesia/ElevenLabs). Oracle steps out of the loop until someone hands the
user back.

The Host never decides this from the user's words. A handoff starts only when
something calls the Host capability:

- `POST /oracle/connect {"agent": "<session or persona>" | "oracle", "principal"?}`
  (full-device auth; the same function backs the CLI), or
- `clarp-admin oracle connect <agent|oracle>`, which any Clarp agent runs with
  its own judgement (managed skill `clarp-oracle-contact`).

In direct-to-primary mode the user's request reaches the primary (e.g. Theo)
like any other turn; Theo decides to run `clarp-admin oracle connect mike`.
Mike runs `clarp-admin oracle connect oracle` when the user wants Oracle back.

Principles that the rest of this document applies:

- **The phone owns audio; the Host owns the record.** The Host never assumes a
  mode is running because it sent an event, and the phone never switches a mode
  because of a Host record alone. Each phase commits only on an acknowledged
  transition.
- **Only one capture owner at a time.** The iOS audio owner never runs Oracle
  and normal capture together, so the order is: preflight, pause Oracle,
  release Oracle audio, start the new capture, confirm it runs, then close the
  old session. The old side stays resumable until the new one is confirmed.
- **Uncertainty is not failure.** A lost response is resolved by an idempotent
  retry or `GET /oracle/handoff`, never by guessing.

## Channel: the Host event stream, mirrored on the Oracle socket

Handoffs travel on **`GET /events` (SSE)**, event type `oracle-handoff`. That
stream is the one connection the app keeps in every mode: it is open during an
Oracle call and during always-on hands-free, it is durable (SSE `id:`,
`Last-Event-ID` replay), and it does not depend on the Oracle socket, which
leaving Oracle closes. An agent→Oracle handoff therefore always has a channel.
A separate control socket would duplicate SSE and need its own reconnect and
auth story.

For `oracle_to_agent` the Host also sends each event on the live Oracle v2
WebSocket as `{"type": "oracle_v2.handoff", ...}` with the same payload, so a
phone whose SSE is momentarily reconnecting still sees it. Events are
wake-ups; the record returned by the ack and `GET /oracle/handoff` is the
truth.

## Identity and clocks

`GET /oracle/handoff` (full-device auth) returns:

```json
{"principal": "device_…", "host_id": "…", "server_now": 1790000000000,
 "handoff": {…} | null, "active": {…} | null}
```

- `principal` is the caller's own authenticated device id, the value the Host
  puts in every handoff's `principal`. The app learns it here (it is also in
  the pairing response as `device_id`); it compares, never guesses.
- `host_id` is this Host's `server_instance_id`, as in `/server-info`.
- `handoff` is the newest record for this principal in any state; `active` is
  the `oracle_to_agent` record currently in state `active`, or null. During a
  failed or pending return, `active` still shows the handoff that put the user
  through, so the app can see that the user is still meant to be with the
  agent.

Every event and record carries `server_now` (Host clock when sent) and
`ttl_ms` (how long the current phase may wait before the Host acts on its
own). The phone computes its deadline as *local receipt time + ttl_ms* and
never compares `expires_at` against its own clock. An event that arrived in an
SSE replay (older than the live tail) is never executed from its own fields:
the phone first reads `GET /oracle/handoff` and uses that record's state,
`server_now` and `ttl_ms`.

## Record and event payload

| Field | Meaning |
|---|---|
| `handoff_id` | Unique id (`hof_<32 hex>`), minted by the Host per handoff. |
| `parent_handoff_id` | For `agent_to_oracle`: the `oracle_to_agent` handoff it ends. Otherwise empty. |
| `host_id` | This Host's `server_instance_id`. |
| `principal` | The paired device the handoff is for. |
| `generation` | Per-principal integer, persisted, strictly increasing with each new handoff in either direction, across Host restarts. |
| `revision` | Increases by one on every state change of this handoff. Events and records with a lower revision than one already seen for the same `handoff_id` are stale. |
| `direction` | `oracle_to_agent` or `agent_to_oracle`. |
| `mode` | `hands_free` (the only mode in contract 21). |
| `state` | See the state machine. |
| `reason` | Empty or why the state changed (table below). |
| `oracle_call` | `running`, `closed` or `unknown`: what the Host knows about the Oracle call at this state. A failure says `running` only when the Oracle call really is live again. |
| `agent` | `{session, agent_id, persona}`: the agent the user is put through to (`oracle_to_agent`), or the agent the user is expected to be with now (`agent_to_oracle`). |
| `oracle` | `{voice_session_id, provider_session, thread_id}`: the Oracle call being left (`oracle_to_agent`), or the thread to resume (`agent_to_oracle`). |
| `issued_at`, `server_now`, `ttl_ms`, `expires_at` | Epoch ms on the Host clock; `expires_at` is informational. |

## State machine

Both directions use the same states. "Old side" is Oracle for
`oracle_to_agent` and hands-free for `agent_to_oracle`; "new side" the other.

```
             ack preparing            ack activating            ack ready
 offered ──────────────────▶ preparing ─────────────────▶ activating ─────────▶ active ──▶ ended
   │                           │                            │                     (return, local stop,
   │ ack failed / ttl          │ ack rolled_back / ttl      │ ack rolled_back      new call, restart)
   ▼                           ▼                            ▼
 failed (old side running)   failed (old side running)    failed (old side running)
                                                            │ ack rollback_failed / ttl
                                                            ▼
                                                          broken (neither side confirmed)
 any non-terminal ── superseded by a newer handoff or a new call ──▶ cancelled
```

| State | Meaning | Old side | New side | Host does on ttl |
|---|---|---|---|---|
| `offered` | Host asked the phone to switch. | running, untouched | not started | `failed` / `no_ack`, `oracle_call: running` (nothing was touched) |
| `preparing` | Phone preflighted and claimed the offer. It is now pausing and draining the old side; it still owns the old side's audio and session. | paused, resumable | not started | `failed` / `prepare_timeout`; Host resumes feeding the old side; `oracle_call: unknown` until the phone acks `rolled_back` |
| `activating` | Phone released the old side's audio and is starting capture on the new side. The old session stays open and resumable. | audio released, session open | starting | `broken` / `activation_timeout`, `oracle_call: unknown`. The Host does **not** resume the old side on its own. |
| `active` | Phone confirmed the new side's capture is running. Only now does the phone close the old session. | closed by the phone | running | none |
| `failed` | Transfer did not happen. `oracle_call` says whether the old side really runs again. | per `oracle_call` | stopped | terminal |
| `broken` | The phone could not confirm either side (rollback failed, or it went silent after releasing audio). The Host never claims the call resumed. | unknown / closed | unknown | terminal |
| `cancelled` | Superseded before completing. | per `oracle_call` | not started | terminal |
| `ended` | A completed handoff is over. | — | — | terminal |

Reasons: `no_ack`, `prepare_timeout`, `activation_timeout`, `client_failed`,
`rolled_back`, `rollback_failed`, `superseded`, `returned`, `return_failed`,
`client_returned`, `local_stop`, `client_restarted`, `local_intent_changed`.

## Acknowledgements

`POST /oracle/handoffs/ack`

```json
{"handoff_id": "hof_…", "generation": 17, "phase": "preparing|activating|ready|rolled_back|rollback_failed|failed|ended",
 "reason": "…", "voice_session_id": "…"}
```

- The Host answers `200 {"handoff": record}` when the phase is applied **or is
  a repeat of the phase already applied**. Acks are idempotent per
  `(handoff_id, generation, phase)`; a phone that lost a response retries the
  same ack and gets the same record.
- A phase that is not legal from the current state answers
  `409 {"error": "illegal_transition", "handoff": record}`. The phone then
  follows the returned record (below). `404` for an unknown id; `409` with
  `"error": "stale_generation"` for a generation older than the principal's
  newest.
- A phone must not start the next local step until the Host has confirmed the
  previous ack. If a response is lost, it retries the same ack, or reads
  `GET /oracle/handoff`, before doing anything irreversible.

Legal transitions:

| From | Ack phase | To | Notes |
|---|---|---|---|
| `offered` | `preparing` | `preparing` | Only within the offer's ttl. |
| `offered` | `failed` | `failed` (`client_failed`, `oracle_call: running`) | Preflight failed; nothing was touched. |
| `preparing` | `activating` | `activating` | |
| `preparing` / `activating` | `rolled_back` | `failed` (`rolled_back`, `oracle_call: running`) | The phone has the old side capturing again. |
| `activating` | `ready` | `active` | New side's capture is running. |
| `activating` | `rollback_failed` | `broken` (`rollback_failed`) | |
| `failed` (`prepare_timeout`) | `rolled_back` | unchanged, `oracle_call` set to `running` | Late confirmation of the forced resume. |
| `broken` (`activation_timeout`) | `ready` | `active` | The phone's late proof that the new side runs is accepted; only the phone knows. |
| `broken` (`activation_timeout`) | `rolled_back` | `failed` (`rolled_back`, `oracle_call: running`) | Late proof that the old side runs. |
| `active` | `ended` | `ended` (`local_stop`, `client_restarted`, `local_intent_changed`) | |
| any | same phase again | unchanged | Idempotent repeat. |

Every other combination is `409 illegal_transition`. In particular nothing
leaves `ended` or `cancelled`, and a `failed` offer never becomes `preparing`
again: a new attempt is a new handoff.

What the phone does with a record it did not expect: if the record says
`failed`, `cancelled` or `broken` while it is mid-transfer, it returns to (or
stays on) the old side if it still can, then acks `rolled_back` or
`rollback_failed` (the late transitions above). If the record says `active`,
it completes the transfer.

## Fencing duplicates and retired events

A phone keeps, per `(host_id, principal)`, the highest `generation` seen and,
per `handoff_id`, the highest `revision` seen and whether it has **executed**
the offer. It drops an event when:

1. `principal` or `host_id` is not its own;
2. `generation` is lower than the highest seen **and** it is an `offered`
   event (later-state events of an older handoff are still applied, see 4);
3. its `revision` is not higher than the one already seen for that
   `handoff_id` (the WS/SSE duplicate, or an SSE replay);
4. it is `offered` and this phone already executed that `handoff_id`.

Later events of the **same** `handoff_id` (`preparing`, `active`, `failed`,
`cancelled`, `broken`) are always applied; only re-execution of an offer is
suppressed. Two more rules keep a retired event from retargeting a newer
local session:

5. An `oracle_to_agent` offer is executed only when `oracle.voice_session_id`
   is the Oracle call the phone has open now.
6. An `agent_to_oracle` offer is executed only when the phone is in hands-free
   with `agent.session` **because of** the handoff named in
   `parent_handoff_id`. If the user has since picked another session, started
   a call or stopped, the phone acks `failed` with `local_intent_changed`; the
   Host marks the return `failed` and the parent `ended`
   (`local_intent_changed`).

## Capability negotiation

- The Host advertises feature `oracle_handoff` (contract 21) in `/server-info`.
- A phone that implements this document opens Oracle v2 with
  `handoff=hands_free` on `/oracle/v2`. The Host offers a handoff only to a
  call that advertised it; `POST /oracle/connect` for any other call answers
  `409 {"error": "handoff_unsupported"}` and leaves the call untouched.
- `agent_to_oracle` is only issued for a handoff this Host created, so its
  receiver has negotiated.

## Oracle → agent

1. `connect(agent)` resolves the agent, finds the caller's live Oracle call,
   creates the record in `offered` (new generation, `ttl_ms` 10 000) and emits
   it on SSE and the Oracle socket. With earcons on, the `connected` cue
   (tinted for that agent) is sent on the Oracle socket first.
2. Phone preflight, no side effects: the recipient session is known and
   usable, the microphone permission is granted, always-on can start. On
   failure: ack `failed`. On success: ack `preparing`.
3. Once `preparing` is confirmed the Host sends Oracle nothing new (no routing
   of later turns, no relayed parts, no context) and keeps forwarding the audio
   Oracle is already producing. The phone stops sending microphone audio and
   lets its playback drain (it may cap the drain at 3 s). The Oracle socket and
   the session stay open. Then it acks `activating` (`ttl_ms` 30 000).
4. Once `activating` is confirmed the phone releases Oracle's audio claim and
   starts always-on with `agent.session` as the recipient. When capture is
   actually running it acks `ready`. The Host sets the server focus to
   `agent.session` (as `POST /select` does), marks the record `active`, and
   `connect()` returns `{"ok": true, "handoff_id", "state": "active"}`.
5. Only after `active` is confirmed the phone sends
   `{"type": "session.close", "handoff_id": "hof_…"}` on the Oracle socket and
   closes it. A close carrying the id of an `active` (or `activating`) handoff
   is the intended end of the Oracle call and never changes the handoff. A
   close or drop without it, before `active`, leaves the handoff to its phase
   ttl.
6. If starting the new capture fails in step 4, the phone re-takes Oracle's
   audio on the still-open session and acks `rolled_back`; if that also fails,
   it acks `rollback_failed`. The Host then plays `switch_failed` (when the
   Oracle socket is still open) and, only for `rolled_back`, gives Oracle one
   neutral data item: `{"host_event": "handoff_failed", "agent": "Mike",
   "reason": "…"}`. Oracle decides what to say. `connect()` returns an error
   either way; for `broken` it says the call could not be resumed.

A new Oracle call opened by the same principal while the handoff is `offered`
or `preparing` supersedes it (`cancelled`, `superseded`). While it is
`active`, a new Oracle call without `handoff_id` ends it (`ended`,
`client_returned`): the user came back on their own.

Oracle's final audio and transcript are drained, not cut. What Oracle said up
to the close is in the Oracle thread's durable memory (the same checkpoint as
any call end). The handoff adds nothing to the agent's conversation; the agent
sees only what the user then says. Records and their terminal states persist.

## Agent → Oracle

Success means the new Oracle session and its audio are ready, not a socket
upgrade or an early POST.

1. `connect("oracle")` finds the caller's `active` `oracle_to_agent` handoff,
   creates an `agent_to_oracle` record in `offered` with `parent_handoff_id`,
   `agent` (the agent the user is expected to be with) and `oracle.thread_id`,
   and emits it on SSE.
2. The phone applies fence rule 6, preflights (Oracle v2 still advertised, the
   thread id is known, microphone permission) and acks `preparing`, or
   `failed`.
3. Once `preparing` is confirmed it stops hands-free capture and settles the
   final utterance: an utterance already captured is finished, transcribed and
   sent with its own `client_msg_id` (so a retry is deduplicated by `/send`);
   speech already delivered is not sent again. Playback of the agent's current
   clip may drain. It then acks `activating`.
4. Once `activating` is confirmed it releases the capture and opens
   `/oracle/v2?handoff=hands_free&thread_id=<thread>&handoff_id=<id>`. Opening
   the socket is not success. When the Oracle session has started
   (`session.started` received) and Oracle capture is running, it acks
   `ready`. The return becomes `active` and its parent `ended` (`returned`);
   the Host plays `back_to_oracle` on the new call and gives Oracle one
   neutral data item: `{"host_event": "returned_from_agent", "agent": "Mike"}`.
   `connect("oracle")` returns ok.
5. If the capability, the provider (e.g. `503`) or Oracle audio fails, the
   phone restores hands-free with `agent.session` **only if the same local
   owner still intends it** (nothing else took over the mode meanwhile) and acks
   `rolled_back`: the return is `failed`, the parent stays `active`. Otherwise
   it acks `rollback_failed`: the return is `broken`, the parent `ended`
   (`return_failed`), and neither side is claimed to run.
   `connect("oracle")` returns an error to the agent.

## Reconciliation, process death and local Stop

- A Host record never restarts the microphone or replaces the phone's current
  local intent on its own. On launch, after an SSE reconnect, or after an ack
  timeout, the phone reads `GET /oracle/handoff` and only reconciles
  bookkeeping:
  - a record in `offered`, `preparing` or `activating` that this process did
    not start (it died mid-transfer): ack `failed` with `client_restarted`
    (`offered`) or `rollback_failed` with `client_restarted` (later phases),
    and leave local audio as the user finds it;
  - an `active` record while the phone is not in hands-free with that agent:
    ack `ended` with `client_restarted` (or `local_intent_changed` if the user
    is in another mode);
  - an `active` record that matches the running local mode: nothing to do.
- An explicit local Stop during an `active` handoff acks `ended` with
  `local_stop`. `clarp-admin oracle connect oracle` then answers
  `404 no active handoff`.
- A Host restart keeps records; phase ttls run from the restart for records
  that were mid-transfer.

## Resolving the target

`agent` may be a session id or a persona name. A session id resolves among
agents that are not deleted, not archived and not Janitors. A persona name
resolves only among the contacts the app shows: the same rows, excluding helper
sub-agents. An unknown name, or one that matches more than one visible
contact, is an error returned to the caller (`unknown agent`, `ambiguous
agent`); the caller decides what to tell the user. `oracle` (any case) means
back to Oracle.

## Which call

A paired device's request acts on that device's call. The administrator
credential (`clarp-admin` and the agents it runs for) acts on the one live
Oracle call or active handoff on this Host; when there are several it must
name the device with `"principal"`, otherwise `409 several live calls`. No
live call: `404 no live Oracle call`. `connect()` waits for the handoff to
reach a terminal state or `active` (at most offer + prepare + activation ttls)
and returns the final record.

## Expected iOS handling (for agreement with the iOS owner)

- Add `handoff=hands_free` when opening `/oracle/v2`, only when the Host
  advertises `oracle_handoff`.
- A handoff coordinator consumes `oracle-handoff` (SSE) and `oracle_v2.handoff`
  (WS), applies the fence rules, executes each offer at most once, and applies
  later states of the same handoff.
- `oracle_to_agent`: preflight → ack `preparing` → pause Oracle capture and
  drain playback (≤3 s) → ack `activating` → release Oracle audio,
  `AppModel.startAlwaysOn(carModeToken:physicalController:)` with the
  recipient set explicitly to `agent.session` (not
  `toggleCarModeRecipient`, which toggles) → ack `ready` → `session.close`
  with `handoff_id`. Rollback: re-take Oracle audio → ack `rolled_back`
  (or `rollback_failed`).
- `agent_to_oracle`: fence rule 6 → preflight → ack `preparing` → stop
  hands-free capture, settle the final utterance → ack `activating` →
  `stopAlwaysOn`, `startOracleMode` opening `/oracle/v2` with `thread_id` and
  `handoff_id` → on `session.started` with capture running, ack `ready`.
  Rollback: restore always-on only if the same owner still intends it.
- The handoff is transient: it must not write the saved Oracle, AfterISpeak,
  addressing or delegation preferences.
- Wait for each ack's confirmation before the next irreversible step; on a
  lost response, retry the same ack or read `GET /oracle/handoff`.
