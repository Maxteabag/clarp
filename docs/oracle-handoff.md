# Oracle handoff: putting the user through to an agent

Status: **proposed wire contract, revision 4, agreed, not yet implemented.** Written
so the iOS owner can agree the wire before either side changes behaviour.
Host contract 22, feature `oracle_handoff`.

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
- `handoff` is the newest record for this principal in any state.
- `active` is the **parent hands-free record**: the `oracle_to_agent` handoff
  currently in state `active`, or null. It is not a general "current mode"
  indicator: an ordinary Oracle call, a manually started always-on session or a
  completed return all leave it null. During a pending or failed return it
  still shows the parent, because the user is still meant to be with the
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
| `mode` | `hands_free` (the only mode in contract 22). |
| `state` | See the state machine. |
| `reason` | Empty or why the state changed (table below). |
| `oracle_call` | `running`, `closed` or `unknown`: the state of the **Oracle** session, never of the generic old side (derivation rule below). |
| `agent` | `{session, agent_id, persona}`: the agent the user is put through to (`oracle_to_agent`), or the agent the user is expected to be with now (`agent_to_oracle`). |
| `oracle` | `{voice_session_id, provider_session, thread_id}`: the Oracle call being left (`oracle_to_agent`), or the thread to resume (`agent_to_oracle`). |
| `issued_at`, `server_now`, `ttl_ms`, `expires_at` | Epoch ms on the Host clock; `expires_at` is informational. |

## State machine

Both directions use the same states. "Old side" is Oracle for
`oracle_to_agent` and hands-free for `agent_to_oracle`; "new side" the other.

```
             ack preparing            ack activating            ack ready
 offered ──────────────────▶ preparing ─────────────────▶ activating ───────────▶ active ──────▶ ended
   │                           │                            │            (oracle_to_agent)   (return,
   │ ack failed / ttl          │ ack rolled_back / ttl      │ ack rolled_back               abandon,
   ▼                           ▼                            ▼                               new call)
 failed (old side running)   failed (old side running)    failed (old side running)
                                                            │ ack rollback_failed / ttl
                                                            ▼
                                                          broken (neither side confirmed)

 agent_to_oracle: activating ── ack ready ──▶ ended (returned); its parent ──▶ ended (returned)
 offered / preparing / activating ── ack abandon ──▶ cancelled (bookkeeping only)
 offered / preparing ── superseded by a newer handoff or a new Oracle call ──▶ cancelled
```

| State | Meaning | Old side | New side | ttl_ms | Host does when the ttl runs out |
|---|---|---|---|---|---|
| `offered` | Host asked the phone to switch. | running, untouched | not started | 10 000 | `failed` / `no_ack`, `oracle_call` derived (nothing was touched) |
| `preparing` | Phone preflighted and claimed the offer. It is now pausing and draining the old side; it still owns the old side's audio and session. | paused, resumable | not started | 10 000 (both directions) | `failed` / `prepare_timeout`; for `oracle_to_agent` the Host resumes feeding Oracle; `oracle_call` derived (`unknown` for `oracle_to_agent` until the phone acks `rolled_back`, since its capture was paused) |
| `activating` | Phone released the old side's audio and is starting capture on the new side. The old session stays open and resumable. | audio released, session open | starting | 30 000 | `broken` / `activation_timeout`, `oracle_call: unknown`. The Host does **not** resume the old side on its own. |
| `active` | `oracle_to_agent` only: the phone confirmed hands-free capture with the agent is running. Only now does it close the Oracle session. | closed by the phone | running | none | — |
| `failed` | Transfer did not happen. `oracle_call` says only whether Oracle runs. | per the phone | stopped | terminal | — |
| `broken` | The phone could not confirm either side (rollback failed, or it went silent after releasing audio). The Host never claims the call resumed. | unknown / closed | unknown | terminal | — |
| `cancelled` | Superseded, or abandoned by the phone, before completing. Bookkeeping only. | per the phone | not started | terminal | — |
| `ended` | A completed handoff is over: the user returned, stopped, or the return completed. | — | — | terminal | — |

An `agent_to_oracle` handoff has no lasting `active` state: its `ready` ack
means the Oracle session and audio run, so it goes straight to `ended`
(`returned`) together with its parent. After that the user is simply in an
Oracle call, and `GET` shows `active: null`.

`oracle_call` is derived from the Oracle v2 session the Host actually holds
for this handoff: for `oracle_to_agent` the call named in
`oracle.voice_session_id`, for `agent_to_oracle` the call opened with this
`handoff_id`. It is `running` when that session is open and the Host is
feeding it and no ack has said its capture is paused or released; `closed`
when no such session is open (for `agent_to_oracle` before the new call opens
and after the phone closes it); `unknown` when the session is open but the
phone has paused or released its audio, or the Host cannot tell. Hands-free
resuming never makes it `running`. In the tables below "derived" means this
rule; for orientation, a failed `oracle_to_agent` transfer whose original call
is still open and fed is `running`, and a failed or rolled-back
`agent_to_oracle` return is `closed` (or `unknown` if the new call is still
open).

Reasons: `no_ack`, `prepare_timeout`, `activation_timeout`, `client_failed`,
`rolled_back`, `rollback_failed`, `superseded`, `returned`, `return_failed`,
`client_returned`, `local_stop`, `client_restarted`, `local_intent_changed`.

## Acknowledgements

`POST /oracle/handoffs/ack`

```json
{"handoff_id": "hof_…", "generation": 17, "phase": "preparing|activating|ready|rolled_back|rollback_failed|failed|abandon",
 "reason": "…", "voice_session_id": "…"}
```

- The Host answers `200 {"handoff": record}` with the **current** record
  (current `state` and `revision`) when the phase is applied now, or when this
  `(handoff_id, generation, phase)` was already applied earlier. A repeat
  never repeats effects (no second focus change, cue, Oracle data item or
  event) and never returns a historical record: a repeated `preparing` after
  the handoff became `active` or `cancelled` answers with that `active` or
  `cancelled` record. A phone that lost a response retries the same ack and
  follows what comes back.
- Precedence: the Host looks up `handoff_id` first. A repeat of an applied
  phase is always `200`, even when a newer handoff has since raised the
  principal's generation. `409 {"error": "stale_generation", "handoff": record}`
  is only for a phase that was **not** applied before and whose generation is
  older than the principal's newest. One exception: `abandon` of the current
  active `oracle_to_agent` parent at that parent's own generation is always
  accepted (bookkeeping only, `active -> ended`). A failed return raises the
  principal's generation while the parent stays active, and the phone must still
  be able to end the parent on a local Stop. Every audio transition keeps the
  stale-generation fence. `404 {"error": "unknown_handoff"}` for
  an id the Host holds no record of. That is not a rejection: it never means the
  command was unaccepted and never authorises replaying it. The phone reconciles
  with `GET /oracle/handoff` instead.
- Retention: the Host keeps every non-terminal record, every terminal record that
  still accepts a late ack (`failed/prepare_timeout`, `broken/activation_timeout`),
  and every parent such a record refers to. Only terminal, unreferenced history
  is pruned, to the newest 20 per principal. The principal's highest generation
  is stored separately and never decreases when records are pruned.
- A phase that is not legal from the current state answers
  `409 {"error": "illegal_transition", "handoff": record}`.
- A phone must not start the next local step until the Host has confirmed the
  previous ack. If a response is lost, it retries the same ack, or reads
  `GET /oracle/handoff`, before doing anything irreversible.

Legal transitions:

| From | Ack phase | To | Notes |
|---|---|---|---|
| `offered` | `preparing` | `preparing` | Only within the offer's ttl. |
| `offered` | `failed` | `failed` (`client_failed`), `oracle_call` derived | Preflight failed; nothing was touched. |
| `preparing` | `activating` | `activating` | |
| `preparing` / `activating` | `rolled_back` | `failed` (`rolled_back`), `oracle_call` derived | The phone has the old side capturing again (Oracle for `oracle_to_agent`, hands-free for `agent_to_oracle`). |
| `activating` | `ready` | `active` (`oracle_to_agent`); `ended` / `returned`, parent `ended` / `returned` (`agent_to_oracle`) | New side's capture is running. |
| `activating` | `rollback_failed` | `broken` (`rollback_failed`) | |
| `failed` (`prepare_timeout`) | `rolled_back` | unchanged state, `oracle_call` derived again | Late confirmation that the old side captures again. |
| `broken` (`activation_timeout`) | `ready` | as `activating` + `ready` | The phone's late proof that the new side runs is accepted; only the phone knows. |
| `broken` (`activation_timeout`) | `rolled_back` | `failed` (`rolled_back`), `oracle_call` derived | Late proof that the old side runs. |
| `offered` / `preparing` / `activating` | `abandon` | `cancelled` (`local_stop`, `client_restarted` or `local_intent_changed`), `oracle_call` derived | Bookkeeping only: local Stop or a restarted process. Neither side resumes or starts audio; for a return the parent becomes `ended` with the same reason. |
| `active` | `abandon` | `ended` (same reasons) | Bookkeeping only. |
| any | same phase again | unchanged | Idempotent repeat. |

Every other combination is `409 illegal_transition`. In particular nothing
leaves `ended` or `cancelled`, `rollback_failed` is only legal from
`activating` (earlier phases still own the old side, so they roll back or
abandon), and a `failed` offer never becomes `preparing` again: a new attempt
is a new handoff. `abandon` never causes the Host to resume, feed or close
anything; it only settles the record.

What the phone does with a record it did not expect (a lost response, a
timeout, an event): nothing at all unless that handoff is the **local owner**
(next section) and the phone is still in its transfer. Then, by the record's
state, and only with the acks the table above allows:

| Record | Phone does | Ack |
|---|---|---|
| `active` (`oracle_to_agent`), or `ended` / `returned` (`agent_to_oracle`), while the phone waits on its `ready` | Completes the transfer: closes the old session (for `oracle_to_agent`, with `handoff_id`). | none (a repeated `ready` is harmless) |
| `failed` / `prepare_timeout` | Restores the old side of this direction if it can: Oracle capture for `oracle_to_agent`; hands-free for `agent_to_oracle` (the new side never started). | `rolled_back` once, only if restored |
| `broken` / `activation_timeout` | If the new side's capture runs: keeps it. Else if it restored the old side of this direction: keeps that. | `ready`, or `rolled_back`, once; otherwise none |
| any other `failed` or `broken`, `cancelled`, `ended` | Stops executing the handoff and clears the local owner. It does not restore or start anything the user did not ask for; audio stays as it is. | none |

These records are terminal with no late transition, so the phone sends no ack
for them. A `409` answer to a recovery ack is final: the phone follows the
returned record once more under this table and never retries the rejected ack,
so there is no 409 loop.

## Fencing duplicates and retired events

The phone's handoff coordinator keeps a single **local owner**: the
`(handoff_id, generation)` it is executing, or that established the current
local mode, or none. Starting a mode manually (a new call, picking another
session, Stop) clears it. Only the local owner may cause audio effects.

Per `(host_id, principal)` the phone also keeps the highest `generation` seen
and, per `handoff_id`, the highest `revision` seen and whether it has
**executed** the offer. It drops an event when:

1. `principal` or `host_id` is not its own;
2. `generation` is lower than the highest seen **and** it is an `offered`
   event;
3. its `revision` is not higher than the one already seen for that
   `handoff_id` (the WS/SSE duplicate, or an SSE replay);
4. it is `offered` and this phone already executed that `handoff_id`.

Events that pass are applied in two layers:

- **Bookkeeping**, always: the stored state of that `handoff_id` is updated,
  including later states of lower-generation handoffs.
- **Effects** (pausing, resuming, starting or stopping audio or a mode), only
  when the event's `handoff_id` is the current local owner **and** the local
  mode is still the one that handoff set up or is transferring, in either
  direction. An old `cancelled`, `failed` or `broken` never stops or restores
  anything once a newer manual mode, call or handoff exists.

Two more rules keep a retired offer from retargeting a newer local session:

5. An `oracle_to_agent` offer is executed only when `oracle.voice_session_id`
   is the Oracle call the phone has open now, and no other handoff is the
   local owner.
6. An `agent_to_oracle` offer is executed only when the phone is in hands-free
   with `agent.session` **because of** the handoff named in
   `parent_handoff_id` (it is the local owner). If the user has since picked
   another session, started a call or stopped, the phone acks `failed` with
   `local_intent_changed`; the Host marks the return `failed` and the parent
   `ended` (`local_intent_changed`).

## Capability negotiation

- The Host advertises feature `oracle_handoff` (contract 22) in `/server-info`.
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
   failure: ack `failed`. On success: ack `preparing` (`ttl_ms` 10 000).
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
3. Once `preparing` is confirmed (`ttl_ms` 10 000) it stops hands-free
   capture and settles the final utterance. Settling means **durable local
   admission** of what was captured under the existing stable `client_msg_id`
   and uncertainty rules of `/send`: the utterance is recorded as a send job
   with its id. It does not require network transcription or delivery before
   leaving, and it authorizes no blind retry; the job continues or is resolved
   by the existing send rules, and the phone may keep a failed or uncertain
   job for explicit review. Speech already delivered is never sent again.
   Playback of the agent's current clip may drain. It then acks `activating`.
4. Once `activating` is confirmed it releases the capture and opens
   `/oracle/v2?handoff=hands_free&thread_id=<thread>&handoff_id=<id>`. Opening
   the socket is not success. When the Oracle session has started
   (`session.started` received) and Oracle capture is running, it acks
   `ready`. The return and its parent both become `ended` (`returned`); the
   Host plays `back_to_oracle` on the new call and gives Oracle one
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

A Host record never restarts the microphone, resumes a side or replaces the
phone's current local intent. On launch, after an SSE reconnect, or after an
ack timeout, the phone reads `GET /oracle/handoff` and reconciles bookkeeping
only, comparing each record with the local mode that record's direction
expects:

| Record | Expected local mode | Phone does |
|---|---|---|
| any direction in `offered`, `preparing` or `activating`, and this process is executing it (it is the local owner) | the transfer in progress | continue or roll back as usual |
| any direction in `offered`, `preparing` or `activating`, not the local owner (the process died mid-transfer, or the user took over) | — | ack `abandon` with `client_restarted` or `local_intent_changed`; touch no audio |
| `oracle_to_agent` in `active` (this is `GET.active`) | hands-free with `agent.session`, owned by this handoff | nothing when it matches; otherwise ack `abandon` with `client_restarted` (nothing running) or `local_intent_changed` (another mode runs) |
| `agent_to_oracle` in `ended` / `returned` | an ordinary Oracle call; no handoff owns it anymore | nothing |
| terminal records | — | nothing |

An explicit local Stop during a handoff in any non-terminal state acks
`abandon` with `local_stop` (`cancelled` before `active`, `ended` from
`active`) and stops only what the user asked to stop. Afterwards
`clarp-admin oracle connect oracle` answers `404 no active handoff`.

A Host restart keeps records; phase ttls run again from the restart for
records that were mid-transfer.

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
  (from `activating`, `rollback_failed` if that fails).
- `agent_to_oracle`: fence rule 6 → preflight → ack `preparing` → stop
  hands-free capture, durably admit the final utterance as a send job →
  ack `activating` →
  `stopAlwaysOn`, `startOracleMode` opening `/oracle/v2` with `thread_id` and
  `handoff_id` → on `session.started` with capture running, ack `ready`.
  Rollback: restore always-on only if the same owner still intends it.
- Local Stop or a restarted process mid-handoff: ack `abandon` and touch no
  audio beyond what the user asked for.
- Audio effects only for the local owner; bookkeeping for every event.
- The handoff is transient: it must not write the saved Oracle, AfterISpeak,
  addressing or delegation preferences.
- Wait for each ack's confirmation before the next irreversible step; on a
  lost response, retry the same ack or read `GET /oracle/handoff`.
