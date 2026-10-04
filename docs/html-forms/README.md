# HTML form artifacts

A `html_form` artifact is an immutable, self-contained HTML document with a
string `version` and an object `answer_schema` (JSON Schema 2020-12, inlined
without references). It uses existing authenticated artifact creation/listing.
HTML payloads allow up to 4 MiB; submissions allow 128 KiB of finite JSON.
These are transport limits, not question-count or visual-design rules.

Publish with `clarp-agent-artifacts create-form SESSION TITLE HTML_FILE SCHEMA_FILE
--version VERSION --artifact-id ID`. A client-selected artifact ID lets the
publisher reconcile an ambiguous create before retrying. Changed content or
questions require a new artifact, preserving earlier responses and drafts.

`POST /artifacts/{id}/submit` accepts `{submission_id, version, answers}` and
returns `{accepted, submission_id, artifact_id, version, delivery_status}`.
Host contract 50 accepts optional boolean `synthesize_audio`, default true as for
ordinary user messages. Native clients persist their current reply-audio/mute
choice in the original submission body. The Host snapshots it with the receipt;
retries cannot alter the accepted preference. Delayed form delivery uses the
same voice preamble and configured synthesis as an ordinary user turn. Existing
receipts remain unchanged and are never replayed by this repair.
The recipient is resolved from the stored artifact, never client input. The
receipt and pending delivery snapshot are committed in the same transaction.
An identical ID/payload retry returns the existing receipt. Reusing an ID for
different answers is rejected. The normal dispatch worker uses a stable
`html-form-{submission_id}` request ID with durable queueing and no steering.
Archiving hides the inbox entry without closing a ready or active form.
Completed, cancelled, expired, or discarded forms reject new answers; an
identical retry of an already accepted submission still returns its receipt.
Receiving a receipt proves acceptance, not completed agent work. Failed dispatch
stays pending for retry. Submissions are preferences, not protected-action approval.

HTML bytes are preserved rather than passed through conversational markup
stripping. The native renderer confines scripts and assets; never serve this
HTML directly on a credential-bearing Host origin. Existing artifact types,
quick decisions and task plans retain their contracts.

A read-only report is an `html_form` with `read_only: true` and no answers:
the Host stores an empty closed `answer_schema` and refuses submissions with
400. Publish with `clarp-agent-artifacts create-report SESSION TITLE HTML_FILE
[--summary S] [--artifact-id ID] [--version V]`; the default ID is derived
from the title and HTML, so an identical retry reconciles to the same artifact.
See `docs/protocol.md`.

See the managed `clarp-html-forms` skill for authoring, draft restoration,
custom controls and publishing. Native implementation and testing live in the
separate iOS repository.


Interactive pages may use `window.clarpForm.submit(answers, {stayOpen: true})`
when `window.clarpForm.capabilities?.stayOpen` is true. This native presentation
option does not change the Host submission contract. After durable acceptance,
the native wrapper retains the WebKit page, archives its local receipt and
prepares a fresh submission ID only for the next explicit Send. Existing HTML
can choose **Send and keep open** in native confirmation; it needs no payload
revision. Default submissions return to chat as before. Ambiguous retries keep
the original ID, payload, reply-audio preference and presentation choice.


## Event journal (Host 51)

`window.clarpForm.log(event)` records an explicit finite JSON object without
confirmation, navigation, answer submission or agent dispatch. Its Promise
resolves to `{event_id, queued: true, synced: false}` only after durable native
queuing; this is not Host receipt proof. The app syncs bounded batches to the
original authenticated Host with stable event IDs. A temporary failure retains
records and retries; permanent/authentication failures retain them for original
connection recovery. No credentials or destination are supplied by the HTML.

Full-device authenticated POST `/artifacts/{id}/events` accepts `{version,
events: [{event_id, client_seq, client_at, event}]}` (1-32 rows, 16 KiB per event).
Same event ID/version/content returns its original receipt and first metadata;
a conflicting event rejects the entire batch. Host timestamps are independent
of client times. Read-only reports and deleted artifacts reject logging.
Archiving is an inbox preference and does not close the journal. The journal
has no UPDATE/DELETE API and never wakes an agent or mutates answers or the inbox.

GET `/artifacts/{id}/events?after=SEQ&limit=100` returns events, next_seq,
snapshot_seq and has_more. Pass `through=snapshot_seq` on subsequent pages for a
consistent export. `format=jsonl` returns the bounded page as NDJSON.
`clarp-agent-artifacts form-events ID --output FILE.jsonl` exports all pages of
one snapshot atomically with private permissions; an existing output requires
`--overwrite`. Event text is user data, not instructions or execution approval.

An existing immutable game that already stores a custom draft event array can
opt in explicitly with `clarp-agent-artifacts form-events ID --draft-key events`.
Only that artifact/key is followed; other drafts stay local. Empty `--draft-key`
disables capture. The app loads/caches the original Host policy, imports deltas
from the selected array using stable content IDs and a durable import cursor,
and does not alter the HTML, answers or form version. New authored pages should
use explicit log calls instead of enabling both paths for the same events.
Draft-array capture is bounded at 10,000 entries; explicit events can continue
without a cumulative array. Local unsynced storage is bounded at 8 MiB and
reports capacity errors without dropping prior records; acknowledged records
are compacted only after a verified Host receipt. These are transport bounds,
not gameplay rules. Telemetry is visible through authenticated read/export,
not pushed into an inference turn.
