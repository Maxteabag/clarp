# Mic admission and plain-TTS capability boundaries

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R11** `tests/regression/test_audio_lifecycle_failures.py::test_tts_auth_error_does_not_claim_an_elevenlabs_quota_failure`: Cartesia authentication failure is labeled ElevenLabs quota. Provider and error category must determine the message; keep raw details in diagnostics.
- **R37** `tests/unit/test_custom_tts_adapters.py::test_ssml_capability_rejects_truthy_non_boolean_values`: bool("false") advertises SSML support and leaks markup to a plain adapter. Validate the capability as a JSON boolean or default conservatively; do not rely on truthiness.
- **R47** `tests/unit/test_voice_markup.py::test_plain_tts_strips_ssml_outside_clarps_internal_markup_vocabulary`: The plain-provider boundary promises to strip SSML but leaves standard prosody/emphasis/say-as tags. Strip the supported SSML vocabulary without deleting literal comparison/code text.
- **R49** `tests/state/mic.test.js — a second tap cancels a start that is still awaiting mic permission`: Two taps before permission resolves create two getUserMedia requests and can start capture after cancel intent. Coalesce acquisition and fence late grants; release stale tracks.

## Implementation and verification

Coalesce pending microphone acquisition and honor cancellation before a late permission grant. Stop stale tracks. Require boolean SSML capabilities and remove standard SSML at plain-provider boundaries without stripping literal code/comparisons. Attribute provider authentication failures correctly. Recorder error/stop ordering and diagnostic logging are separate qualified follow-ups, not proven permanent capture leaks.

## Qualified or excluded claims

- **R50** (needs-contract): The test requires onerror and invokes it alone, while onstop already clears capturing. It does not prove a permanent stuck recorder. Model error/data/stop ordering and establish whether partial audio should be uploaded; diagnostic absence is separate.
- **R51** (deferred): HTTP transcription failures already flash a user-visible error. Missing durable diagnostics is useful observability work, not proof of lost recorder state. Avoid logging unlimited provider text or transcript content.
