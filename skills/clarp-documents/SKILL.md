---
name: clarp-documents
description: Generate, inspect and publish searchable PDF planning documents, proposals and finished writing with useful diagrams and UX concepts.
---
# Finished documents

**Substantial finished planning documents are PDFs by default.** Keep the editable
Markdown/HTML source. Publish the PDF as a durable `file` artifact so the phone
can read, share and save it. A proposal can still be a proposal: PDF is the
finished deliverable format, not a claim that its design has shipped.

Keep `clarp-agent-tasks` checklists, runtime/job state, decisions and ordinary
conversation replies in their native form. Do not turn routine replies into
files or replace durable task bookkeeping with a PDF.

## Generate and inspect

Use the helper next to this skill (requires `uv`, Python 3.11+, and system Pango):

```bash
uv run SKILL_DIR/scripts/pdf_document.py render /absolute/plan.md \
  --output /absolute/plan.pdf --title "Product plan"
```

It accepts Markdown or HTML and uses local WeasyPrint; no external PDF service.
`uv` installs the declared Python dependencies. Place linked images/SVGs below
the input directory. Remote asset fetching and reads outside that directory are
blocked; ordinary external hyperlinks remain links. Use `--preface visuals.html`
for visual plates before the complete source and `--css layout.css` for local
styling. Relative asset paths are resolved against the source directory.
Existing outputs require deliberate `--force`.

For software/UX plans, include meaningful database/architecture relationships
and visual UX examples where they clarify decisions. Vector SVG or HTML/CSS
works well; a simple wireframe is enough when polish adds little. Label proposed
UI concepts explicitly; only label an image a verified app screenshot after
observing the actual app. Avoid filler diagrams and wall-of-text layouts.
Preserve requirements when adding visual summaries.

The render writes `plan.pages/page-NNN.png`, `text.txt`, and `inspection.json`.
Inspect **every rendered page** with an image viewer. Check readable type, page
breaks, orphan headings, table continuation, working links, and diagrams inside
margins. The report checks searchable text and page bounds; it does not replace
visual inspection or verify that the design is implemented. Check the extracted
text against the complete source. Do not publish an image-only PDF.

To inspect an existing PDF:

```bash
uv run SKILL_DIR/scripts/pdf_document.py inspect /absolute/plan.pdf \
  --output /absolute/plan-review
```

## Publish after visual review

```bash
uv run SKILL_DIR/scripts/pdf_document.py publish /absolute/plan.pdf \
  --session ACTUAL_SESSION --title "Product plan" \
  --summary "Proposal with requirements, relationships and phone concepts" \
  --receipt /absolute/plan-publication.json
```

This reuses `clarp-media-publish` for upload and `clarp-agent-artifacts` for the
file artifact. The receipt retains the upload URL and artifact/file ID. Return
the PDF link and artifact ID when another agent needs to share it.

A receipt is created before upload and must not already exist. On a timeout or
failure, **inspect the receipt and list the session's artifacts before retrying**.
An `uploaded` receipt permits creating the missing artifact from that existing
asset; do not reupload it. A `started` receipt is ambiguous: reconcile the media
store before another upload. Never blindly repeat an external mutation.

For short editable writing or a specifically requested Markdown artifact, the
existing `document` payload `{"content":"markdown"}` remains supported:
`clarp-agent-artifacts create SESSION document TITLE SUMMARY JSON_PAYLOAD`.
