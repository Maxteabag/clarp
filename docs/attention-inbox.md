# Attention inbox and native questions

Status: implementation proposal; coordinated Host and iPhone PRs, no deployment.

## User experience

Updates removes the portrait story strip and organizes actionable work into
**Blocking work**, **Other decisions**, and **Ready to review**. Ongoing work
appears in a collapsed **Working** section. Urgency and estimated answer effort
are separate: a difficult blocked question stays important even if it takes
longer than a quick preference. Existing delivery problems and device requests
remain reachable. Unread chat is not automatically a completed deliverable.

Both Updates and the originating conversation render the same native question
control: two or three options with stable IDs, optional descriptions and a
recommended option, plus **Write my own answer**. Selecting an option does not
submit it; the user explicitly sends the selected or custom answer. Existing
Yes/No approvals remain explicit authorization requests. An ordinary question
answer does not grant unrelated authorization.

An ellipsis menu in the top-left header offers **Select items** and **Archived
items**. Selection reveals a bottom action bar. **Archive** hides selected items
from the inbox while preserving their underlying content and work. **Discard**
cancels unanswered decisions/questions, removes selected artifact records, or
discards unsent local items where that action already exists. It never stops a
running agent/job or deletes an artifact's source file. Unsupported discard
actions remain unavailable. Partial failure retains the affected selections and
shows a readable error. Archived items can be restored.

## Host contract

Creation continues to use `POST /decisions`; old requests default to an approval.
New optional fields are:

```json
{
  "response_type": "single_choice",
  "options": [
    {"id": "existing", "label": "Keep the existing layout", "description": "Smallest change"},
    {"id": "compact", "label": "Try the compact layout"},
    {"id": "compare", "label": "Show both first"}
  ],
  "allow_custom_text": true,
  "recommended_option_id": "existing",
  "blocks_progress": true,
  "priority_reason": "The implementation needs a layout choice before it can continue.",
  "urgency": "normal",
  "response_effort": "quick",
  "deadline_at": null
}
```

`response_type` is `approval` or `single_choice`. Questions require 2–3 unique,
nonempty option IDs and readable labels. Custom text is supported for questions;
approval answers remain accepted/rejected. `urgency` is `normal` or
`time_sensitive`; `response_effort` is `quick`, `short`, or `review`. Non-normal
urgency and blocking claims require a nonempty reason. Deadlines use epoch
milliseconds. Legacy defaults are nonblocking, normal urgency, review effort.

The server computes numeric priority rather than accepting arbitrary ranks:
200 for blocking, 100 otherwise, plus 10 for time-sensitive requests. Within
priority, actual deadlines sort first, then creation time and stable identity.
Agents can inspect the pending list to understand competing requests, but never
reorder other agents' records. The phone applies the same ordering across Hosts.

Questions reuse the decision tables, revision checks, expiry, and delivery
outbox, but their outer artifact type is `question`. This prevents older clients
from rendering their hard-coded Yes/No controls for a question. Legacy
`GET /attention` returns approvals only; `GET /attention?decision_format=2`
includes questions. `include_archived=1` additionally includes archived pending
requests. Unsupported clients may show a generic artifact preview; answering
questions requires a supporting client. The server rejects legacy binary
answers to a question.

Serialized decision records and attention items expose the creation fields,
`answer` (null until answered), and `archived_at`. Question resolution uses:

```json
{"expected_revision": 1, "answer": {"option_id": "compact"}}
```

or:

```json
{"expected_revision": 1, "answer": {"text": "Keep the current navigation, but simplify the rows."}}
```

via `POST /decisions/{id}/resolve`. Exactly one answer form is permitted. Option
IDs resolve to immutable labels; custom text preserves the user's casing and
wording, with outer whitespace removed and a finite length limit. Legacy
approval bodies keep `choice: accepted|rejected`. Questions finish as `answered`.
Repeating the same normalized answer is idempotent; a different answer or stale
revision cannot overwrite a completed answer.

`POST /decisions/{id}/dismiss` takes `expected_revision`, marks a pending request
cancelled, and durably notifies the original agent that the user discarded it.
This is neither approval nor an answer; the agent must not guess permission or
repeat the unchanged request. Duplicate dismissal is a no-op. Expiry and
discard notices carry the same non-authorization semantics.

`POST /artifacts/{id}/archive` takes `archived` and `expected_updated_at`.
Archival is separate from completion or cancellation. Artifact listing keeps
archived records available for chat/history; attention excludes them by default.
`POST /artifacts/{id}/discard` takes `expected_updated_at` and soft-deletes a
non-decision artifact record without touching source files. Questions and
approvals use their dedicated dismissal endpoint. These operations are
revision/timestamp guarded and idempotent for an already matching state.

Resolution stores the full answer in the durable delivery snapshot and delivers
it to the owning session using the existing stable message ID. The foreground
and background delivery paths share one formatter. An answer being saved does
not prove that the agent has resumed; `delivery_pending` remains authoritative.

## iPhone behavior

The new client requests attention format 2, decodes optional fields with legacy
defaults, and scopes identities/in-flight state to Host plus decision ID. Shared
question UI serves Updates and conversation artifacts. Text drafts persist
locally by Host and decision; submission requires a reachable Host. Failed or
stale submissions retain input and show a retry/refresh explanation.

Blocking decisions and existing delivery blockers appear in Blocking work;
other questions, approvals and device requests appear in Other decisions.
Ready to review contains published reviewable artifacts, not arbitrary unread
messages, drafts, failed or cancelled artifacts. Ongoing agents/jobs remain in
Working; unread conversations can still be reached from Chats. Opening a result
uses the existing artifact reader; archiving removes it from this inbox.

Server-backed items use durable archive/discard endpoints. Existing local-only
attention items use versioned local archive markers and their existing discard
actions. A meaningfully changed item may reappear; merely refreshing must not
undo an archive. Selection is keyed by stable item identity, never row position.
No swipe gesture is required for bulk actions. Errors from an offline Host
remain visible; successes are removed from selection independently.

## Agent workflow

Extend the maintained `clarp-agent-artifacts` helper with pending-attention
inspection and native question creation. Preserve binary `decision` invocation;
allow optional triage metadata without treating payload fields as authorization.
The skill explains when clarification is worth asking, how to state blocking
impact/effort, and how to continue independent work while awaiting a response.
CLI popup tools remain unavailable; instructions explicitly distinguish them
from supported durable Clarp question artifacts. No agent may self-resolve a
question or approval on the user's behalf.

## Verification

Exercise legacy approvals, option/custom answers, invalid/duplicate options,
same-answer retries, conflicting answers, stale revisions, expiry, discard,
archive/restore, and durable delivery retries in isolated server tests. Verify
legacy attention filtering and rejection of binary answers for questions.
Verify native decoding, deterministic ordering, local archive persistence, and
selection/partial-failure behavior. Simulator checks cover the three buckets,
option/custom answers, inline history, header selection and bottom bulk actions,
including large text. Retain and inspect screenshots from the exact candidate.
