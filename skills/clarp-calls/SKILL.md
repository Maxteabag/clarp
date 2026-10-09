---
name: clarp-calls
description: Manage the user's Clarp group call. Add, remove, hold, resume or switch the agents in it, or call someone else, including bringing in another agent yourself. Use when the user is on a call or hands-free with you and asks for any of these, e.g. "can we add Mike to this conversation", "put Solu on hold", "switch me to Nadia", "call Omar instead", "drop Theo", "who's on the call", or when your turn carries a [Group call] header and another agent should join.
---

# Managing a group call

A group call is the user's hands-free conversation with several agents at
once, in the call screen of the Clarp app. The Host keeps the call: who is in
it, who is on hold, and who has the floor (gets what the user says when they
name nobody). You change it with `clarp-admin call`. You decide what the user
means; nothing on the Host guesses from their words. Asking an agent to *do*
something ("ask Mike to check the deploy") is ordinary work, not a call
change.

```bash
clarp-admin call status                 # the live call: participants, states, floor
clarp-admin call add mike               # "can we add Mike to this conversation"
clarp-admin call hold solu              # "put Solu on hold" (no speech reaches them)
clarp-admin call resume solu            # "bring Solu back"
clarp-admin call switch nadia           # "switch me to Nadia" (gives Nadia the floor)
clarp-admin call transfer omar          # "call Omar instead" (holds whoever has the floor)
clarp-admin call remove theo            # "drop Theo from the call"
clarp-admin call end                    # "end the call" / "hang up"
clarp-admin call start theo mike        # start a call (the first agent gets the floor)
```

Pass the name the user said, spoken spelling and all: the Host matches session
ids, persona names and close approximations ("Mikey", "Nadja"), and prefers
agents already in the call. Run `clarp-admin call status` first when you are
unsure whether a call is live or who is in it.

## Bringing someone in yourself

You may add another agent on your own initiative, or hand the user over
("let me bring in Nadia, she owns the migration"):

```bash
clarp-admin call add nadia        # Nadia joins; you keep talking
clarp-admin call switch nadia     # hand over: the user talks to Nadia next
```

- Always say it aloud before or as you do it, and why, in one short line.
- Add only agents the user asked for. Add anyone else only when it clearly
  helps the conversation, and say so ("Nadia knows the migration, I'm adding
  her"). Never add agents silently, to fill the call, or to hand off work you
  could do yourself.
- Don't hold, remove or transfer other agents unless the user asked.

## The call header

When the user speaks to you in a group call, your instructions start with a
`[Group call]` header: who is on the call, who is on hold, and the call's
turns since you last spoke, quoted as `User -> Theo: …` and `Theo: …`. Use it
the way you would use what you heard on a real call ("what do you think of
Theo's idea?"). Other participants see your reply quoted the same way when
they speak next; they don't hear it now.

## Saying what happened

Every success prints `"ok": true`, the updated `call`, and a `summary` such as
"Mike joined the call." or "Put Theo on hold and called Omar." Confirm it
aloud in one short `<speak>` line, in your own words. After a `switch` or
`transfer` the user is talking to the other agent, so keep it to that line:
"Okay, here's Nadia."

Errors print `"ok": false` and an `error`. Tell the user plainly:

- `unknown agent`: no contact by that name. Say who you think they meant, or
  ask.
- `ambiguous agent` with `candidates`: ask which one, by name ("Mike Ross or
  Mikael?"), then run the command again with the session id.
- `no live call`: there is no group call. Offer to start one
  (`clarp-admin call start …` with yourself and the agent they named), or just
  pass the message on.
- `not in the call`: they asked to hold or drop someone who isn't on it.
- `several live calls`: add `--principal <device>` from `clarp-admin call
  status --principal …`, or ask.

In a call the user's words go to whoever has the floor, unless they open with
a participant's name ("Nadia, what do you think?"). So "Solu, go on hold" may
reach Solu: holding or removing yourself is fine, say a one-line goodbye.

Holding or removing an agent never stops its work. A reply it is already
writing still lands in its chat but is not played on the call.

The wire contract is `docs/group-calls.md` in the Clarp repository.
