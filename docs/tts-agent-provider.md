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

Clips the Host makes directly for an agent (a ring's opening line, the herald,
"is ready" announcements) follow the same route as its queued replies when the
entry changes something: another provider, or a `<provider>_voice` for the
provider in use. Such a clip then falls back only to `[tts] fallback`; the
built-in ElevenLabs backup of the global direct path no longer applies, so with
`fallback = "none"` a failing provider leaves that clip silent (the ring still
rings, the herald reports a failed synthesis). An entry that changes neither
(an empty voice, or a voice for a provider not in use) keeps the global path.
A voice preview always plays the voice it was given.

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

## Google Chirp 3 HD

`lib/google_tts.py` is the built-in `google` provider: one batch Cloud
Text-to-Speech request per clip, MP3 out. `google_voice` (or a contact's
`google` voice) is a full Google voice name from a fixed list, today the 30
Chirp 3 HD voices for `nb-NO` (`nb-NO-Chirp3-HD-Kore`, ...). An agent with no
Google voice at all speaks with `nb-NO-Chirp3-HD-Kore`; a saved voice that is
not on the list is refused (`voice_unavailable`) and never replaced, so only
the configured fallback, if any, speaks instead.

Credentials come from one place: an explicit `[google_tts]` with both
`credentials` (an `authorized_user` ADC file) and `project`, or, without that
section, whatever `[google_stt]` uses. An incomplete `[google_tts]` is an
error and never borrows from `[google_stt]`. `GET /tts/providers` reports the
`credential_source` and the `error`, never the file or the project.

Clarp's own pauses (`<break time="…"/>`, at most 3 s) become the only SSML
element; every other character is escaped text. Text over Google's 5,000-byte
limit is refused, not cut. The installed `custom.google-chirp` adapter, if
any, is a separate provider with its own stored voices.

### An old custom adapter whose id is `google`

Before the built-in provider existed, a custom adapter could use the id
`google`. Such a package no longer loads, and every stored `google` (in
`[tts]`, in `[tts.agents.*]` and in contact voices) would otherwise silently
mean the built-in provider. Instead the Host refuses built-in Google with a
fixed code until an operator decides:

- `google_id_collision`: an installed package claims the id `google`;
- `google_id_unverifiable`: the adapter directory, or a manifest in it, cannot
  be inspected (a symlinked or unreadable directory; a symlinked, unreadable,
  oversized or unparseable manifest, or one that is not a JSON object).
  Symlinks are never followed, so a symlinked manifest or package blocks
  even when its target is harmless, and so does any symlink directly in
  the adapter directory (it could be a package).

An id counts as `google` exactly when the old Host read it as `google`
(trimmed, lower case); any other id, a number or an empty id is no
collision.

While refused, `GET /tts/providers` and the voice catalog show `google` as
unavailable with that code (Google voices are listed with `current: false`
and `taken_by: null`); `POST /tts/providers`, `POST /agent-voice` and
`GET /voice-preview` answer 409 `{"error": "<code>"}`; a clip routed to
Google fails with the code unless an explicit non-Google `[tts] fallback`
speaks instead (the `providerFallback` event carries the code). Nothing stored
is rewritten, and the package stays where it is. The event log records a
refused clip as `googleIdCollision`.

`clarp-admin tts google-collision --report` lists the packages, every stored
`google` use and the exact table that would resolve it. In a container,
`docker compose run --rm clarp clarp-admin tts google-collision
--resolve-builtin [--dry-run]` writes `[tts.google_resolution]` (choice
`builtin`, bound to the colliding packages' fingerprints) with the Host
stopped and the data root held exclusively, as a restore is; any change to
those packages makes the resolution void again. `--dry-run` never blocks and
writes nothing: it prints the packages, the table and whether exclusive access
is obtainable now (or which exit code, 75, 76 or 77, would refuse it). In
native mode the command refuses: stop the services and paste the reported
table yourself.

A `tts-adapters.d` that is itself a symlink (for example one managed by
dotfiles) or not a directory is never followed, so it blocks built-in Google
with `google_id_unverifiable` even when no package claims `google`, and
`--resolve-builtin` cannot lift that. Replace it with a real directory or
remove it. Only the last path component is checked; a symlinked parent
directory is followed as usual.

## Delivery

Raw PCM delivery only exists for Cartesia. A clip from any other provider is
delivered as a chunked MP3 file, which every client plays from its URL;
clients already choose per clip.

Every clip logs a `tts_worker route` event with `provider` and `delivery`.
A synthesized clip then logs `tts_worker spoke` with the `provider` and
`voice` that actually spoke (`fallback_for` names the routed provider when its
fallback spoke instead).
