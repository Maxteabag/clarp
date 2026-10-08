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

The Linux desktop offers the same `log(event)` and `capabilities.eventLog`
from its loopback form page when the Host advertises `html_form_events`. The
page never sees the token or the Host: the app appends each event to
`$XDG_DATA_HOME/clarp/form-events/<form>/events-outbox.jsonl` before resolving,
sends it with its own credential under the same wire fields and limits, and
sends what a previous run left queued as soon as it reconnects, without the
form being reopened. A followed draft key is honoured the same way.

## Version-bound network connections (Host contract 56)

Forms and read-only reports default to blocking external connections. Publish
an explicit allowlist with repeated `--connect` options:

```sh
clarp-agent-artifacts create-form SESSION "Game" game.html answers.json \
  --artifact-id form-STABLE_ID --version 1 --connect https://storage.googleapis.com
```

`create-report` accepts the same flag. The list is stored as `payload.connect_origins`
with the immutable form version or report revision, and copied into flat responses.
Up to eight exact HTTPS origins are accepted; paths, user information, wildcards,
query strings, fragments and HTTP are rejected. Hosts lowercase DNS names,
remove port 443 and deduplicate. Changed interactive policies require a new artifact;
report revisions preserve old policies in their revision history.

Updated wrappers expose `window.clarpForm.capabilities.network` (an array, empty
for default-deny). Older wrappers may omit it: keep a baked-in/offline fallback.
Only connection APIs such as fetch/XHR are permitted; scripts, images, fonts,
frames and navigation do not gain access. CORS is still required. Native wrappers
supply no Clarp credentials or authenticated proxy. A globe/info disclosure lists
the effective origins without changing the game's viewport.

Authenticated `GET /artifacts/<id>/html` renders the current HTML version with
an opaque sandbox origin and the same connection policy. It supplies network
capability only, not an answer/event bridge. The desktop's loopback bridge keeps
its private local connection allowance for interactive drafts/events; that origin
is not part of the external capability list. Read-only reports have no bridge
allowance. Invalid cached metadata denies every external connection.
