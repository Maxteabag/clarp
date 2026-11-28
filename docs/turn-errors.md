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
  "key": "turn_error.usage_limit",
  "text": {
    "en": "Your Codex account is out of usage until 18:27.",
    "nb": "Codex-kontoen din er tom for kvote til kl. 18:27."
  },
  "severity": "warning",
  "actions": [
    {"id": "retry_at", "at_ms": 1795850820000,
     "label": {"en": "Try again at 18:27", "nb": "Prøv igjen kl. 18:27"}},
    {"id": "switch_model", "label": {"en": "Switch model", "nb": "Bytt modell"}},
    {"id": "copy_details", "label": {"en": "Copy for AI", "nb": "Kopier for AI"}}
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
- `key`: `turn_error.<code>`, for an app that wants its own strings. The Host's `text` is the
  default and is always present.
- `text`: one sentence per language. Show `nb` when the device's preferred language is Norwegian
  (`nb`, `nn`, `no`), `en` otherwise.
- `severity`: `error` (red), `warning` (amber: a limit or wait, nothing broken), `info` (grey).
- `actions`: in order of importance; draw the first as the primary button. Each has its own
  `label` in both languages. Unknown `id`s are skipped.
- `detail`: the provider's raw words (up to 1000 characters), for the disclosure. May be empty.
- `message_id`: the user message the turn was for; what `retry` sends again. May be empty (a
  heartbeat, a goal wake): then the retry actions are left out.
- `fingerprint`: equal for the same failure. Used to collapse repeats (below).
- `copy_text`: what `copy_details` puts on the clipboard: a short prompt for an agent with the
  code, backend, model, trace, time and raw words.

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
