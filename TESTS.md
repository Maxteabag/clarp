# Test inventory — one regression test per bug we've already fixed

## How to run

| Layer | Command | Where it runs | Time |
|---|---|---|---|
| Python unit + integration (1,500 tests) | `make py` | in-process, every core, 60 s per-test timeout | ~30 s |
| JavaScript unit (250 tests) | `make js` | vitest, in-process | ~15 s |
| Container install, restart, backup | `make docker-test` | throwaway Docker node | minutes |
| Browser suite (Playwright) | `make e2e` | Chromium on the host against a throwaway Docker node | minutes |

The in-process suites never touch the host: each test owns a temp database,
config, cache, and port, `systemctl`/`launchctl` are shimmed, and a conftest
guard refuses any socket that is not loopback. Playwright has no default
server; `make e2e` starts one in Docker with the auth token `test` and the
built-in roster, and `CLARP_BASE_URL` must be set explicitly otherwise.

For each bug we hit during development, this lists the symptom, where the bug
actually lived (the **layer**), and what test would catch it now. This becomes
the initial test suite scaffold.

Layers, cheapest first:

- **Pure** — a function with inputs and outputs and no I/O. Fastest tests.
- **State** — client-side logic with explicit state (state machine, queue
  scheduler). Mock the player, the clock, the network.
- **Hook** — a Python hook script. Use a tmp project tree and a stub TTS.
- **API** — full HTTP roundtrip via a test client to the running server, with
  external systems (AI CLI backends, ElevenLabs, faster-whisper) mocked or stubbed.
- **E2E** — real browser, Playwright. Reserved for the bugs that genuinely
  require iOS-y behaviour.

## Bugs we've encountered, mapped to tests

### B1 — Server bound to 127.0.0.1, unreachable from phone
- **Layer**: API (config check).
- **Test**: start the server with an injectable bind address; assert that the
  default config binds `0.0.0.0` and that an integration test from a non-
  loopback address gets a 200 on `/health`.

### B2 — iOS NotAllowedError on audio.play() after autoplay re-locked
- **Layer**: E2E (browser-specific).
- **Test**: Playwright test that loads the PWA, taps the mic to unlock, waits
  thirty seconds, then asserts the next clip plays without a NotAllowedError.
  Until E2E exists, a **State** test asserts that on a `NotAllowedError`
  rejection the clip is re-queued at the head and retried.

### B3 — iOS premature `ended` with `preload="none"`
- **Layer**: State (audio scheduler with a mock element).
- **Test**: fake the player to fire `ended` at `currentTime < duration * 0.85`;
  assert the scheduler resumes the same clip instead of advancing.

### B4 — Whisper hallucinations on silence (`Thank you`, `Bye`, etc.)
- **Layer**: Pure (hallucination filter).
- **Test**: table-driven test over thirty known hallucination phrases; assert
  `filter(p)` returns `""`. Plus negative cases ("Thank you for explaining
  the bug") that must NOT be filtered.

### B5 — Multi-sentence YouTube outro hallucination
- **Layer**: Pure.
- **Test**: feed "Thank you very much. I hope you enjoyed it. Good night."
  through the per-sentence filter; assert empty. Important: a single real
  sentence "Thank you, that worked." must NOT be dropped.

### B6 — SSE replay on PWA reopen blasts old clips
- **Layer**: State (SSE handler with `lastAudioTs` filter).
- **Test**: seed `lastAudioTs` to T, feed three SSE events with timestamps
  T-1000, T+500, T-200; assert only T+500 is enqueued, the others are
  marked `sseSkipOld`.

### B7 — TTS cuts off at angle-bracket fragments like `<space>`
- **Layer**: Pure (sanitiser).
- **Test**: `sanitize("show only <space> / <?> here")` returns "show only /
  here" (or similar), without the angle-bracket fragments. Includes a
  parametrised matrix of weird-but-legal angle-bracket inputs.

### B8 — Cross-device OSError moving MP3 from /tmp to ~/.cache
- **Layer**: Hook (filesystem contract).
- **Test**: in a pytest fixture, mount tmp on tmpfs and cache on a separate
  tmpfs-backed dir, simulate the worker's tmpfile→move flow; assert the
  destination exists and tmp is gone. Effectively asserts `shutil.move` is
  used (not `os.replace`).

### B9 — First-run position tracking played whole history on resume
- **Layer**: Hook.
- **Test**: create a fake transcript with 50 entries, no cursor row; run
  the hook; assert it stays silent and writes the current position. Then
  append three new entries and re-run; assert only those three appear in
  the synthesis call.

### B10 — PostToolUse fires before transcript flushed
- **Layer**: Hook + state.
- **Test**: race test — simulate the hook firing immediately after a tool;
  assert that if no new text is found, position advances and no TTS is
  attempted (no orphan tmp file left in `/tmp`).

### B11 — Two concurrent hooks racing on the transcript cursor
- **Layer**: Hook (concurrency).
- **Test**: fire two hook processes simultaneously against the same session;
  assert the same text isn't spoken twice and the position ends correctly.
  Uses `pytest-xdist` or a threaded harness.

### B12 — Routing required the agent name as the very first word
- **Layer**: Pure (router).
- **Test**: parametrise over forms — "Rachel, do X", "Hi Rachel", "Hey
  Rachel can you", "talk to Rachel about" — all route to Rachel when she
  appears in the first three words.

### B13 — Whisper transcribed Rachel as "Chill" / Bella as "Bell"
- **Layer**: Pure (fuzzy router + vocab prompt).
- **Test**: assert `_word_similarity("Bell", "Bella") >= 0.78` and that
  `resolve_agent_by_spoken_name("Bell can you", {agents})` returns Bella's
  session. Also a property test: every roster name fuzzes to itself.

### B14 — Routing target changed when chip moved mid-recording
- **Layer**: State.
- **Test**: drive the state machine through `startCapture` with the chip on
  Mike, then switch chip to Rachel, then `onCaptureEnd`; assert the
  `sendText` call uses session=mike (locked at record-start).

### B15 — Background agent played before addressee while addressee not yet busy
- **Layer**: State (scheduler + state machine).
- **Test**: in `awaiting` state for session=mike, feed two SSE events —
  Rachel's clip then Mike's; assert Mike's plays first and Rachel waits.
  Also: in `awaiting` state with only Rachel's clip and no Mike clip,
  assert nothing plays until either Mike's clip arrives or the safety
  deadline elapses.

### B16 — Queue stuck because of stale `isCapturing`/`playing` state
- **Layer**: State.
- **Test**: enter `recording`, drop two clips, transition to `transcribing`
  and then `idle`; assert that `playNext` is called and clips drain. With
  the state machine in place, the `setInterval(playNext, 1000)` safety net
  becomes redundant — the test should pass without it.

### B17 — New agent sessions had no audio mode → silent
- **Status**: Obsolete. Per-session audio modes (the `/audio` command and
  `~/.config/claude-tts/modes/`) were removed; the server-side streamer
  always voices `<speak>` regions and muting is the per-turn
  `synthesize_audio` flag, so a fresh session can no longer resolve to
  "off" by accident.

### B18 — TTS character cap of 2000 truncated long replies mid-sentence
- **Layer**: Pure / hook.
- **Test**: feed an 1800-character text; assert no truncation. Feed an
  8500-character text; assert truncation at the last word boundary
  before 8000, ending with "…".

### B19 — MAX_UTTER_MS = 25s cut tap-recording short
- **Layer**: State.
- **Test**: in singleShot mode, simulate 60s of capture; assert no
  auto-stop before user taps to end. In continuous mode, assert the cap
  still applies if user set one.

### B20 — Whisper VAD silence threshold chunked long sentences mid-speech
- **Layer**: Pure / config.
- **Test**: feed audio frames simulating a 4-second mid-sentence pause; assert
  the VAD does not end the utterance with a threshold of 2500ms but does
  with 1200ms. Effectively a threshold-sensitivity test.

### B21 — Wind kept VAD energy high → no end-of-speech detection
- **Layer**: Pure (energy reader).
- **Test**: synthesise a frequency-domain frame with energy concentrated
  below 300 Hz (wind rumble); assert `readEnergy()` returns below the
  silence threshold. With voice band centered at 1 kHz, energy is above
  the threshold.

### B22 — iOS chat input hidden behind keyboard
- **Layer**: E2E.
- **Test**: Playwright on mobile Safari simulation (or real device farm);
  open chat, focus input, assert the chat bar is visible above the keyboard
  rect via `visualViewport.height`.

### B23 — Reconnect overlay flashed on brief SSE blips
- **Layer**: State.
- **Test**: drive the connection state through `live → dead → live` in 5
  seconds; assert reconnect overlay never appears. Then keep `dead` for 16
  seconds; assert it appears exactly at 15 seconds.

### B24 — Stop button state stale after hook completion
- **Layer**: API + Hook.
- **Test**: simulate a UserPromptSubmit hook fire; assert busy marker
  exists for the session and `/status` returns `busy: true`. Then simulate
  a Stop hook fire; assert marker is gone and `/status` returns false.

### B25 — Domi created without voice_id (null fallback to default voice)
- **Layer**: API.
- **Test**: POST `/agents` with `{name: "Domi"}` and no voice_id; assert the
  saved agent has Domi's roster voice id. Then POST `{name: "Mike"}` for
  a name already present; assert 409 conflict.

### B26 — Retired persistent-terminal dispatch path
- **Layer**: Architecture.
- **Test**: `/send` integration tests assert per-turn backend dispatch. There
  is no long-lived subprocess or keystroke endpoint.

### B27 — Service worker served stale app.js on update
- **Layer**: E2E.
- **Test**: Playwright loads the PWA; assert that on a server-side asset
  hash change, the SW activates the new version within the configured
  poll interval and the page reloads.

### B28 — Background agent identification removed but originally added unwanted "X here" prefix
- **Layer**: State / config.
- **Test**: assert no prefix is added to any TTS text. (The previous version
  added "Bella here." for non-addressee clips — we don't want that back.)

### B29 — Chip showed session id instead of persona name
- **Layer**: State.
- **Test**: given `agentsBySession = {claude: {name: "Mike", ...}}` and
  `currentSession = "claude"`, assert `chipLabel(currentSession) === "Mike"`.

### B30 — Whisper picked up "Thank you" on dead air between turns
- Same as B4 / B5; included here as one of the noisiest false signals so
  the test suite carries multiple representative phrases.

## Cross-cutting tests

Beyond the per-bug tests, there are some general invariants worth pinning:

- **No real time in unit tests**. Tests inject a clock and assert against
  it. A test that calls `time.sleep` or relies on `setTimeout` fires is
  banned from the unit suite — those belong in integration or E2E.
- **No real ElevenLabs calls**. The TTS engine is an interface; the real
  implementation is one impl, the test impl returns a deterministic dummy
  MP3 (a small valid silent .mp3 byte string).
- **No real Whisper inference in the fast suite**. The Whisper engine
  interface is mocked; we have one slower integration test that runs the
  real model on a handful of canned audio files (silent 1s clip, a "hello",
  the "thank you very much" hallucination trigger) to assert end-to-end.
- **Session-id sanitisation property test**: for any random unicode string,
  the sanitised session id is alphanumeric / dot / underscore / hyphen and
  never empty.
- **Filename protocol property test**: for any agent session id, the round
  trip `make_filename(session) → parse_filename(name)` returns the same
  session.

## Suggested directory layout

```
tests/
  unit/                       — pure functions
    test_routing.py
    test_hallucinations.py
    test_sanitize.py
    test_word_similarity.py
    test_vocab_prompt.py
  state/                      — JS state machine + queue (vitest)
    state-machine.test.js
    audio-queue.test.js
    sse-filter.test.js
    chip-label.test.js
  hook/                       — pytest with fixtures
    test_transcript_cursor.py
    test_first_run.py
    test_concurrent_hooks.py
    test_cross_fs_move.py
    test_mode_fallback.py
  api/                        — pytest + httpx client to a live server
    test_send_routing.py
    test_agents_crud.py
    test_status_busy_marker.py
    test_transcribe_endpoint.py
  e2e/                        — playwright, slowest, run on PRs only
    autoplay-unlock.spec.ts
    keyboard-lift.spec.ts
    sw-update.spec.ts
```

## Philosophy summary

1. Every reproducible bug becomes a test.
2. Test at the lowest layer the bug actually lives in.
3. Time is injectable. No real sleeps in unit tests.
4. External systems (AI CLI backends, ElevenLabs, Whisper, the browser) are interfaces
   with one real impl and one fake impl. Tests use the fakes.
5. End-to-end is reserved for the genuinely browser-specific bugs.
6. Each test file leads with a one-line comment naming the bug it pins.
