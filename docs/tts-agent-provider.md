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
agent's stored voice map is used, then the contact's voice for that provider,
then the provider default. Config changes apply to the next clip without a
restart.

## A voice per contact and provider

A contact's voice is a map with one entry per provider, for example
`{"elevenlabs": "...", "cartesia": "...", "gemini": "voice_..."}`. Switching
`[tts] provider` picks that provider's entry; the other entries are kept, so
switching back restores the old voice.

Gemini voices for contacts come from `[gemini_tts.voices]` (contact name to a
prebuilt name or Voice Design id). Voice Design ids belong to the Google
project that created them, so there are no built-in defaults:

```toml
[gemini_tts.voices]
Arnold = "voice_..."   # designed to keep his Australian Cartesia accent
Ingrid = "voice_..."
```

An agent without its own Gemini voice uses the Gemini voice of the contact
whose Cartesia voice it has. With none, it stays silent, as it would under
Cartesia; `[gemini_tts] voice` is only the default for agents moved to Gemini
with `[tts.agents.<name>] provider = "gemini"`.

Built-in contacts get the `gemini` entry in their stored voice map on the next
server start; clips use the table immediately.

An agent always speaks with one voice. `[tts] fallback` still applies when the
agent's provider fails; leave it `none` to keep one voice per agent.

## Gemini

`lib/gemini_tts.py` streams Gemini 3.8 Flash TTS (24 kHz PCM over SSE) through
ffmpeg into MP3. `gemini_voice` takes a prebuilt name (`Kore`, `Charon`, ...)
or a Voice Design id (`voice_...`). First audio takes about 1-2.5 s, against
about 130 ms for Cartesia.

`[gemini_tts] backend` picks the API surface. `gemini_api` (default) calls
generativelanguage with `api_key`; on Tier 1 that project allows only 100
requests a day per model. `vertex` calls the Vertex AI global endpoint
(`aiplatform.googleapis.com`; regional endpoints 404 for the 3.8 TTS models)
with `vertex_api_key`, a Vertex express key, and has no fixed daily cap. First
audio on Vertex measured about 1.1 s.

Voice Design ids belong to the surface that created them. Agents and clients
keep seeing the Gemini API ids; on Vertex the worker swaps each for the same
contact's id in `[gemini_tts.vertex_voices]`. Vertex voices are created with
the Voices API (`v1beta1/projects/<p>/locations/global/voices`, OAuth only,
API keys are refused) and expire a year after last use.

## Delivery

Raw PCM delivery only exists for Cartesia. A clip from any other provider is
delivered as a chunked MP3 file, which every client plays from its URL;
clients already choose per clip.

Every clip logs a `tts_worker route` event with `provider` and `delivery`.
