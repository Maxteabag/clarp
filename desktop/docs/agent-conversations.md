# Agent-to-agent conversations

When one agent prompts another (`POST /send` with `sender`), the recipient's
transcript stores a `user` row with `origin: agent` and the sender's stable
`agent_id`. The recipient's answer stores the same id so the trigger remains
attributable, but the answer is authored by the recipient.

## Attribution rules in the timeline

- `MessageDelegate.teamAuthored` is true only for an incoming `user` row with
  `origin: agent`. It renders right-aligned with the sender's avatar.
- An `assistant` row with `origin: agent` is the current agent's own reply. It
  renders as an ordinary reply with a compact marker: `↩ Replying to <name>`.
  The marker never quotes the answered text. The Host supplies
  `reply_to_agent_id/name/session`; `sender_*` are empty on such rows.

## Pair rooms

`GET /agent-conversations` lists one room per pair of agents that have
exchanged at least one delivered prompt. Rooms are keyed by
`pair:<lower agent_id>:<higher agent_id>`, so direction, retries and renames
never create a second room. The sidebar shows them under "Agent conversations"
with both avatars, a preview and an unread dot. Unread is device-local: the
last seen `latest_revision` per Host and room is stored in QSettings.

Selecting a room calls `selectSession("pair:…")`. The controller treats pair
ids as projections: no `POST /select`, no clip recovery, no media load and no
agent-guarded shortcuts (`hasAgent` is false). The timeline loads through the
ordinary `/log` tail/delta/older machinery; `transcript-updated` for either
participant refreshes open rooms and debounces a list refresh.

The pane renders every row as an authored group message (`groupView`): avatar
on the left, author name, then `Replying to <other> · private reply` for
answers. A private reply lived only in the author's own chat; the UI never
claims it was delivered to the other agent. The composer is replaced by a
footer note; agents are messaged from their own chats.

## Verification

```sh
ctest --test-dir desktop/build/release -R 'core|activity-layout' --output-on-failure
```

`tst_native_core` covers reply provenance roles, cache round-trips and the
pair-room controller flow against the fake Host. `tst_message_attribution.qml`
covers incoming versus reply rendering and the group view; `tst_pair_rows.qml`
covers the sidebar row, unread dot and selection.
