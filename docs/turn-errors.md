# Turn errors: one plain sentence, the right buttons

Peter, 28 Nov 2026: "Menneskelige feilmeldinger i chatten. 100% enig. La oss bli absolutt best på
dette."

When a turn fails, the Host says what happened and what the user can do in one short sentence, in
Norwegian and English, with the buttons that fit. The provider's raw words stay behind a
disclosure. The Host decides all of this (one source of truth); the apps only draw it.

Feature `turn_error_explained` (Host contract 217). Endpoint `POST /turn-retry` (feature
`turn_retry`, same contract).

## Where the error object appears

The same `error` object (below) is attached to:

1. **`/log` rows** that say why a turn failed (`origin:"system"`, the row that carries
   `failure_kind`), and the Host's `turn-interrupted` notice rows (`kind:"notice"`): `row.error`.
   Old rows are explained when they are read, so history improves too.
2. **The live turn** (`op:"turn"` with `status:"failed"`): `turn.error` keeps `reason`, `message`
   and `detail` as before, and gains the fields below.
3. **`agent-activity`** of an agent whose last turn failed: `error` (same object), so a chat list
   can show the sentence instead of "Turn failed. Your message is saved.".

An app that does not know the feature keeps showing `text` as today.

## The error object

```json
{
  "code": "usage_limit",
  "key": "error.usage_limit_until",
  "params": {"backend": "Codex", "model": "gpt-6.1", "time_ms": 1795850820000},
  "text": {
    "en": "Your Codex account is out of usage until 18:27.",
    "nb": "Codex-kontoen din er tom for kvote til kl. 18:27."
  },
  "severity": "warning",
  "actions": [
    {"id": "retry_at", "at_ms": 1795850820000,
     "label": {"en": "Try again at 18:27", "nb": "Prøv igjen kl. 18:27"}},
    {"id": "switch_model", "label": {"en": "Switch model", "nb": "Bytt modell"}},
    {"id": "copy_details", "label": {"en": "Copy diagnostics", "nb": "Kopier diagnose"}}
  ],
  "detail": "You've hit your usage limit. Upgrade to Pro … or try again at 6:27 PM.",
  "backend": "codex",
  "model": "gpt-6.1",
  "message_id": "m-8f2c…",
  "trace_id": "a247fdb8045c08b8",
  "resets_at_ms": 1795850820000,
  "fingerprint": "usage_limit:codex:5f1a09c2",
  "copy_text": "A Clarp agent turn failed. Diagnose it …"
}
```

- `code`: stable, from the list below. Apps may pick an icon by it; never parse `text`.
- `key` and `params`: the sentence's key in the app's own catalog and what fills it (see Catalog
  keys below). **An app says the sentence from its catalog, in the user's chosen language**, and
  formats `time_ms` with the platform's formatter in that locale. It falls back to `text` only
  for a key it does not have, or when a placeholder its string uses is empty in `params`.
- `text`: the Host's own sentence in English and Norwegian, the fallback above (`nb` for a
  Norwegian app language, `en` otherwise).
- `severity`: `error` (red), `warning` (amber: a limit or wait, nothing broken), `info` (grey).
- `actions`: in order of importance; draw the first as the primary button. The button's words
  come from the catalog (`error.action.<id>`); its `label` (en/nb) is the fallback. Unknown `id`s
  are skipped.
- `detail`: the provider's raw words (up to 1000 characters), for the disclosure. May be empty.
- `message_id`: the user message the turn was for; what `retry` sends again. May be empty (a
  heartbeat, a goal wake): then the retry actions are left out.
- `fingerprint`: equal for the same failure. Used to collapse repeats (below).
- `copy_text`: what `copy_details` puts on the clipboard: a short prompt for an agent with the
  code, backend, model, trace, time and raw words.

## Catalog keys

Shared with every client's catalog (Android writes `_` for `.`: `error_usage_limit`,
`error_action_retry`). English is the source; clarp-i18n translates. Placeholders are named
here; each platform uses its positional form. `{time}` is `time_ms` (or an action's `at_ms`)
formatted by the platform in the app's locale: the time alone when it is today, else with the
weekday within a week, else with the date. Backend and model names are never translated.

| key | English | params |
|---|---|---|
| `error.usage_limit` | Your {backend} account is out of usage. Try again later, or switch model. | backend |
| `error.usage_limit_until` | Your {backend} account is out of usage until {time}. Try again then, or switch model. | backend, time |
| `error.billing` | Your {backend} account is out of credits. Top it up, or switch model. | backend |
| `error.auth` | Your {backend} sign-in has expired. Sign in again, then try again. | backend |
| `error.account_plan` | Your {backend} plan does not include {model}. Switch model or account. | backend, model |
| `error.free_model_restricted` | This free OpenCode model only runs in OpenCode's own apps. Choose another model. | |
| `error.connection` | Lost the connection to {backend}, and the Host's retries did not get through. Try again. | backend |
| `error.network_gone` | This computer had no network for 15 minutes, so the turn stopped. Try again once it is online. | |
| `error.provider_busy` | {backend} is overloaded right now, and the Host's retries did not get through. Try again in a minute, or switch model. | backend |
| `error.provider_down` | The provider of {model} is unavailable right now. Try again later, or switch model. | model |
| `error.invalid_request` | {backend} rejected the conversation history. Start a fresh session with the same conversation and try again. | backend |
| `error.refused` | The model refused to answer (a safety filter blocked it). Rephrase, or switch model. | |
| `error.prompt_too_long` | This conversation has grown too long for the model. Compact it and continue. | |
| `error.timeout` | {backend} stopped responding. Try again. | backend |
| `error.runner_exit` | {backend} stopped unexpectedly. Try again. | backend |
| `error.ended_from_outside` | Something outside Clarp ended the turn. Send the message again. | |
| `error.interrupted` | The turn was cut off before it finished. Send the message again. | |
| `error.not_started` | The Host could not start the turn. Try again. | |
| `error.folder_missing` | This agent's folder {folder} no longer exists. Recreate it, or make a new agent in another folder. | folder |
| `error.folder_missing_no_path` | This agent's folder no longer exists. Recreate it, or make a new agent in another folder. | |
| `error.provider_mismatch` | This agent ran on another model provider than the one it is bound to. Check its Codex home and provider. | |
| `error.session_locked` | The {backend} session is busy in another process. Try again in a moment. | backend |
| `error.moved` | This agent moved to another Host. Continue the conversation there. | |
| `error.unknown` | The turn failed at {backend}. Try again, or switch model if it happens again. | backend |
| `error.action.retry` | Try again | |
| `error.action.retry_at` | Try again at {time} | time (the action's `at_ms`) |
| `error.action.fresh_retry` | New session, try again | |
| `error.action.compact_retry` | Compact and continue | |
| `error.action.switch_model` | Switch backend or model | |
| `error.action.sign_in` | Sign in again | |
| `error.action.copy_details` | Copy diagnostics | |
| `error.details` | Details | |
| `error.repeat_count` | {count}× (plural) | count |
| `error.copied` | Copied | |
| `error.retry_scheduled` | Will try again at {time} | time |
| `error.retry_sent` | Sent again | |
| `error.action_failed` | That didn't work. Try again. | |

`copy_text` is English on purpose (it is for an agent) and stays out of the catalogs.

## Codes

| code | when | severity | actions |
|---|---|---|---|
| `usage_limit` | the account's usage window is spent | warning | retry_at (when the reset is known), switch_model, copy_details |
| `billing` | credits, payment, a hard or monthly limit: no reset clears it | error | switch_model, copy_details |
| `auth` | the sign-in ran out | error | sign_in, retry, copy_details |
| `account_plan` | the plan lacks the model | warning | switch_model, copy_details |
| `free_model_restricted` | an OpenCode free model refused outside OpenCode | warning | switch_model |
| `connection` | the link to the provider broke, retries used up | error | retry, copy_details |
| `network_gone` | the computer had no network for 15 minutes | error | retry |
| `provider_busy` | overloaded, rate limited, 429/5xx after the Host's own retries | warning | retry, switch_model, copy_details |
| `provider_down` | the model's provider is unavailable | error | retry, switch_model, copy_details |
| `invalid_request` | the provider rejected the request as malformed (bad tool or call ids, an empty tool result, invalid payload, 400). Trying the same session again replays the same history, so the first action starts a fresh one | error | fresh_retry, switch_model, copy_details |
| `refused` | the model refused (a safety filter blocked the answer) | warning | switch_model, copy_details |
| `prompt_too_long` | the conversation no longer fits the model | warning | compact_retry on Claude, fresh_retry elsewhere; switch_model |
| `timeout` | the backend stopped responding | error | retry, copy_details |
| `runner_exit` | the backend's process ended unexpectedly | error | retry, copy_details |
| `ended_from_outside` | a signal from outside the Host ended the turn | info | retry |
| `interrupted` | the turn was cut off before it finished (a restart, a process that went without a word) | info | retry |
| `not_started` | the Host could not start the turn | error | retry, copy_details |
| `folder_missing` | the agent's folder is gone | error | copy_details |
| `provider_mismatch` | the agent ran on another provider than it is bound to | error | copy_details |
| `session_locked` | the backend's session is held by another process (a Codex thread with another active writer, a locked Grok session) | warning | retry |
| `moved` | the agent moved to another Host | info | (none) |
| `unknown` | anything else | error | retry, switch_model, copy_details |

## Actions

| id | what the app does |
|---|---|
| `retry` | `POST /turn-retry {"session", "message_id"}`: the Host sends the same message again (text and attachments) as a new turn. |
| `retry_at` | `POST /turn-retry {"session", "message_id", "at_ms"}`: the Host sends it again at that time. The card then reads the label's time as scheduled. |
| `fresh_retry` | `POST /turn-retry {"session", "message_id", "fresh": true}`. The turn after one whose history the provider rejected (`invalid_request`), or one too long on a tool that cannot compact (`prompt_too_long` off Claude), begins a new session by itself that opens with the Host's record of the conversation, so this is a try again whose turn is fresh. Any new message of the user's is fresh the same way. |
| `compact_retry` | `POST /turn-retry {"session", "message_id", "compact": true}`: the Host compacts the conversation, then sends the message again. |
| `switch_model` | Open the app's model/backend picker for this agent. |
| `sign_in` | Open the app's sign-in for `backend` (the action carries `"backend"`). Leave it out if the app has none. |
| `copy_details` | Copy `copy_text` to the clipboard. |

`POST /turn-retry` answers `{"ok": true, "status": "queued" | "scheduled" | "compacting" | "refreshing"}`, or 404
when the message is not in the agent's conversation, 409 when it has a newer answer already.

## Drawing it

- One card: an icon by severity, the sentence, the buttons, and a "Details" disclosure (closed)
  that holds `detail`, then `backend · model · trace_id` in small monospace, selectable.
- **Repeats collapse.** Rows whose `error.fingerprint` is the same, with no reply of the agent's
  between them (user messages and system rows may lie between), are drawn as one card at the
  newest of them, with a count ("3×"). The older ones are hidden; the user's messages stay.
- **A failure the Host recovers from is not an error.** Retries, account switches and waits for
  the network or an account show on the working line ("Reconnecting", "Waiting for an account
  until 18:27"), never as a card.
- A Stop is not an error: it stays the quiet "Stopped." line.
- `retry_at` whose `at_ms` has passed is drawn as `retry` ("Try again"), since the window has
  reset; a card cached since then must not offer a time in the past.

## What the Host recovers from by itself

- A dropped connection or a timeout: tried again after 2, 4, 8, 16 and 30 s; with the network
  gone, it waits up to 15 minutes for it.
- A provider that is overloaded or rate limited: tried again after 30 and 60 s.
- A usage limit or an expired sign-in, where the Host is set to choose accounts: another account
  takes the turn.
- A session held by another process (`session_locked`): tried twice more, 5 and 10 s later.
- A session whose history the provider rejected (`invalid_request`): the next turn opens a fresh
  session with the Host's record of the conversation.

Only when these are used up does the user see a card.
