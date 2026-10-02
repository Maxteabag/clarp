# Oracle on Gemini Live

Oracle v2 calls can run on Google's Gemini Live (`gemini-3.8-live`) instead of
OpenAI's GPT-Live. GPT-Live stays the default.

## Choosing it

- `config.toml`: `[oracle] voice_provider = "gemini"` (default `"openai"`),
  optional `gemini_model` (default `gemini-3.8-live`) and `gemini_voice`
  (default: the `Oracle` entry in `[gemini_tts.voices]`, else
  `[gemini_tts] voice`).
- At runtime, overriding the config: `POST /oracle/voice-provider
  {"provider": "gemini"}` or `{"provider": "openai"}`; `GET` shows the choice,
  what new calls actually use and why they differ. The next call uses it; a
  call in progress keeps its model.
- The Gemini key is `[gemini_tts] api_key` (or `GEMINI_API_KEY`). Without it,
  calls use GPT-Live. The OpenAI key is still required: the router that turns
  requests into agent work is an OpenAI model in both cases.

If Gemini cannot be reached, or rejects the session before it is set up, that
call runs on GPT-Live and new calls keep using GPT-Live for ten minutes, so a
broken Gemini setup never leaves Oracle silent. Podcast detours and the
WebRTC "tinkered" engine always use GPT-Live.

## How it works

`lib/oracle_gemini_live.py` is a socket that speaks GPT-Live's `session.*`
events to the stable Conversation, so routing, relays, memory, earcons and
handoffs are the same code on both models. The mapping, and what differs:

| GPT-Live | Gemini Live |
|---|---|
| `session.start` | `setup`; startup history joins the system instruction |
| 24 kHz PCM both ways | sent up as `audio/pcm;rate=24000` (Gemini resamples); 24 kHz down |
| `delegation.created` | non-blocking `delegate_to_agent` function call; the route's appends come back as its `functionResponse` (`WHEN_IDLE`) |
| commentary / thinking / instructions appends | `clientContent` text marked `[Host, speak]` (completes a turn) or `[Host, context]` (silent) |
| phone `oracle_v2.interrupt` | the rest of the current reply's audio is dropped on the Host |
| (none) | `serverContent.interrupted` → `oracle_v2.playback_flush` + `oracle_v2.quiet` to the phone |
| usage in seconds | token counts priced at list rates → `oracle_v2.usage` with `billing: "gemini_api"` |

A Gemini connection lasts about ten minutes. On `goAway`, the Host reconnects
with the latest session-resumption handle once Oracle stops speaking, or two
seconds before the deadline. An unexpected close is handled the same way.
Context-window compression is on, so long calls stay inside the context limit.

Known gaps: Gemini has no truncate, so it does not know how much of an
interrupted reply was heard, and older iOS builds ignore `playback_flush`.
On those builds, audio already buffered plays out after a barge-in.
