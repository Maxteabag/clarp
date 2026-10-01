# Per-agent voice provider

Every agent speaks with `[tts] provider` unless it has its own entry, keyed by
agent name or session:

```toml
[tts]
provider = "cartesia"

[tts.agents.Ingrid]
provider = "gemini"
gemini_voice = "Charon"

[gemini_tts]
api_key = ""                      # or GEMINI_API_KEY
model = "gemini-3.8-flash-tts"
voice = "Kore"                    # default when an agent names no gemini_voice
```

Keys: `provider`, and `<provider>_voice` (for example `cartesia_voice` or
`gemini_voice`) for that provider's voice. Without a `<provider>_voice` the
agent's stored voice map is used, then the provider default. Config changes
apply to the next clip without a restart.

An agent always speaks with one voice. `[tts] fallback` still applies when the
agent's provider fails; leave it `none` to keep one voice per agent.

## Gemini

`lib/gemini_tts.py` streams Gemini 3.8 Flash TTS (24 kHz PCM over SSE) through
ffmpeg into MP3. `gemini_voice` takes a prebuilt name (`Kore`, `Charon`, ...)
or a Voice Design id (`voice_...`). First audio takes about 1-2.5 s, against
about 130 ms for Cartesia.

## Delivery

Raw PCM delivery only exists for Cartesia. A clip from any other provider is
delivered as a chunked MP3 file, which every client plays from its URL;
clients already choose per clip.

Every clip logs a `tts_worker route` event with `provider` and `delivery`.
