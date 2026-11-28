---
name: clarp-notifications
description: Read or change what the user is notified of in Clarp, event by event and per agent (replies, questions, failed turns, updates, finished jobs, helper reports, calls, account notices), mute or unmute an agent, or mark a chat read on every device. Use when the user says things like "stop notifying me about failed turns", "tell me when Theo's jobs finish", "don't ping me for Vera's replies", "turn off calls from Mike", "what am I notified of", or "mark that chat read".
---

# Notifications per event

The Host keeps the user's notification choices, so all their devices agree. Each
event has one switch for every agent, and each agent can override any event. An
agent's override wins, then the global switch, then the default. A muted agent
pushes nothing at all, whatever the switches say.

Off means no push to the phones and no popup on the desktop. The chat's badge and
unread mark stay, because they belong to the conversation.

This needs a Host whose `GET /server-info` lists the feature `notification_events`
(Rust Host, contract 223). On an older Host the routes below answer 404: use
`POST /agent-mute` only.

## The events

- `reply` (on): a turn ended with a reply or speech for the user.
- `needs_you` (on, with sound): an agent asked something, or replied while a
  question of its own still waits.
- `failed` (on): a turn failed and the Host stopped trying. Not for a Stop, and
  not for helpers or quiet agents.
- `update` (on): an agent's note to the user (`clarp-admin tell-user`).
- `job_done` (off): an agent's background job finished or failed.
- `helper_report` (off): a helper reported to the agent that made it.
- `call` (on): an agent rings. The global `call` switch is the ring switch
  (`POST /calls/ring/settings` `enabled`); changing either changes both.
- `account` (on): account and quota notices.

## Read the choices

```bash
curl -s -H "Authorization: Bearer $CLARP_AUTH_TOKEN" "$CLARP_SERVER_URL/notification-settings"
```

The answer is
`{"events": [{"id", "message_key", "default", "on"}], "agents": {"<agent_id>": {"session", "muted", "events": {"<id>": true|false}}}, "version"}`.
`on` is the global choice in effect. `agents` lists only agents that are muted or
have an override.

## Change them

For every agent:

```bash
curl -s -X POST -H "Authorization: Bearer $CLARP_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"events": {"failed": false, "job_done": true}}' "$CLARP_SERVER_URL/notification-settings"
```

For one agent (by session or agent id). `null` goes back to the global choice:

```bash
curl -s -X POST -H "Authorization: Bearer $CLARP_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent": "theo", "events": {"job_done": true, "reply": null}}' "$CLARP_SERVER_URL/notification-settings"
```

An unknown event id is refused with 400 `unknown_event`, and an unknown agent with
404 `unknown_agent`. Nothing changes when a request is refused. The answer is the
whole document, as from `GET`. Every app is told at once
(`notification-settings-changed`).

To mute an agent completely, use `POST /agent-mute` `{"session": "theo", "muted": true}`.

Change these only when the user asked. Say what you changed in one short line.

## Mark a chat read

```bash
curl -s -X POST -H "Authorization: Bearer $CLARP_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"session": "vera"}' "$CLARP_SERVER_URL/chat-read"
```

Every app marks the chat read, and the phones take its notifications down. The
selection and focus stay as they are.

## Which apps show what

- The Host applies the switches to every push it sends, so they work at once on
  every phone, also on app versions that have no settings screen yet.
- The desktop app decides its popups from `alert` on `user-notification`.
  Versions from before contract 223 show a popup for every reply.
- The iOS and Android apps get settings screens for these switches, and the new
  notification actions (choices as buttons, mark read, retry), in their releases
  after contract 223. Until then the user changes the switches through you.
