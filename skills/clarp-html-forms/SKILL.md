---
name: clarp-html-forms
description: Create an interactive HTML planning proposal with editable choices, numbers, priorities and recoverable local drafts. Use when the user wants a plan they can fill in or an agent-generated planning interface.
---
# Interactive planning forms

Always prefer HTML forms for plans, including finished proposals and settled
plans. Give the agent creative freedom to present the case and ask questions.
Do not impose a preferred maximum question count, fixed layout, design style,
mandatory control palette or complexity preference. The agent may use sliders,
charts, screenshots, design examples, illustrations, custom interactions or any
other presentation that suits the plan. These are possibilities, not requirements.
Preserve existing
quick Clarp decisions and native task bookkeeping. PDF is only for an explicit
user request, never an automatic final planning deliverable.

## Publish a native form

Publish self-contained HTML plus an answer JSON Schema through the installed
helper. The schema describes answers, not layout or allowed UI widgets. Inline
scripts/styles and data-URL images, fonts, media and illustrations run offline.
External network requests and navigation are unavailable in the native wrapper;
embed required libraries and assets instead. Do not place Host secrets in HTML.

```bash
clarp-agent-artifacts create-form ACTUAL_SESSION "Plan title" plan.html answers.schema.json \
  --version 1 --artifact-id form-UNIQUE_STABLE_ID --summary "What this plan explores"
```

Use `--dry-run` to inspect the exact request without publishing. Choose the ID
once, then reconcile it with the artifact API after an ambiguous POST; never
blindly mint another ID. Interactive-form payloads are immutable. Changed questions
or HTML need a new artifact ID, leaving existing drafts and receipts attached to the
original. Existing quick-decision and task APIs remain unchanged.

## Read-only reports

A report, findings, audit or write-up that asks nothing is a read-only form: the
same self-contained HTML, no schema, no answers.

```bash
clarp-agent-artifacts create-report ACTUAL_SESSION "Report title" report.html \
  --summary "What it found"
```

The default artifact ID comes from the title and HTML, so a retry of the same
report reconciles to the same artifact. On Host contract 49 (`html_report_revisions`),
reuse one explicit `--artifact-id` and choose a new `--version` for each changed
publication. The report keeps its link, owner, creation time, pin and archive state.
An identical retry returns the current report; retrying an older published version
cannot roll the link back. Reusing a version for different HTML is rejected.

```bash
clarp-agent-artifacts create-report ACTUAL_SESSION "Report title" report.html \
  --artifact-id report-STABLE_ID --version 2 --summary "Updated findings"
clarp-agent-artifacts report-history report-STABLE_ID
clarp-agent-artifacts report-history report-STABLE_ID --version 1
```

History lists contain bounded metadata; an explicit version returns that revision
including its HTML. Restore old content by publishing it under a new version label.
The helper compares the current version before replacing a report; concurrent
changes stop for review instead of silently overwriting another publication.
The artifact update API also accepts report `payload_patch` changes and assigns
a fresh cache version when no version is supplied. Pass `expected_version` to
reject a patch based on an outdated revision. Interactive forms remain immutable,
including their drafts and submission receipts. Older Hosts require a new artifact ID.
The Host refuses answers to a report. Keep it readable on a
phone in light and dark mode (`prefers-color-scheme`). Plans with questions stay
forms.

## Connect the HTML to Clarp

Ordinary named inputs, selects and textareas are collected and restored by the
native wrapper. Use `name` as a stable answer key. Custom controls can supply any
JSON object through `window.clarpForm.setAnswers(answers)`. Restore custom state
from `window.clarpForm.getDraft()` or the `clarpformready` event's `detail`.
Call `window.clarpForm.submit(answers)` to submit custom state, or use a normal
HTML form submit button. Page-requested submission opens a native confirmation;
Clarp also provides its own Send answers button for direct user submission.
Opening a form or running its scripts cannot send answers without a user action.

The wrapper saves drafts on input/change in native storage scoped to Host,
artifact and version, and caches the complete HTML for offline reopening. This
is the native equivalent of localStorage; author code must not assume the
embedded page has persistent browser storage. Browser-only prototypes still
need their own localStorage/service-worker support.

Back returns without submitting. Successful submission returns after a durable
Host receipt; failed submission retains the answers and offers retry. A stable
submission ID and original payload survive ambiguous retries. Receipt means
accepted for durable delivery, not that the agent finished acting. Preferences
are not authorization for deployments, purchases or other protected actions.

## Verify before publishing

Inspect the rendered form at phone and desktop widths; exercise custom controls,
numeric values, saved drafts, reopening offline, Back, and submission failure.
Verify exported/submitted values reflect the actual controls. For changes to the
bridge, use isolated Host tests and native simulated UI tests; never send an
unrequested live message merely to test it. Do not claim native behavior from a
browser mockup. Keep evidence of the exact form version.
