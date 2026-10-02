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
- `--urgency normal|time_sensitive`: time-sensitive breaks through the user's
  Focus, and only when combined with `--blocks-progress`. Use it only when you
  are blocked AND waiting has a real cost (a login window, a deadline).
  Everything else is an ordinary notification.
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

## Ask for urgent typed input (codes, short answers)

When you need a value only the user has, right now (an SMS or e-mail login
code, a short reference), ask with a text input card instead of in chat. It
shows a text box, a countdown, and a reply field on the notification itself.

```bash
artifact=$(clarp-agent-artifacts input "$CLAUDE_PWA_SESSION" \
  "Ruter SMS code" "Ruter just sent an SMS code to your phone. Enter it here." \
  --hint one_time_code --expires-in 300 --blocks-progress --urgency time_sensitive \
  --priority-reason "The login is waiting for the code and the code expires in 5 minutes." \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["artifact_id"])')
clarp-agent-artifacts wait "$artifact" --timeout 300
```

- `--hint one_time_code` gives a code keypad with SMS autofill; use `text` otherwise.
- `wait` blocks until the answer arrives and prints it: exit 0 answered,
  3 expired/withdrawn/discarded, 4 timed out. Use it when you would otherwise
  sit idle; the answer is also delivered to you as a normal decision result.
- Treat the typed value as a secret when it is one: use it, never echo it into
  chat, commits or files.

## Always say how long the user has

If the answer is only useful for a limited time (a login window, a code's
lifetime, a meeting starting), set `--expires-in SECONDS` (or `--expires-at`)
on any card. The card shows a countdown, and when time runs out it is marked
**Expired**: it stays in the conversation, it is never deleted, and you are
told it expired. Do not create short-lived cards without an expiry.

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
- Discard is silent: you are not told. Expiry grants neither approval nor an
  answer and is delivered to you. Neither is permission. Stop the dependent
  action; do not guess permission or immediately recreate the unchanged request.
- Archive only hides the card from the inbox; it does not answer or cancel work.
- If creation times out, inspect `attention --session SESSION` before retrying
  to avoid duplicate questions. Do not create duplicates merely to test a helper.
- A saved answer can still have `delivery_pending: true`. It is durable but does
  not prove that the originating agent has resumed; the Host retries delivery.
- Question answers and expiry notices queue behind busy work.
  Successful delivery can mean durable admission to that queue, not execution;
  do not stop another active turn merely to process the notification.

## Keep your requests current

Updates is only useful if every card in it still matters. You own your cards:

- When a request is no longer valid (you found the answer, the plan changed,
  the time window passed, a newer card replaces it), withdraw it at once:
  `clarp-agent-artifacts withdraw "$CLAUDE_PWA_SESSION" DECISION_ID`.
  Withdrawal is silent and closes the card everywhere.
- Never post a second card for the same question; withdraw the old one first.
- When the user writes to you in chat, Clarp closes all your open requests as
  `superseded`. Treat their message as the reply. If it did not answer
  something you still need, ask again, as a new card if necessary.
- Short-lived prompts ("approve within 3 minutes", "pick 67 in the app") get
  an `--expires-at` matching the window, so they disappear on their own.
- Do not file CI results as attention. If a failing workflow blocks your work,
  ask a question that says what is blocked and what you propose.
