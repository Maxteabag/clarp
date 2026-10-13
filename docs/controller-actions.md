# Controller custom actions

A Flic button or the Watch can run a **custom action** instead of a voice
behaviour. Agents define the actions and map gestures to them; the phone only
ever sends an action id (or the gesture it saw) and the Host does the rest.

## Actions

An action is `{id, title, type, enabled}` plus the fields of its type:

| Type | Fields | Does |
|---|---|---|
| `http_post` | `url`, `body` (JSON object, up to 8 KB) | POSTs `body` to `url` with a 5 s timeout, no redirects. 2xx is success. |
| `agent_prompt` | `session`, `text` | Sends `text` to that Clarp agent, like `clarp-admin prompt`. |

`url` must be allow-listed: loopback (`http://127.0.0.1`, `http://localhost`,
`http://[::1]`) always is; anything else needs a prefix under
`[controller_actions] allowed_urls` in `config.toml`. The list is checked when
an action is saved and again when it runs. There is no shell action, and
nothing in a run request can change what an action does.

## Mappings

A mapping is `{device, button, gesture, action_id}`. `device` is `*` (every
controller) or one controller's id; a device's own mapping wins over `*`.
`button` is `primary` or `secondary`; `gesture` is one of `single-click`,
`double-click`, `hold`, `swipe-left|right|up|down` and
`rotate-clockwise|counterclockwise`. An unmapped gesture keeps its voice
behaviour on the phone.

## HTTP

| Request | Who | |
|---|---|---|
| `GET /controller-actions` | any device | `{revision, actions, mappings, types, buttons, gestures}` |
| `POST /controller-actions` | local administrator | create; 201 `{action}` |
| `POST /controller-actions/update` | local administrator | `{id, …changed fields}` |
| `POST /controller-actions/delete` | local administrator | `{id}`; also clears its mappings |
| `POST /controller-actions/mappings` | full device | `{device, button, gesture, action_id}`; `action_id: null` clears |
| `POST /controller-actions/<id>/invoke` | paired device, limited too | `{request_id}` required; optional `device`, `button`, `gesture`, `source_kind` for the audit |
| `GET /controller-actions/audit?limit=` | any device | newest first |

Invoke answers 200 `{ok, action_id, title, type, status?, error?, duration_ms}`
even when the target fails (`ok: false`); 400 without a `request_id`, 404 for
an unknown action, 409 for a disabled one. Nothing else in the body is used:
the phone never supplies a URL, body, prompt or command. A repeated
`request_id` within ten minutes, even one arriving while the first is still
running, returns the first result with `duplicate: true`. Another request for
the same action within 1.5 s of a run that is in flight or succeeded returns
`debounced: true` without running; a failure can be retried at once. The
target has a 5 s timeout (`agent_prompt` 15 s).

The phone resolves the gesture itself: it only invokes when the exact
controller (or `*`), button and gesture are mapped, and only for the small
button's swipes and presses. The small button's hold stays the emergency stop
and the large button is never mapped. Every mappable gesture already has a
voice behaviour (swipe up replays, swipe down skips, …), so a mapping is an
explicit opt-in that replaces it for that controller.

Every change to the registry sends the ephemeral SSE event
`controller-actions-updated {revision}`; clients refetch when it is newer.
Every definition change and run is audited (last 500 rows).

## clarp-admin

```bash
clarp-admin controller-action add rk-demo-leak --title "Demo leak" \
  --http-post http://127.0.0.1:8765/api/scenario \
  --body '{"feed":"demo-cam-02","clip":"leak"}'
clarp-admin controller-action add ask-koko --title "Ask Koko" --agent koko-5850 --text "Status?"
clarp-admin controller-action map --button secondary --gesture swipe-up --action rk-demo-leak
clarp-admin controller-action unmap --button secondary --gesture swipe-up
clarp-admin controller-action update rk-demo-leak --disable
clarp-admin controller-action list | run <id> | remove <id> | audit
```

The Ringerikskraft demo's two actions (`rk-demo-leak`, `rk-demo-reset`, both
POSTing to the leak-watch scenario endpoint) are seeded by
`scripts/demo/rk-demo-controller-actions.sh`; add `--map` to bind the small
button's swipe up and down to them.
