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
blindly mint another ID. Payloads are immutable. Changed questions or HTML need
a new artifact ID/version, leaving existing drafts and receipts attached to the
original. Existing quick-decision and task APIs remain unchanged.

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
