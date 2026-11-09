# Set up a front-desk agent for a group of workers

One conversation agent that never does the work: the user asks it for status,
decisions and steering, and the workers keep working without interruption.

## 1. Create the agent

Use `clarp-agent-admin` (`POST /personas`, then `POST /agents`). Check first that the
name is free: `POST /personas` returns 409 for a taken name. A Claude backend is
needed to run `clarp-status-fork`. Personality, at most 4000 characters, for example:

> The user's front desk for their working agents. You never do the work yourself; you
> keep the user oriented and in control. Answer fast and short. For status, always
> fork the worker with `clarp-status-fork <agent...>` and cite the time of each status;
> never guess and never ask the busy worker in its chat. For decisions, check
> `clarp-agent-artifacts attention` and present what is waiting on the user, most
> blocking first, with a recommendation. To steer, pass the user's instruction to the
> right worker with `clarp-admin prompt` (--from your own session) and confirm in one
> line. Hide coordination, tool calls and internals unless the user asks why.

## 2. Write a charter and send it

Keep the charter outside any repository and include:

- the workers (session slugs and one line on what each owns);
- status: `clarp-status-fork`, never from memory, and report fork failures;
- questions about a worker's work: `clarp-status-fork <worker> --question '<question>'`;
  work that needs tools goes to the worker with `clarp-admin prompt`;
- decisions: `clarp-agent-artifacts attention [--session S]`; relay answers given in
  chat to the worker;
- no e-mail and no contact with anyone outside Clarp;
- the memory rule: fork one worker at a time when available memory is low;
- a daily log: requests served, fork seconds, failures, and anything the user still
  had to ask a worker directly.

Send it with `clarp-admin prompt --to <front> --from <you> --text "Read and follow ..."`
and ask for a first `clarp-status-fork` run as proof. Check the front's log for a real
`clarp-status-fork` tool call rather than an answer from memory.

## 3. Tell the workers

Tell each worker: questions for the user go only as native cards (`clarp-decisions`),
never as chat text; keep the goal and plan current, because the fork reads them;
treat the front's messages as the user's.

## Why relaying is safe

Chat messages never close a worker's pending cards (Host contract 79 removed
the old supersede-on-chat rule). Prompts relayed by the front therefore steer
without wiping the worker's questions, and so does the user typing in the
worker's chat; a card closes only when answered, dismissed, withdrawn or
expired.

## Measure

Before starting, record a one-week baseline: status pings in worker chats, the share
of cards answered rather than left to expire or withdrawn, time to a status, and the
user's messages per day. Compare after a week.
