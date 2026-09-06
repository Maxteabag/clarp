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
The recipient is resolved from the stored artifact, never client input. The
receipt and pending delivery snapshot are committed in the same transaction.
An identical ID/payload retry returns the existing receipt. Reusing an ID for
different answers is rejected. The normal dispatch worker uses a stable
`html-form-{submission_id}` request ID with durable queueing and no steering.
Receiving a receipt proves acceptance, not completed agent work. Failed dispatch
stays pending for retry. Submissions are preferences, not protected-action approval.

HTML bytes are preserved rather than passed through conversational markup
stripping. The native renderer confines scripts and assets; never serve this
HTML directly on a credential-bearing Host origin. Existing artifact types,
quick decisions and task plans retain their contracts.

See the managed `clarp-html-forms` skill for authoring, draft restoration,
custom controls and publishing. Native implementation and testing live in the
separate iOS repository.
