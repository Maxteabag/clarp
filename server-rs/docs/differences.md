# Differences from the Python Host, on purpose

The Rust Host joins the fleet on the contract the apps already speak. Where it differs from the
Python Host in something a client can see, the difference is in this file with its reason, or it
is a bug. The conformance run (`../conformance`) finds the differences; this file explains the
ones that stay.

## Features that are not there yet

The Rust Host leaves a feature it does not have out of `capabilities.features`, which is how an
app learns not to expect it. What belongs to such a feature is left out of both sides before the
conformance run compares them (`NOT_YET` in `conformance/conform.py`).

| Feature | What is absent | Planned |
|---|---|---|
| `send_delivery_state` | `/log`: `delivery_scope`, `delivery_stop_generation`, and `delivery` on a user row; the event `delivery-updated` | Stage 3 |

The Rust Host lists a feature when everything the feature names is built and held to the Python
Host (`FEATURES` in `crates/clarp-host/src/edge.rs`, each with the contract number that
introduced it, as the Python Host's `FEATURE_CONTRACTS` has it). On 15 Nov 2026 these are:
`pairing`, `turn_queue_resume`, `background_jobs`, `background_job_cancel_generation`,
`durable_task_goals`, `artifacts`, `flat_artifact_responses`, `artifact_summary_v1`,
`artifact_pins`, `decision_lifecycle`, `text_input_requests`, `push_grants`, `push_unregister`,
`desktop_send_quiet_push`. Everything else in the Python Host's list is absent, also where a part
of it is built (`attention_index`: `GET /attention` is there, the paged `GET /attention/inbox` is
not; `goal_updater`: a criterion can be set aside, the Goal keeper is not there;
`log_turn_summary`: rows carry the turn's end, an answer that tool work follows is not yet marked
as commentary).

Parts of the contract that no feature stands for, which the Rust Host sends empty for now
(`NOT_BUILT` in `conformance/conform.py`):

| Part | What is empty | Planned |
|---|---|---|
| Portraits and the persona catalogue | `avatar_url` and `model_avatar_url` of an agent; `personas` in the snapshot | Stage 3. An agent that moves keeps its portrait from then on |

## Compared as what a client may rely on

| What | The Python Host | The Rust Host | Why it is compared this way |
|---|---|---|---|
| Revisions | a counter that grows with every write of a row | the log position times 2^20, with room for a reply that grows | The contract promises that a client which holds a cursor is given what changed since, not numbers. The run compares a head as its place among the cursors the capture handed out, and a row's revision as how many of those cursors it is newer than. How two rows that changed between the same two cursors stand to each other, no client can tell, and the Python Host writes them in either order (defects R8, R16) |
| The Host's version in `server-version`, and `clarp_version` in `GET /server-info` | the newest change time of its static files; the version of the Python package | its own version | Each Host's own. The run compares that there is one |
| The id of a row the agent wrote | `live-` and a hash of the agent, the session, the trace and the tool's message id | `r-` and the tool's message id, or the id of the message it answers and a number | An id is the Host's own choice. The run compares that the same row has the same id wherever it is named. The prefix is different on purpose: in the apps `live-` marks a row that has no fixed place in the history yet (the iOS app does not page back from one, `HistoryCursorPolicy`). A row of the Rust Host is stored under one id from its first word and has a place from then on |
| The id of something an agent made or asked (`artifact-...`, `decision-...`), in a path a client calls and in what the agent is told and says back | random | random | The run gives each id one name wherever it is written, also where a preview cut it short, and compares that. Only ids the Host gave out under an id's own key in the same capture: other hex in a text is content |
| When an artifact was pinned, put away, thrown away, and when a request was answered | a reading of the clock | a reading of the clock | The run compares that there is one, and where there is none |
| A queue's revision | counted per change of the queue | the same changes, counted the same: a message that was left for later counts as joining and leaving, also when nothing is in its way | Compared as the number it is |
| The start of the turn that runs, `turn_started_at` in the snapshot | the first busy state the agent entered after its last turn ended | the moment the turn that runs was taken up; at rest 0 | A reading of a clock, which an app counts the running turn from (issue 354). The run compares which turn it is the start of, and which message the client had last sent when it began |

## Differences that are registered one by one

Each has its id in `../conformance/intended.txt`. The id is a hash of the place and both values:
it covers that exact difference and nothing else.

| What | The Python Host | The Rust Host | Why |
|---|---|---|---|
| A plain send that has to wait | changes the queue and announces nothing (defect R5) | announces it with `queue-updated`, like a send with `queue_if_busy` | Every device shows the same queue, not only the one that sent |
| The preview of a notification for a spoken reply | shows an emotion tag the engine acts out, `[sigh]` (defect R10) | does not | A notification shows the reply as every other place shows it |
| `phase` of the reply that closes a later turn | `null` when the turn before had settled, `final` otherwise (defect R6) | `final` | A reply that closes a completed turn is the agent's final word in it, whenever it is read |
| `last_completed_message` after a turn that had waited in the queue | the user's message, though the agent has answered it (defect R12) | the agent's answer | A list of chats shows what the agent last said, whichever way the turn began |
| `model` in an agent's row after a turn that completed | empty after some turns (defect R13) | the model that answered | An app shows which model an agent runs on; a turn that completed has said it |
| The focus after the Host sent an agent something by itself (an answer that is delivered, a goal that is taken up again) | moves to that agent (defect R14) | stays where the user put it | Focus is the agent the user has open, and where a message without a name goes. Only the user moves it |
| `pinned` in the answer to the discard of a pinned artifact | `true`, beside `pinned_at: null` (defect R15) | `false` | A discard unpins. The answer says what is now true |

## Differences the conformance corpus does not reach

| What | The Python Host | The Rust Host | Why |
|---|---|---|---|
| A plain send to a busy agent from something that is not a person | ends the running turn ("legacy other origins preempt", defect R7) | waits its turn | No automation ends a turn a person may be waiting for |
| Pushes to phones | on by default, through the developer's relay | off until the Host is given a relay (`--push-relay`); `tools/install-host` names it | A Host that is started bare speaks to nobody outside. Tests and disposable Hosts never reach a real relay |
| A paired device with limited scope registers a push token | registers a row nobody owns | refused, 403 | A limited device may read and send; it changes nothing on the Host |
| Phones reached through FCM (Android) | pushed to through a Google service account | registered, not pushed to yet | Built when an Android phone is to be reached |
| A push straight to Apple with the Host's own key, Live Activities, the silent push that asks an app to sync, the agent's portrait in a push | there | not there | A Host reaches a phone through the relay: by the phone's grant, or by the Host's credential there. The rest comes with the surfaces it belongs to |
| A message from an agent the Host does not have (an agent of a peer Host) | filed as an agent's, without a sender | filed as an agent's, with the name the sender gave | The reader sees who wrote, and on which Host to answer |
| A send from outside with origin `heartbeat` | accepted when heartbeats are on | always refused with the words for heartbeats that are off | This Host has no heartbeats yet |
| The model an agent's row shows after a turn that was stopped | the model of the last answer in the tool's transcript | the model of the last completed turn | The same thing, read from what the Host has. A turn that was cut off may not have said its model yet |
