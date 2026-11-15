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
`desktop_send_quiet_push`, `snapshot_without_archived`, `helper_agents`, `media`, `html_reports`,
`html_report_revisions`, `html_form_network`, `attention_index`. Everything else in the Python Host's list is absent, also where a part
of it is built (`goal_updater`: a criterion can be set aside, the Goal keeper is not there;
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
| Revisions | a counter that grows with every write of a row, one for the whole Host | the agent's own log position times 2^20, with room for a reply that grows | The contract promises that a client which holds a cursor is given what changed since, not numbers. The run compares a head as its place among the cursors the capture handed out for that conversation, and a row's revision as how many of those cursors it is newer than. How two rows that changed between the same two cursors stand to each other, no client can tell, and the Python Host writes them in either order (defects R8, R16) |
| The Host's version in `server-version`, and `clarp_version` in `GET /server-info` | the newest change time of its static files; the version of the Python package | its own version | Each Host's own. The run compares that there is one |
| The id of a row the agent wrote | `live-` and a hash of the agent, the session, the trace and the tool's message id | `r-` and the tool's message id, or the id of the message it answers and a number | An id is the Host's own choice. The run compares that the same row has the same id wherever it is named. The prefix is different on purpose: in the apps `live-` marks a row that has no fixed place in the history yet (the iOS app does not page back from one, `HistoryCursorPolicy`). A row of the Rust Host is stored under one id from its first word and has a place from then on |
| The id of something an agent made or asked (`artifact-...`, `decision-...`), in a path a client calls and in what the agent is told and says back | random | random | The run gives each id one name wherever it is written, also where a preview cut it short, and compares that. Only ids the Host gave out under an id's own key in the same capture: other hex in a text is content |
| The id of a file an agent published (`asset_...`), under its own key, in `url`, `uri` and `markdown`, and in the path it is fetched by | `asset_` and 24 random signs | `asset_` and 24 random signs | An id is random on both. It is named in the order a client first learns of it, wherever it is written: what a client relies on is that the id it was given fetches the file |
| When an artifact was pinned, put away, thrown away, and when a request was answered | a reading of the clock | a reading of the clock | The run compares that there is one, and where there is none |
| A queue's revision | counted per change of the queue | the same changes, counted the same: a message that was left for later counts as joining and leaving, also when nothing is in its way | Compared as the number it is |
| The start of the turn that runs, `turn_started_at` in the snapshot | the first busy state the agent entered after its last turn ended | the moment the turn that runs was taken up; at rest 0 | A reading of a clock, which an app counts the running turn from (issue 354). The run compares which turn it is the start of, and which message the client had last sent when it began. `live_activity.turn_started_ms` is the same moment here. On the Python Host it is the start of the newest message's turn and begins again when more is sent to an agent that works (397 of 2,899 working stretches on elitebook in four days, found by the review of PR 357), so the apps count from `turn_started_at` only |

## Differences that are registered one by one

Each has its id in `../conformance/intended.txt`. The id is a hash of the place and both values:
it covers that exact difference and nothing else.

| What | The Python Host | The Rust Host | Why |
|---|---|---|---|
| A plain send that has to wait | changes the queue and announces nothing (defect R5) | announces it with `queue-updated`, like a send with `queue_if_busy` | Every device shows the same queue, not only the one that sent |
| The preview of a notification for a spoken reply | shows an emotion tag the engine acts out, `[sigh]` (defect R10) | does not | A notification shows the reply as every other place shows it |
| `phase` of the reply that closes a later turn | `null` when the turn before had settled, `final` otherwise (defect R6) | `final` | A reply that closes a completed turn is the agent's final word in it, whenever it is read |
| `last_completed_message` after a later turn of an agent: one that had waited in the queue, or a plain one that was not its first | the user's message, though the agent has answered it (defect R12) | the agent's answer | A list of chats shows what the agent last said, whichever way the turn began |
| `model` in an agent's row after a turn that completed | empty after some turns (defect R13) | the model that answered | An app shows which model an agent runs on; a turn that completed has said it |
| `pinned` in the answer to the discard of a pinned artifact | `true`, beside `pinned_at: null` (defect R15) | `false` | A discard unpins. The answer says what is now true |
| A reply, to a client that catches up by revision | given again after the next message arrived: the Host wrote it a second time (defect R16) | given once | A row that did not change is not sent again |
| `live_activity` in an agent's row after its first turn | missing until the next turn, in some runs (defect R17) | there: at rest, since the turn's end | An app's line for an agent says what the agent last did, whichever turn that was |
| What a tool that died left on stderr, as the state of the failed turn gives it (`detail.error`) | as the tool wrote it, with the end of its last line | the same words without the line's end | The words are what failed, the line's end is how a terminal was told. `detail.provider_message` and the row under the message carry them without it on both Hosts |
| `phase` of the last thing a turn said before it failed | `final` | none | A turn that failed has no final word: the row below says why it ended |

## Differences the conformance corpus does not reach

| What | The Python Host | The Rust Host | Why |
|---|---|---|---|
| A plain send to a busy agent from something that is not a person | ends the running turn ("legacy other origins preempt", defect R7) | waits its turn | No automation ends a turn a person may be waiting for |
| Pushes to phones | on by default, through the developer's relay | off until the Host is given a relay (`--push-relay`); `tools/install-host` names it | A Host that is started bare speaks to nobody outside. Tests and disposable Hosts never reach a real relay |
| A paired device with limited scope registers a push token | registers a row nobody owns | refused, 403 | A limited device may read and send; it changes nothing on the Host |
| Phones reached through FCM (Android) | pushed to through a Google service account | registered, not pushed to yet | Built when an Android phone is to be reached |
| A push straight to Apple with the Host's own key, Live Activities, the silent push that asks an app to sync, the agent's portrait in a push | there | not there | A Host reaches a phone through the relay: by the phone's grant, or by the Host's credential there. The rest comes with the surfaces it belongs to |
| A message from an agent the Host does not have (an agent of a peer Host) | filed as an agent's, without a sender | filed as an agent's, with the name the sender gave | The reader sees who wrote, and on which Host to answer |
| A send that names its sender and an origin beside it (`user`, `oracle`, `schedule`) | taken as the origin it names (defect R18) | that agent's: `agent` | Who a message is from decides whether the focus moves and whether a Stop holds it. An agent is not a person because it says so |
| A turn whose account is out, on a machine with an account selector | the Host runs the owner's selector: it stops every running turn of that tool, lets the selector put another login in place, and resumes them all | never changes a login and stops no turn: the turn waits and is taken up when the login has changed, or ends after half an hour (`--accounts follow`) | Two Hosts on one machine share the tools' logins. The one that has the selector changes them; the other follows. A selector of this Host's own comes when a machine has no other Host |
| A send from outside with origin `heartbeat` | accepted when heartbeats are on | always refused with the words for heartbeats that are off | This Host has no heartbeats yet |
| The model an agent's row shows after a turn that was stopped | the model of the last answer in the tool's transcript | the model of the last completed turn | The same thing, read from what the Host has. A turn that was cut off may not have said its model yet |
| A model an agent is set to that its tool does not have | refused for Codex when the Host has the list `codex debug models` gave it, and by `clarp-admin model` for every tool, from the catalogue the apps read | taken; the tool says so at the agent's next turn | This Host has no catalogue of models yet. It comes with accounts and usage |
| Two names that differ only in letters that fold to others (`ß` and `ss`) | one name (`str.casefold`) | two names (small letters compared) | Names are compared without their case. The rest of the folding table is not carried for a case no fleet has |
| The session name of a new agent | the name asked for when nobody has it, else the agent's name in small letters and four random letters (`maximus-6c37`) | the name asked for when nobody has it, else the agent's name in small letters, with random letters only when that is taken (`maximus`) | A session name is the Host's to choose and a client's to copy. The shorter one is easier to say and to type |
| A helper that answers its maker's request (`clarp-admin reply`) | has reported, and the apps are not told | has reported, and the apps are told to read the list again | Every move of a helper is announced the same way |
| A file an agent publishes, when nobody says for which agent (`POST /media` without `X-Session`), and a gallery asked for without a session | the agent the Host is configured to default to | refused with `session required`; an empty gallery | This Host has no agent it falls back to: every request names its agent |
| A published file that is text, fetched by a client that takes gzip | compressed | as it is | What agents publish is mostly compressed already; a client that asks for gzip is given a file it can read either way |
| What a file with no usable name or extension is called and kept as | by the machine's table of file types (`mimetypes`, which reads `/etc/mime.types` where there is one) | by Python's own built-in table, the part of it a published file can ask for | The same file is read the same on every machine. An image is told by its first bytes on both |
| `POST /tools/media` | not there | the same as `POST /media`, for the token a turn is given for its agent's tools | That token opens `/tools/` and nothing else, and publishing a file is a tool's act |
| A file one agent published and another agent shows, after the second has moved to this Host (`clarp-host import`) | does not arise: the files of all agents are in one table | kept in the log of the agent that shows it too, marked as one that came along: fetched under its id, not in that agent's gallery, and no artifact of that agent is made of it. An id stands for one file on the Host: another file under it is refused | An agent's log holds what the agent needs to be shown whole. The file is kept once on the disk, whoever names it |
| An artifact that names a published file, after its agent's session has another name | not found for this session: the file is looked up by the session name it was published under | found: an agent's files are in the agent's own log | A file belongs to the agent that published it, whatever the agent is called |
| The name of a list of Updates (`snapshot_revision` of `GET /attention/inbox`) and the cursor a page ends with | a digest of the rows as its table has them; a cursor is that name and a place, in base 64 | a digest of the same fields as this Host has them; a cursor of the same shape | Each Host's own: a client hands a cursor back and compares names, and reads neither. The run compares whether two pages are of one list and whether a page has more after it. A cursor of one Host is stale on the other |
| A countdown in Updates | shown where its clock stands | not shown | Countdowns are not built |
| An artifact with a `podcast` in its payload | read as an episode (`podcast_live`) | refused: podcast episodes are not available on this Host yet | Not built. What is not there says so |
| A form whose schema of answers has a word of JSON Schema other than `type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, `minLength`, `maxLength`, `minItems`, `maxItems`, `minimum`, `maximum`, or the words that say nothing of an answer (`title`, `description`, `default`, `examples`, `format`, `deprecated`, `readOnly`, `writeOnly`, `$comment`, `$schema`, `contentMediaType`, `contentEncoding`) | taken: a library reads all of JSON Schema (jsonschema, draft 2020-12) | refused when the form is made, with the word: `answer_schema says \`pattern\`, which this Host does not read yet` | This Host reads a schema itself, and says of answers that do not fit what the library says first (1,423 recorded pairs). A form is not taken whose answers it would judge otherwise. The four forms of the measured week use `type`, `properties` and `enum`. A word that means nothing to the library means nothing here either |
| Of several answers nobody asked for that do not fit a schema given as `additionalProperties`, which one the user is told of | whichever the library's set gives first: not the same from one start of the Host to the next | the first as the answers were sent | The library leaves it open |
| A whole number in a form's answers that 64 bits do not hold | kept and shown as it was written | read as a fraction | No form asks for one |
| What a form's page logs of its use (`POST` and `GET /artifacts/<id>/events`, the feature `html_form_events`), and the tool's `form-events` | kept, and read back | not there; the tool says it is not available on this Host yet | Not built. No form of the measured weeks logged anything |
| A user's wish to have the agent's reply to their answers spoken (`synthesize_audio`, the feature `html_form_reply_audio`) | the reply is spoken | kept with the answers; nothing is spoken | Speech out is not built |
| A form's answers whose agent is put away | kept, and told when the agent is taken out again | told at once | Not compared yet for any kind of message: what this Host does with what is said to an agent that is put away is still to be held to the Python Host |
| A summary of a report (`representation=summary-v1`) in a list so large that the Host's budget for hashing runs out | `content_omitted` without its digest, marked `hash_pending` | always with its digest and length | This Host keeps a page under its digest: it has nothing to compute |
| A report whose revision is published again as it is, or as it once was | answered with the report as it stands, and announced as changed (`artifact-updated`) | answered the same, and nothing is announced | Nothing changed |
| A report's revision that names no version | `revision-` and the 32 hex digits of a random UUID | `revision-` and 32 random hex digits | Random on both |
| A goal's updater (`updater` in a plan: whether a janitor keeps the goal's bookkeeping, which, when it last ran, its last receipt) | as the janitor left it | off, with nothing in it | Janitors are not on this Host. A goal that moves here is the same goal in everything else (rehearsed on 15 Nov with 18 real plans) |

