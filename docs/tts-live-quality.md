# Live and quality voices

Clarp can speak a clip with one of two voices:

- **Live** (`[tts] provider`): used when the agent's chat is open, so a
  back-and-forth conversation starts speaking quickly. Cartesia streams the
  first audio in about 130 ms.
- **Quality** (`[tts] quality_provider`): used when the chat is not open,
  because nobody is waiting on it. Gemini 3.8 Flash TTS takes 1-2 s to start
  and sounds better.

When `quality_provider` is empty every clip uses the live voice, which is the
old behavior.

## When is a chat open

`lib/tts_mode.py` treats a chat as open when both are true:

1. The agent is the focused agent (the `focus` table, set when a client
   opens a chat with `/select` or `/focus`).
2. A client is in front: an iOS `/application-activity` lease reported as
   foreground, or an active `/desktop-presence` lease. Both expire 45 s after
   the client's last report.

Focus is not cleared when a chat is closed, so the last chat opened while the
app is on screen still counts as open. If presence cannot be read, the clip
uses the live voice.

## Delivery

Raw PCM delivery only exists for Cartesia. A clip routed to any other
provider is delivered as a chunked MP3 file, which every client plays from
its URL; clients already decide per clip.

## Failures

If the quality voice fails (a Gemini 429 or 503, a missing key), the clip is
spoken by the live voice instead. Live clips use `[tts] fallback` as before.

## Per-agent overrides

Use `[tts.agents.<name or session>]` to try a voice on one agent:

```toml
[tts]
provider = "cartesia"

[tts.agents.Jax]
quality_provider = "gemini"
gemini_voice = "Charon"

[gemini_tts]
api_key = ""                      # or GEMINI_API_KEY
model = "gemini-3.8-flash-tts"
voice = "Kore"
```

Keys: `live_provider`, `quality_provider`, and `<provider>_voice` (for example
`cartesia_voice`, `gemini_voice`) for the provider the clip is routed to.
Config changes apply to the next clip without a restart.

Every clip logs a `tts_worker route` event with `mode`, `provider`,
`chat_open` and `delivery`.
