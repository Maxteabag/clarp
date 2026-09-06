# Oracle conversation style trials

Manual paid Realtime simulations, 6 September 2026. Goal: attentive, human and
brief, with short openings and acceptance/result updates. Preserve tool-grounded
state and attribution. These trials use real gpt-realtime-2.1 audio with scripted
agent results, not the full container E2E. Inputs are cached; no CI invocation.

First candidate still appended a verbose promise after "Marcus is on it" and
omitted the agent name from a status answer. The revised instructions end the
acceptance update there and keep attribution explicit. Also stop immediate
get_agent_status calls after successful delegation: Clarp already polls results.

Actual outputs:

## human-busy-1

Functional scenario passed: True.

- Okay, I’ll get that queued up with Theo when there’s room.
- Theo’s busy, so it’s queued.
- Okay, Theo checked it. The preview loads, but the title is missing.

## human-delegate-1

Functional scenario passed: True.

- Okay, let’s see about getting Marcus on that.
- Marcus is on it. I’ll wait for his update and then I’ll tell you what he finds.
- Okay, Marcus checked it. The preview loads, but the title is missing.

## human-delegate-2

Functional scenario passed: True.

- Okay, let’s get that started and see what comes back.
- Marcus is on it.
- Okay, Marcus checked it. The preview loads, but the title is missing.

## human-delegate-3

Functional scenario passed: True.

- Okay, I’ll get that started.
- Marcus is on it.
- Okay, Marcus checked it. The preview loads, but the title is missing.

## human-status-1

Functional scenario passed: True.

- Okay, let me check on Theo’s status.
- Still working on it.

## human-status-2

Functional scenario passed: True.

- Okay, let’s check on Theo’s status for you.
- Theo is still working.

These examples are for human judgment, not a claim that one prompt guarantees
wording. Timed unsolicited waiting updates are not implemented by this prompt;
status responses here were explicitly requested. The model must not invent
progress or promise a timer it cannot initiate. Deployment remains separate.
