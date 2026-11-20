---
name: clarp-native-forms
description: Ask the user a few structured questions they answer in the app itself, as a native form (a render_spec card). Use when you need a choice, a short text or a yes/no from the user to go on, and an HTML form would be more than the question needs.
---
# Native forms (render_spec)

A native form is a card the app draws itself: a few fields and one button. The
user answers in the chat, and the answer comes back to you as a message, like
any other message to you. Use it for a decision you cannot make yourself: pick
one of a few options, give a short text, say yes or no. Not for plans or long
proposals (use `clarp-html-forms`), not for an approval of a protected action
(use a decision, `clarp-artifacts`).

The form is written in the catalog `clarp.form`, version 1. Its components:
`stack`, `card`, `heading`, `text`, `divider`, `text_field`, `toggle`, `choice`.
The full catalog, with every prop and limit, is at
`GET /artifact-catalogs/clarp.form/1` on this Host. An example request is in the
Clarp repository at `tests/fixtures/render_spec/build-decision.json`.

Publish the whole request from a file, and check it first:

```bash
clarp-agent-artifacts publish-render-spec request.json --validate-only
clarp-agent-artifacts publish-render-spec request.json
```

A refusal names a code and the path in your request that caused it. Fix that
and publish again; nothing was created.

Not every app can draw the form. One that cannot shows a plain card with the
title and cannot answer it. So say in your reply what you are asking, briefly,
and do not wait for the form alone when the question is urgent. A form is
answered once; its answer arrives as a message from the user that names the
form and gives each value.
