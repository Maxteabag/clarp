# Group calls

Status: **implemented, Host contract 60, feature `group_calls`.**

A group call is Car Mode's multi-agent hands-free conversation, presented with
the call UI: the participants' avatars, who is speaking or thinking, a timer,
and mute, route, chat and end controls. Peter's speech goes to one participant
at a time, and each agent replies in its own voice. Unlike the 1:1 agent call,
which is purely client-side, the Host owns the call: who is in it, who is on
hold, and who has the floor. Clarp agents can therefore manage the call for him
("Can we add Mike to this conversation?", "put Solu on hold", "switch me to
Nadia", "call Omar instead").

## Principles

- **The Host owns the record; the phone owns audio.** As with the Oracle
  handoff (`docs/oracle-handoff.md`), the Host never plays or captures
  anything. It keeps the call record, emits an event for each change, and
  steers routing. The phone decides what to play.
- **Nothing on the Host guesses from words to manage the call.** A participant
  changes only through an explicit request: the app's buttons, or an agent
  running `clarp-admin call …` with its own judgement (skill `clarp-calls`).
  Addressing a participant by name ("Mike, …") is Car Mode's ordinary routing,
  and it never adds anyone to the call or takes them off hold.
- **Reuse Car Mode.** A call utterance is an ordinary hands-free `/send`. The
  orchestrator (the message delegator) picks the recipient as it does in Car
  Mode, but only among the call's reachable participants. Replies come back as
  ordinary `audio` events in each agent's own TTS voice.

## The record

One live call per principal (the paired device that owns it). Records are
stored in `settings_store` under `group_call.<principal>`, so a live call
survives an HTTP restart with no schema change. The newest 20 ended calls are
kept for history.

```json
{
  "call_id": "gcl_<32 hex>",
  "host_id": "…",
  "principal": "device_…",
  "state": "live",
  "reason": "",
  "revision": 7,
  "started_at": 1760000000000,
  "ended_at": null,
  "floor": "mike-86db",
  "participants": [
    {"session": "theo-97e5", "agent_id": "a-theo", "persona": "Theo",
     "state": "on_hold", "joined_at": 1760000000000, "changed_at": 1760000100000},
    {"session": "mike-86db", "agent_id": "a-mike", "persona": "Mike",
     "state": "active", "joined_at": 1760000100000, "changed_at": 1760000100000}
  ],
  "server_now": 1760000100500
}
```

- `state`: `live` or `ended`. `reason` says why a call ended: `ended` (someone
  ended it), `empty` (the last participant left), `superseded` (a new call
  replaced it), `stale` (no change for 12 hours).
- `revision` rises by one on every change. Clients drop an event whose
  revision is not newer than the one they hold for that `call_id`.
- `floor` is the session that gets an utterance which names nobody. It is
  always a participant in `active` or `invited` state, or `""` when nobody can
  take speech (everyone is on hold).
- Participant `state`:
  - `invited`: added, not yet spoken to. It can take speech and the floor, and
    becomes `active` the first time speech is routed to it or it gets the
    floor. The app shows it as joining.
  - `active`: in the conversation.
  - `on_hold`: in the call but receives no speech. The phone does not play
    its audio while the call is up.
  - `left`: removed. Kept in the record so the app can show who was there.
  Re-adding a `left` or `on_hold` participant makes it `active` again; it
  keeps its place in the list.

## Endpoints

Full-device authentication, as for `/oracle/connect`. The administrator token
(`clarp-admin`, run by agents) acts on the one live call; with several live
calls it must name `call_id` or `principal`. Every body may carry `call_id`;
without it the request applies to the caller's live call. Agents are named by
session id, persona name, or a spoken approximation of either (see "Names").

| Method and path | Body | Effect |
|---|---|---|
| `GET /calls` | `?principal=` (administrator only) | `{"call": record or null}`: the live call. |
| `POST /calls` | `{"agents": ["theo", …], "request_id"?}` | Start a call with 1..8 agents; the first gets the floor. A live call is ended (`superseded`) unless `request_id` matches it, in which case the same call is returned (a retried create). |
| `POST /calls/add` | `{"agent"}` | Add a participant (`invited`); re-adding resumes. Idempotent. If the call had no floor, the new participant gets it. |
| `POST /calls/remove` | `{"agent"}` | Mark the participant `left`. Removing the floor holder passes the floor (below). Removing the last reachable participant ends the call (`empty`). Idempotent. |
| `POST /calls/hold` | `{"agent"}` | `on_hold`. Holding the floor holder passes the floor. Idempotent. |
| `POST /calls/resume` | `{"agent"}` | Back to `active`; takes the floor if nobody has it. Idempotent. |
| `POST /calls/switch` | `{"agent"}` | Give the floor to this participant (resuming or adding it if needed). Nobody is put on hold. |
| `POST /calls/transfer` | `{"agent"}` | "Call someone else": put the floor holder on hold, then add or resume this agent and give it the floor. |
| `POST /calls/end` | `{}` | End the call: everyone `left`, `state: ended`. Idempotent. |

Every success answers `{"ok": true, "call": record, "changed": bool, "summary":
"Mike joined the call."}`. `summary` is a short sentence an agent can say
aloud. Errors answer `{"ok": false, "error": …}` with 400 (bad body), 401
(limited device), 404 (`no live call`, `unknown agent`), 409 (`ambiguous agent`
with `candidates`, `not in the call`, `several live calls`, or a full call).

Passing the floor: it goes to the participant that most recently had it among
those still `active` or `invited`, else the first such participant in list
order, else `""`.

Agents that cannot be voice targets (Janitors, archived or deleted agents) can
never join a call.

## Routing speech

Car Mode routes in two steps, and a call reuses both. The phone matches a
leading spoken name on the device first (`VoiceAddressingPolicy`). A match
goes out as `POST /send` with `force_session` to that agent. Anything else goes
to the Host's AI router (`POST /orchestrator/route-delegation`). In a call the
phone's name candidates are the call's reachable participants, and an
utterance that names nobody goes to the `floor`, the way Car Mode treats a
selected recipient. Every send in a call carries the new `call_id`. The Host
then enforces the record, so a stale phone cannot reach a held agent:

1. The call must be live and belong to the caller, else `409 {"error": "call
   ended"}` and nothing is dispatched. The phone then ends its call UI.
2. If `session` is not a participant that can take speech (on hold, left, or
   not in the call), the Host uses the call's floor instead. With no floor it
   answers `409 {"error": "nobody to talk to"}`.
3. When the orchestrator runs (a route-delegation, or a hands-free send
   without `force_session`), its candidate agents are only the call's `active`
   and `invited` participants. An on-hold or absent agent is never chosen.
4. When the orchestrator is off or declines, the utterance goes to the floor
   (Car Mode's direct fallback).
5. The participant that received the utterance takes the floor, and an
   `invited` participant becomes `active` (a `route` change with an event).

Replies are ordinary turns: `audio` events carry the agent's own voice, and
`agent-state` drives the speaking and thinking indicators.

### Mid-turn changes

Call changes never stop an agent's work. When a participant is held or
removed, or the call ends, while that agent is mid-turn, the turn finishes and
its reply lands in the agent's chat as usual. A send that arrives after the
change is routed by the new record, so a held agent cannot receive the next
utterance.

The phone never drops a held participant's reply. Clips from an `on_hold`
participant are kept on the phone, and the participant's tile says it has an
update. When it is resumed or gets the floor, the kept clips play in order.
Clips from a participant that `left`, or that arrive after the call ended, are
not played; the reply stays readable in the chat.

## Shared context

In a call, participants hear each other. The Host gives the agent that is
answering a compact call header in its instructions (never in the user's
message, so it does not show up in the chat or `/log`):

```
[Group call] You are Mike, on a voice call with the user, Theo and Nadia. On hold: Omar. The user is talking to you now. …
Since you last spoke:
User -> Theo: What's the plan for Friday?
Theo: Ship the beta on Friday and keep the flag off.
Nadia: The migration is the only risk.
```

- The header is added when the agent is a reachable participant of a live call
  and the call's latest utterance (within 10 minutes) was routed to it. Any
  other turn of that agent gets no header.
- The quotes are the call's user messages to participants and the
  participants' final replies, from the call's start, after this agent's own
  last reply in the call. The utterance being answered is the prompt itself
  and is not quoted. Host notices, automation and tool rows are left out, and
  voice markup is stripped.
- The quotes are capped: the newest 12, at most 1,500 characters in total and
  280 per quote.
- Claude receives it as UserPromptSubmit `additionalContext`; Codex,
  AGY, Grok and OpenCode receive it in the app-turn instruction block that the
  transcript parsers strip back off (`lib.voice_preamble`). The header is read
  only; building it never changes the call.

## Transcription in a call (feature `call_transcription_policy`, contract 67)

Owner settings (via the transcription settings endpoint, read back by the
same GET), unset by default: `call_languages` (one ISO code or a set of up to
three, e.g. `no,en`) and `call_engines` (an ordered fallback list, e.g.
`google:chirp_3,elevenlabs:scribe_v2`). A language set together with an engine
that takes a single language (anything but Google Chirp and ElevenLabs
Scribe) is refused when saved, and so is a set that only Scribe would get.

A clip is in a call only when `/transcribe` carries `X-Clarp-Call-Id` and
`X-Transcription-Session`, the call is live, belongs to this device on this
Host, and the session is a reachable participant of the call record (never UI
focus or name delegation). Otherwise the request is refused before any
provider sees the audio; an absent header is ordinary chat transcription.

Per field, independently: the language is the agent's own
(`agent_languages`), else `call_languages`, else the global language; the
engines are the agent's own (`agent_engines`), else `call_engines`, else the
device's `X-Transcription-Model`, else the global engine. A device header
that is not applied is reported (`requested_model.applied: false` with the
reason), never refused.

Without a call policy (`call_languages` and `call_engines` both unset),
in-call transcription is unchanged: the same engine chain as chat, nothing
skipped and no new refusal, with the sources still reported.

An explicit `X-Transcription-Model: server-default` or `local` from a device
(it is an ordinary header value, and clients may send it) is passed to the
engine exactly as outside a call: the same `transcribe_model_bytes` call with
the value as sent, and the same answer, including a refusal. Only an absent
or empty header falls through to the global engine, as in chat.

With a call policy configured, engines are planned on the real model and what
it does with a language: `server-default` (and `local`) means the configured
local model. Google Chirp takes a language set; ElevenLabs Scribe takes one
code, and a set skips it (Scribe would drop the set and detect any language);
other cloud engines take one code and get the first code of a set
(`language_adapted: "first_code"`); an English-only local model (catalog
`["en"]`, e.g. `small.en`) takes `en` only. Multilingual or unknown local
engines never receive the code, so they are skipped when a call policy is
configured. An incompatible engine is skipped (`{engine, outcome: "skipped",
reason: "language_incompatible"}`); the rest run in order, the next only on
failure.

Either way every attempt records, in `stt.attempts` and the `transcribe`
event, the `language` the Host handed at that engine's boundary: a local
model (faster-whisper's own language, in process or in its worker; the `-l`
value whisper.cpp runs with; OpenAI whisper's `language`), a custom adapter
(none: the protocol carries no language), or the cloud provider adapter (the exact argument; Scribe is handed none for a set). It is
not what a provider sent on, received or detected. `auto` means no language
constraint was handed (not a detection). `null`, or no `language` at all (a
skipped engine), means the boundary was unobserved for that attempt, and only
that: it does not say whether the engine was invoked, and the Host never fills
it in (an attempt can fail without its boundary noted, and an engine with no
recorded boundary, such as a disabled or unavailable one, records `null`).

When a call policy is configured and no engine can take the call language,
the clip is refused before any provider sees it: **409** `{"ok": false, "error": "no compatible engine",
"language": "<code(s)>", "engines": ["<real model ids>"], "attempts": [<the
skips>]}`. Saving settings refuses a policy that can never work (the call
languages against `call_engines`, or, when those are unset, the resolved
global engine), including a later change of the global engine; the readback
adds `call_policy_error` (empty, or why the stored policy can no longer work).
Settings save `call_languages`, `call_engines` and the global engine in one
transaction, and each clip reads all three in one read: a clip resolves,
plans and reports with one policy, never a mix of an old and a new one. A
stored policy that can never work (written around the settings check, an
unreadable value, or broken by a later global engine) is refused for every
call clip before any provider: **409** `{"ok": false, "error": "call policy
invalid", "call_policy_error": "<why>", "language": "<stored call_languages>",
"engines": ["<call_engines as real model ids>"], "attempts": []}`. It is never
treated as unset or global, and the call record then omits `transcription`.

The call record (`GET /calls`, the answer's `call`, route responses) adds
`transcription: {session: {language: {value, source}, engines: {value,
source}}}`. It is a **live derived view, not a snapshot**: the Host computes
it at each read from the same per-field resolution the next clip would use,
never stores it, and never sends it on the `group-call` event. A policy or
override change mid-call shows at the next read; the per-clip `stt` of a
transcription stays the authority for what actually ran.

**Absence of `transcription` means unknown**, never "reset to global". It can
mean any of:
- neither a configured call policy nor a listen-first call makes it relevant
  (a say-path call stays identical to before, even with agent overrides);
- every participant field resolves to `global`;
- the call is not live (ended, superseded);
- computing the view failed;
- an older Host.

A client omits `X-Transcription-Model` on call-scoped requests only when the
view explicitly shows `engines.source` exactly `"call_policy"` for the
captured participant (and the Host advertises `call_transcription_policy`).
Absence, or any other source, fails safe: keep sending the header.

## Events

Every change emits one durable SSE event:

| `type` | Fields |
|---|---|
| `group-call` | `call_id`, `host_id`, `principal`, `state`, `reason`, `revision`, `started_at`, `ended_at`, `floor`, `participants`, `change`, `server_now` |

`change` is `{"action": "add"|"remove"|"hold"|"resume"|"switch"|"transfer"|
"start"|"end"|"route", "session": …, "by": …}`. `by` is the agent session that
made the change through the CLI, or `""` when the phone or Host did. A client
applies the event only for its own `principal` and only when `revision` is
newer than what it holds. On reconnect, `GET /calls` is the snapshot.

## Agent logs

Each change writes a quiet notice into the chat of every agent the change
concerns: the agent added, held, resumed, removed, given the floor, and the
other participants for joins, departures and the end of the call. Notices are
assistant-role rows with origin `system`. They render in the chat, never push,
badge or mark unread, and are never spoken. Examples: "Group call: Mike joined
(with Theo).", "Group call: you are on hold.", "Group call ended."

## Names

`clarp-admin call` and every endpoint resolve a name in this order:

1. an exact session id or persona (case-insensitive) among contacts that can
   join a call (as `oracle_contact.resolve_visible`);
2. a spoken approximation: the name is lower-cased and stripped of everything
   except letters and digits, and compared with each persona and session stem
   (`mike` for `mike-86db`) for equality, a prefix of at least three
   characters, or a similarity of at least 0.75 (`difflib`, as the
   orchestrator's name candidates). Participants of the call are preferred
   over the rest of the roster.

Exactly one best match resolves. Two or more equally good matches answer `409
ambiguous agent` with `candidates` (persona and session), so the agent can ask
"Mike Ross or Mikael?". No match answers `404 unknown agent`.

## CLI and skill

```bash
clarp-admin call status                    # the live call as JSON
clarp-admin call start AGENT [AGENT …]
clarp-admin call add|remove|hold|resume|switch|transfer AGENT
clarp-admin call end
# any of them: --call gcl_…  --principal device_…
```

The managed skill `skills/clarp-calls/SKILL.md` teaches agents when to use
them and to confirm aloud what happened, using the response's `summary`. An
agent in the call may bring someone in itself ("let me bring in Nadia"): it
says so aloud, and adds only agents the user asked for, unless the addition is
clearly helpful and it says why.

## iOS

The app gates group calls on `group_calls` in `/server-info` features. An older
Host keeps the client-only 1:1 call, unchanged.

- A 1:1 call on a capable Host is a call record with one participant.
  Adding someone from the roster turns it into a group call; the same screen
  shows the grid.
- The call screen becomes a participant grid (FaceTime-like): avatar, name,
  speaking (audio playing for that agent) and thinking (`agent-state`)
  indicators; on-hold dimmed; invited shown as joining. Tapping a participant
  offers switch to, hold or resume, and remove. The add button opens the
  roster.
- Capture, VAD, addressing and playback are Car Mode's hands-free pipeline
  (always-on capture, `VoiceAddressingPolicy`, the route-delegation fallback,
  the playback queue). The call passes `call_id` on each send, uses the
  reachable participants as name candidates and the Host's `floor` as the
  selected recipient, and skips clips from held or departed participants.
- `group-call` events update the screen live; `GET /calls` refreshes it after
  a reconnect or launch. An event that ends the call closes the screen.

## Compatibility

Host contract 60 (taken after coordination), feature `group_calls`: new
endpoints, the `group-call` SSE event, and the optional `call_id` on `/send`.
Older clients never send `call_id` and ignore the event.
