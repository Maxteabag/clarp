---
name: clarp-agent-admin
description: Create or refine Clarp agent personas, including identity, personality, voice, and avatar direction.
---

# Clarp Agent Administration

Use the server's agent creation flow for the durable persona record. A good
persona has a distinct name, a concise behavioral description, a voice that is
not already occupied, and recognizable artwork.

## Workflow

1. Resolve the target server and session with `clarp-sessions`.
2. Preserve the user's explicit name, backend, directory, and voice choices.
3. Write personality guidance as concrete behavior, tone, and decision style;
   avoid biography that does not affect responses.
4. Prefer the Cartesia catalog exposed by the server over hard-coded voice IDs.
   Preview candidates and do not silently reuse a voice marked as occupied.
5. Use the app's symbol avatar when image generation is unavailable.
6. If an image-generation tool is available and the user requests artwork,
   generate a square, face-readable avatar with no text. Do not assume an
   OpenAI key exists on the Clarp server: image generation belongs to the
   current agent/tool environment. Publish the result with `clarp-media` and
   let the user approve it before replacing an established avatar.

## Creating a personality

A personality is one live `POST /personas` call. It needs no deploy and no
restart. Reach the server on `[server].bind_addr` and `[server].port` from
the configuration reported by `clarp-admin paths` — it is not necessarily on localhost —
and authenticate with `[server].auth_token` as a bearer token.

```
POST /personas {name, voice_id, personality, avatar_base64, avatar_symbol}
```

- `voice_id` is a JSON *string*, not an object: `{"cartesia":"<uuid>"}`. Copy
  the chosen voice's `selection_value` verbatim.
- `avatar_base64` is a square **JPEG** of at most 512000 raw bytes. It is
  stored server-side and served at `/persona-avatars/<persona_id>`.
- `personality` is at most 4000 characters, and takes precedence over any
  built-in clause for that name.

`POST /agents {name, cwd, backend}` then starts a session that inherits the
persona's voice, personality, and avatar. `DELETE /personas/<name>` removes a
non-builtin.

Never register a new personality in the server source — the roster, Cartesia
voice, and personality tables in `lib/config.py`, or an avatar in
`static/avatars/`. That path is for built-ins only, and none of it takes
effect until the server restarts.

## Choosing a voice

`GET /cartesia-voices` returns the English catalog: `name`, `tagline`,
`description`, `gender`, `country`, `preview_url`, `selection_value`, and
`taken_by`. Play `preview_url` for the shortlist and let the user pick; do not
choose a voice on description alone. `taken_by` is computed from running
agents only, so also check the personas list for a voice already claimed by a
personality with no live session.

Never expose provider API keys to the phone or place them in prompts. Creating
or editing a persona does not authorize deleting its conversation history.
