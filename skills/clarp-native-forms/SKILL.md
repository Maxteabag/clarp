---
name: clarp-native-forms
description: Ask the user a few structured questions they answer in the app itself, as a native form (a render_spec card). Use when you need a choice, a short text or a yes/no from the user to go on, and an HTML form would be more than the question needs.
---
# Native forms (render_spec)

A native form is a card the app draws itself: a few fields and one button. The
user answers in the chat, and the answer comes back to you as a message, like
any other message to you. Use it for a decision you cannot make yourself: pick
one of a few options, give a short text, say yes or no. When you need such a
choice from the user, prefer a native form to asking in prose: the user
answers with a tap, and the answer arrives as a message you can act on. Not for plans or long
proposals (use `clarp-html-forms`), not for an approval of a protected action
(use a decision, `clarp-artifacts`).

The form is written in the catalog `clarp.form`, version 1 (version 2, below,
adds charts, tables and buttons). Version 1's components:
`stack`, `card`, `heading`, `text`, `divider`, `text_field`, `toggle`, `choice`.
The full catalog, with every prop and limit, is at
`GET /artifact-catalogs/clarp.form/1` on this Host.

The file you publish is the whole request: your session, the artifact type,
a title and summary, and the form itself under `payload`. The form alone is not
a request; published bare, the Host refuses it with "unknown agent session".
A complete request that asks one choice:

```json
{
  "session": "YOUR_SESSION",
  "type": "render_spec",
  "title": "Build decision",
  "summary": "Send this build or review it first",
  "payload": {
    "version": "build-1",
    "catalog": "clarp.form",
    "catalog_version": 1,
    "submit": {"label": "Submit", "intent": "submit_decision"},
    "answer_schema": {
      "type": "object",
      "properties": {"choice": {"type": "string", "enum": ["send", "review"]}},
      "required": ["choice"]
    },
    "spec": {
      "root": "root",
      "state": {},
      "elements": {
        "root": {"type": "stack", "props": {}, "children": ["selection"]},
        "selection": {
          "type": "choice",
          "props": {
            "label": "Next step",
            "value": {"$bindState": "/choice"},
            "options": [
              {"value": "send", "label": "Send this build"},
              {"value": "review", "label": "Review first"}
            ]
          },
          "children": []
        }
      }
    }
  }
}
```

A payload with every component (text field, toggle, heading) is in the Clarp
repository at `tests/fixtures/render_spec/build-decision.json`; it goes under
`payload` like the one above.

Check the request first, then publish it:

```bash
clarp-agent-artifacts publish-render-spec request.json --validate-only
clarp-agent-artifacts publish-render-spec request.json
```

A refusal is printed on standard error as the Host gives it: usually a code and
the path in your request that caused it, sometimes a sentence (an unknown session). Fix that
and publish again; nothing was created.

## One-tap answers and undo

On a Host with contract 228 or later (feature `decision_undo`), apps that support it send a
form whose only input is one choice, or a version 2 form of buttons only, on one tap, and let the
user undo it for a few seconds. The answers reach you when that window ends. An undone answer
never reaches you and the form is open again; the user may answer it anew under a new
submission. Forms with two or more inputs keep their Send button. Nothing changes in how you
publish a form.

## Version 2: chart, table and buttons

Version 2 (`"catalog_version": 2`, catalog at `GET /artifact-catalogs/clarp.form/2`)
is version 1 plus three components. Each app is switched on for it separately.

**Drawn by: the web app** (the browser app and the desktop shell that loads it), from
its contract 2, the Android app, from its contract 42, the iOS app, from its contract 87
(TestFlight 3028), and the native desktop app, from its contract 2. An app
that does not draw version 2 shows such a form as a plain card that cannot be answered,
so use `"catalog_version": 1` when the user may answer on one of those.

The version 2 components:

- `chart`: `kind` `bar`, `line` or `pie`, up to 6 `series` of `{name, points: [{x, y}]}`, each name its own;
  optional `title`, `x_label`, `y_label`, `unit`. Every x of one chart is a string or every
  x a whole number; within a series no x repeats and numeric x increases. Bar: at most 60
  points per series; pie: one series of up to 12, no y below 0; 600 points in all.
- `table`: `columns` (1 to 8 of `{key, label, align?}`) and `rows` (up to 200 objects keyed
  by column keys; a cell is a string, a whole number, true/false or null), at most 1000 cells.
- `button`: `label`, `action` (an identifier), optional `style` (`primary`, `secondary`,
  `destructive`) and `confirm` (a question asked before it submits). A tap submits the form.

Numbers are whole numbers, as everywhere in the catalog: no 12.5. For fractions give
integers and `decimals` (0 to 6, on the chart or on a table column): `y: 125` with
`"decimals": 1` is drawn as 12.5. Numeric x is never scaled.

A form with buttons has no `submit`. Its `answer_schema` declares `action` as a required
string whose `enum` is exactly the buttons' actions, and nothing binds `/action`. The
answer names the button: `Action: "Ship it" (ship)` before the values. A short payload:

```json
{
  "version": "deploy-7",
  "catalog": "clarp.form",
  "catalog_version": 2,
  "answer_schema": {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["ship", "hold"]}},
    "required": ["action"]
  },
  "spec": {
    "root": "root",
    "state": {},
    "elements": {
      "root": {"type": "stack", "props": {}, "children": ["lat", "row"]},
      "lat": {"type": "chart", "props": {"kind": "bar", "title": "p95 latency", "unit": "ms",
        "series": [{"name": "p95", "points": [{"x": "before", "y": 120}, {"x": "after", "y": 80}]}]},
        "children": []},
      "row": {"type": "stack", "props": {"axis": "horizontal"}, "children": ["ship", "hold"]},
      "ship": {"type": "button", "props": {"label": "Ship it", "action": "ship", "style": "primary"}, "children": []},
      "hold": {"type": "button", "props": {"label": "Hold", "action": "hold", "confirm": "Hold the release?"},
        "children": []}
    }
  }
}
```

A fuller one (a line chart, a table, a note and two buttons) is at
`tests/fixtures/render_spec/deploy-v2.json` in the Clarp repository.

Not every app can draw the form. One that cannot shows a plain card with the
title and cannot answer it.

Which apps draw forms (version 1):

- iOS app: from client contract 74 (TestFlight 2981); version 2 from 87 (see above).
- Web app (the browser app and the desktop shell that loads it): from its
  first contract (version 2: see above).
- Android app: from client contract 41 (release 0.1.0, build 1058); version 2 from 42 (see above).
- Native desktop app: from client contract 1; version 2 from 2 (see above).

So say in your reply what you are asking, briefly, and do not wait for the form
alone when the question is urgent. A form is
answered once; its answer arrives as a message from the user that names the
form and gives each value.
