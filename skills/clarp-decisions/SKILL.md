---
name: clarp-decisions
description: Ask a durable native multiple-choice question with a custom answer, request explicit yes or no approval, or inspect pending attention before asking the user.
---
# Native questions and approvals

Use this skill when an important unknown would materially change the work or
when the user must authorize an action. Make routine implementation choices
yourself. A question is useful when the cost of a wrong assumption exceeds the
cost of asking; it is not a reason to stop independent work.

Prerequisites: the installed `clarp-agent-artifacts` helper and the originating
session in `CLAUDE_PWA_SESSION`. Resolve the session through `clarp-sessions` if
it is missing. Native questions need a supporting Host and client. The question
helper checks Host support before creating anything; if unavailable, ask in
ordinary text. CLI popup tools such as `AskUserQuestion` and `request_user_input`
remain unavailable in the phone app. This documented native artifact workflow
is the supported way to offer choices.

## Inspect the user's existing attention

```bash
clarp-agent-artifacts attention
# Optional: --session SESSION or --include-archived
```

Look at the pending requests before adding one so you understand the competing
work. Describe the facts about your own request; never assign an arbitrary list
position, alter another agent's request, or inflate urgency to jump the queue.

Optional creation flags:

- `--blocks-progress`: this work cannot continue without the answer.
- `--priority-reason TEXT`: what is blocked, why timing matters, and the effect
  of waiting. Required for blocking or time-sensitive requests.
- `--urgency normal|time_sensitive`: use time-sensitive only for a real reason.
- `--deadline-at MILLISECONDS`: an actual deadline as epoch milliseconds.
- `--effort quick|short|review`: quick answer, about a minute, or closer review.
  Effort is separate from importance; a short question need not be urgent.
- `--context TEXT`, `--reference ID`, and `--expires-at MILLISECONDS` provide
  supporting context, a related identifier, and an optional expiry.
- `--dry-run` validates and prints the request without any network action.

Clarp computes ordering from blocking impact, time sensitivity, deadline, and
creation time. Default requests are nonblocking, normal urgency, review effort.

## Ask a material clarification

```bash
clarp-agent-artifacts question "$CLAUDE_PWA_SESSION" \
  "Choose the navigation" "Which navigation should I build?" \
  '[{"id":"keep","label":"Keep the current navigation","description":"Smallest change"},{"id":"simplify","label":"Simplify the navigation"},{"id":"compare","label":"Show both first"}]' \
  --recommend keep --blocks-progress --effort short \
  --priority-reason "The navigation choice is needed before these screens can be implemented."
```

Provide two or three distinct options with stable, unique IDs and concise,
readable labels. Descriptions are optional. A recommendation is optional and
must identify an existing option. The native card always allows **Write my own
answer**. `--payload JSON_OBJECT` can attach relevant identifiers and context.
Use `--dry-run` to inspect a request before posting if needed.

After creation, verify the returned artifact has `type: question`, a decision
ID, `response_type: single_choice`, and the intended options. The same question
appears in Updates and the originating conversation. Await its result while
continuing work that does not depend on the answer. A selected option comes back
with its immutable label; custom text preserves the user's wording. Neither is
blanket authorization for unrelated or protected actions.

## Request explicit authorization

```bash
clarp-agent-artifacts decision "$CLAUDE_PWA_SESSION" \
  "Send the email" "Send the reviewed email to the named recipient?" \
  Yes No '{"recipient":"approved-recipient","draft_id":"reviewed-draft"}' \
  --blocks-progress --priority-reason "Sending requires explicit approval." --effort quick
```

The existing invocation remains supported. Approval buttons are always the
literal labels **Yes** and **No**; put the exact action, scope, and consequences
in the question/context/payload. Do not perform the protected action until the
accepted result returns, then revalidate that the approved scope still applies.
Do not substitute a multiple-choice preference for required explicit approval.

## Results and recovery

- Never call resolve/dismiss endpoints or edit the decision database to answer
  on the user's behalf. Creation does not authorize the requested action.
- An accepted approval, rejected approval, selected option, and custom text are
  different outcomes. Respect the actual response and its scope.
- Discard or expiry grants neither approval nor an answer. Stop the dependent
  action; do not guess permission or immediately recreate the unchanged request.
- Archive only hides the card from the inbox; it does not answer or cancel work.
- If creation times out, inspect `attention --session SESSION` before retrying
  to avoid duplicate questions. Do not create duplicates merely to test a helper.
- A saved answer can still have `delivery_pending: true`. It is durable but does
  not prove that the originating agent has resumed; the Host retries delivery.
- Question answers, discard notices, and expiry notices queue behind busy work.
  Successful delivery can mean durable admission to that queue, not execution;
  do not stop another active turn merely to process the notification.
