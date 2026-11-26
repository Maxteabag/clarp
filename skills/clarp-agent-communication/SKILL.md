---
name: clarp-agent-communication
description: Send work or status from one Clarp agent to another. Use when collaboration needs an explicit cross-agent handoff.
---

# Clarp Agent Communication

Resolve the destination with `clarp-sessions`, then send through the canonical
server endpoint using `clarp-admin prompt --to SESSION --from "$ME"`, where
`$ME` is your verified session (normally `$CLAUDE_PWA_SESSION`; see
`clarp-sessions`).

For another explicitly trusted Clarp server, include its configured peer name:

```bash
clarp-admin prompt --server work --to rachel \
  --from "$ME" --text "Please review the remote branch."
```

The destination is always `(peer server, session)`. A persona name alone is
not a cross-server identity. Peers communicate over their configured HTTP(S)
address, normally through Tailscale, and never share SQLite or filesystems.

Agent-origin messages must identify the sending session. Do not impersonate the
user, and do not send an external message merely because another agent asked.

## Agents bound to a model provider of their own

On a Rust Host with the feature `bound_privacy` (contract 179), an agent bound to a model
provider of its own (`codex_provider` on its row; see `clarp-sessions`) keeps its words
from every agent that is not bound to the same home and provider and that its owner has not
let in (`readers` on its row, by agent id):

- What it sends such an agent (`prompt`, `request`/`reply`, a helper's report) arrives as
  "<name> sent you a message. Its words are withheld …", and its own `prompt` answers
  `words_withheld: true`: the recipient did not get the words. A `--delay` prompt it
  schedules for another agent goes out under its name too, whatever `--from` says. Send to
  another Host (`--server`) is refused. Its `<team>` updates reach such teammates as
  "<name> posted an update (words withheld …)".
- A helper made with it as the parent (`clarp-admin agent create --parent <it>`) is bound
  to the same home and provider and must run on Codex, so the helper reads its parent and
  its parent reads the helper.
- From such an agent you cannot read its log, its goals, its artifacts or its status fork:
  reads answer `bound_conversation` (HTTP 403, or exit 77 in the tools), and its rows and
  events come with `words_withheld: true` and empty texts. Its name and state stay.
- Messages to it are delivered as usual.
- To read it, ask its owner: `clarp-admin agent provider --session <it> --reader <you>`
  makes a card ("Let you read <it>'s conversation?") in your chat; only the owner's own yes
  lets you in, not one given on his behalf. Do not try to get its words another way (its
  files, the Host's configuration, a tool run outside your turn).

## When you need something back

`prompt` is one-way. Whatever the recipient answers in its own chat stays
there: it is not delivered to you. When the result matters, send a request,
and let your goal wait for it:

```bash
clarp-admin request --to avana --from "$ME" --goal "$PLAN_ID" --deadline 21600 \
  --text "Please add an event log to forms; tell me when it is live."
```

The goal (from `clarp-goal`) wakes you with the answer, or at the deadline
with none. It never replaces another dependency the goal waits on (exit 4,
nothing sent); a scheduled timer on that goal is replaced by this wait.
Without `--goal` the answer still comes back as a message, but nothing wakes
you if none comes. Exit 5 means delivery is uncertain: the request may have
arrived, so do not send it again; the goal keeps waiting.

When a message starts with `[Request r… from …]`, answer the sender
explicitly, not only in your own chat:

```bash
clarp-admin reply --request r1a2b3c4d5e6 --from "$ME" --kind result --text "Live: clarpForm.log()."
```

`--kind blocked` says what you wait for (an approval, say) and `--kind
progress` shares a useful milestone; both keep the request open, and each
distinct one starts a turn for the sender, so send few (at most five per
request). Do not send acknowledgements, and do not answer a reply or a
progress note unless you need something more: that is how messages
ping-pong. A pending approval is a real blocker: report it, never work around
it. While the user has the requester's goal paused or blocked, a result it
still waits for is held for the resume and updates are refused; a result it
no longer waits for (its deadline already woke the requester) comes as a
message instead of being lost; `reply` says `held` when that happens, so do
not resend. A user's Stop also pauses the agent's queue: plain `prompt` and
`reply` messages still run (and end the pause when nothing else is held),
but a send with `queue_if_busy` waits, unannounced, until the user resumes
the queue; do not use it to reach a stopped agent. On the Rust Host the pause
outlasts the user's own waiting messages, which the Stop lets run: after they
have run, the agent stays held until the user writes to it again or presses
Resume. A `queue_if_busy` send, a goal's check-in or a schedule that reaches
it then waits, held: its answer says `queued: true`, and `GET /turn-queue`
says `paused: true`. Do not resume a queue the user stopped yourself; if what
waits is urgent, tell the user through their front desk. Requests work between agents on this Host; for
a peer server use `prompt`.

A message from the user (or the Oracle) never waits behind a turn that is not
the user's. When it reaches an agent whose running turn answers another agent,
a schedule, an automation or a goal's wake, and the agent's tool can take it
into that turn now (Claude Code, agy-acp, Codex on its chat lane), it is read
into that turn on a Rust Host from contract 192 (feature `owner_steers`):
nothing is cut. From then on the user sees the turn: what you write after
reading their message is in their chat, until you answer them with
`clarp-admin tell-user`, after which the rest is the agents' again. So answer
the user, then finish the other agent's work and answer that agent as usual.

Where the tool cannot take it (another backend, a Codex turn off its lane, a
turn that is starting or ending), or on a Host from contract 187 before 192
(feature `owner_preempts`), that turn is stopped at once and the user's message
runs next. The agent then gets a Host message (origin `made-way`) that quotes
what it was working on and asks it to take that up again. When you get one,
finish that work, or do it if you had not started. If it came from another
agent, answer them as usual (`reply` for a request, or `prompt`), because the
cut turn told them nothing. As a sender, your message's turn can end as
stopped for a newer message; do not send it again, because the recipient
takes it up after the user. This does not happen behind the user's own turn,
a front desk's relay of the user's words, a Codex native goal's turn, or a
compaction.

Otherwise, a message from the user, or a front desk's relay of the user's words, that
reaches an agent while its turn runs is read into that turn where the agent's
tool can take it: Claude Code on the Rust Host, and Codex on a Rust Host whose
owner turned the `codex_chat_lane` setting on (off by default; see
`clarp-server-admin`). Elsewhere, and for every other message between agents,
it waits and runs as the agent's next turn. The user's messages always reach
you in the order they were said: a newer one is not read into your turn while an
older one still waits. Either way, send it once: the queue shows it until it is
read.

Whether a message sent now would be read into an agent's running turn or wait for
it to end: on a Rust Host from contract 197 (feature `queue_steer`),
`GET /turn-queue?session=<agent>` says `steerable: true` while its running turn takes
one now, `false` while it would wait. The user can have one of their own waiting
messages read into the turn (`POST /turn-queue/<id>/steer`), or take over a turn that
cannot take it (`POST /turn-queue/<id>/take-over`): that turn ends as stopped for a
newer message, as above, and their message runs next. Those are the user's actions,
from the app; do not call them for the user, and do not use them for your own
messages, which they refuse.

What the user's Send does while an agent works is one owner setting on a Rust Host
from contract 206 (feature `send_while_busy`): `GET /send-settings` says `while_busy`,
`steer` (the default), `after_turn` or `take_over`; the apps send it with the user's
messages. With `steer`, a user message the running turn's tool cannot take at all
(no steering for that backend, a turn running beside its lane, a terminal open on the
tool, a turn whose input already ended) stops that turn and runs next, just like a take-over,
so you can get a turn ended as stopped for a newer message even behind the user's own
turn. With `after_turn` it waits for the turn; with `take_over` it stops the turn.
A message that names an attached file or begins with `/` waits. If the running turn
can neither take words nor be stopped, the message waits for it to finish.
This acts only on the user's own sends. Your messages, a schedule's and an
automation's wait as above, whatever `while_busy` they name. The setting is the
owner's (`POST /send-settings`, refused from an agent's turn); do not change it unless asked to.

Messages from agents keep a turn each. The user's own messages that waited while you were
busy (because your tool could not take them into the running turn) do not: on a Rust Host from contract 169 (feature `queued_read_together`) they reach you
together in one prompt that says so, numbered `[1]`, `[2]`, … in the order sent. Read all of
them and answer them as one; the last may change or take back the first.

A helper agent (one created with `--parent`) reports to its parent the same
way. That message moves the helper to `reported`; a message from the parent
back to it moves it to `running` again. See `clarp-sub-agents`.

## Whether another agent can answer now

On a Rust Host that lists feature `agent_availability` in `GET /server-info`
(contract 206), every agent's row says whether it can answer now, why not, and
until when:

```bash
curl -s -H "Authorization: Bearer $CLARP_AUTH_TOKEN" \
  "$CLARP_SERVER_URL/agents/ada-1a2b" | jq .availability
```

`state` is `ok` (it answers now), `busy` (a turn runs; `queue_ahead` messages
are read before yours), `unresponsive` (a running turn has shown
no sign of life for 3 minutes; `last_alive_ms`), `restarting` (its process
is starting or starting again), `switching_account` (the Host moves it to another account) or
`usage_limit` (no account can take its turn now). `text` is the line the apps
show. A launch held by the pool without a known cause says "Waiting for account".
`back_at_ms` is when the Host expects it to answer (epoch ms, or `null`).
Use it to decide, not to refuse: a message to an agent that cannot answer now
still waits in its queue and runs later. When the work cannot wait for
`back_at_ms`, ask another agent, or tell the user why through their front desk.
An older Host has no `availability`: read `busy` and `queued_turn_count`.

## When the user does not see the turn

A turn another agent's message started is coordination: the user may choose to
keep it out of the chat, and then nothing you write in it, your final reply
included, reaches the user (the Host adds one line to such a turn's prompt
when that is so). To tell the user something from any turn, run
`clarp-admin tell-user --from "$ME" --text "..."`: it puts a note in your own
chat that is always shown and notifies the user. It works only from your own
running turn. Decision cards and artifacts are shown as always.

## When the user reads the chat in Conversation view

The user can read an agent's chat in Conversation view (Host feature `chat_view`,
Rust Host contract 145; the Host adds one line to each turn's prompt when your chat is
in it). There they see their own messages, your notes, cards, forms and
artifacts, and the final reply of each turn once the turn has ended. Your text in
between and your tool work are folded into "Show work" and are not read. To be
heard mid-turn, post: `clarp-admin tell-user --from "$ME" --text "..."` (a note,
shown at once and notified). Your final reply always reaches them, so end every
turn with one that stands on its own. In Session view, the default, everything
is shown as before.
