#!/usr/bin/env python3
"""Run the Python Host's R175 rules using only stand-in transcription adapters."""
from contextlib import ExitStack, contextmanager
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/provider-once-vectors.json"
INK = "cartesia:ink-whisper"
INK2 = "cartesia:ink-2"
NOVA = "deepgram:nova-3"
SCRIBE = "elevenlabs:scribe_v2"
CHIRP = "google:chirp_3"
OWN = "faster-whisper:small"


@scratch.case("clarp-provider-once-vectors-")
def reference(root):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    (root / "scenario-config.toml").write_text("")
    sandbox.isolate(root)
    sandbox.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import call_transcription as call, heard_audio, settings_store
    from lib import stt_language as language, stt_providers as providers, stt_router as router
    from lib import transcription_pipeline as pipeline

    names = [
        "", " ", "local", " local ", "server-default", "server-default\x1f", OWN,
        f" {OWN} ", "faster-whisper:base", INK, INK2, f"\t{NOVA} ", NOVA.upper(),
        "cartesia:retired", " CARTESIA:retired ", "other:one", "own", "LOCAL",
    ]
    identity = [
        {"engine": name, "own": own, "identity": router.engine_identity(name, own)}
        for name in names for own in (None, "", OWN, "faster-whisper:base")
    ]
    orderings = [
        [], [""], [" ", "", "local", "server-default", OWN], [OWN, "local", OWN],
        [INK, INK2, INK, NOVA], [INK2, INK2, INK], [NOVA, NOVA.upper(), NOVA],
        [NOVA, f" {NOVA} ", NOVA, SCRIBE], ["local", "own", "LOCAL", OWN],
        [OWN, "faster-whisper:base", "local", "faster-whisper:base"],
        [INK, NOVA, INK, SCRIBE, NOVA, INK2, SCRIBE, CHIRP],
    ]
    distinct = []
    for order in orderings:
        for own in (None, OWN):
            kept, skipped = router.distinct_engines(order, own)
            distinct.append(dict(order=order, own=own, kept=kept, skipped=skipped))
    splits = [
        {"input": text, "engines": language.split_engines(text)}
        for text in (None, "", " , ,\t ", INK, f"{INK},{INK}", f"{INK}, {INK2}, {INK}, {INK2}",
                     f" {NOVA},\t{NOVA}, {SCRIBE} ", f"{INK},{INK.upper()},{INK}",
                     f"local,server-default,{OWN}, local", f",{CHIRP},,{NOVA},",
                     f"{NOVA}\u00a0, {NOVA}, \u2007{SCRIBE}, {SCRIBE}")
    ]
    merges = []
    existing = {"old": f"{INK},{INK}", "ada": NOVA}
    for update in (
        {}, {" ADA ": f"{INK}, {NOVA},{INK2},{INK}"}, {"ADA": f"{INK2}, {INK2} "},
        {"OLD": None}, {"ada": ""}, {"ada": " , "}, {"ada": f"{NOVA},retired,{NOVA}"},
        {"ada": f"\t{SCRIBE}, {SCRIBE}, , {NOVA}", "mike": f"{CHIRP}, {CHIRP}"},
        {"ada": 4}, {"ada": False}, {"": NOVA}, [NOVA], {"ada": [NOVA]},
    ):
        with patch.object(language, "agent_engines", return_value=existing.copy()):
            try:
                kept = language.merge_agent_engines(
                    update, is_valid=lambda model: model in {INK, INK2, NOVA, SCRIBE, CHIRP})
                merges.append(dict(update=update, kept=kept))
            except ValueError as error:
                merges.append(dict(update=update, error=str(error)))
    call_policies = []
    for raw in ("", f"{NOVA},{NOVA}", f" {INK}, {INK2},{INK}", f",{SCRIBE},,{SCRIBE},"):
        with patch.object(settings_store, "get_text", return_value=raw), \
                patch.object(settings_store, "get_many", return_value={call.CALL_ENGINES_KEY: raw}):
            _, canonical = call.validate_settings(
                "", raw, is_valid=lambda _: True, global_engine="local")
            call_policies.append(dict(raw=raw, engines=call.call_engines(),
                                      snapshot=call.policy_snapshot()["engines"], saved=canonical))

    contacts = []
    for engine in names:
        for handed in ({}, {"language": None}, {"language": "auto"}, {"language": "en"}):
            known = set()
            pending = [INK, INK2, NOVA, SCRIBE, CHIRP, "cartesia:retired", "local", "other"]
            with patch.object(pipeline, "log"):
                kept = pipeline._not_sent_twice(engine, handed, pending, known, "fixture")
            contacts.append(dict(engine=engine, observed="language" in handed,
                                 provider=pipeline._hosted_provider(engine),
                                 reached=pipeline._reached(engine, handed), pending=pending, kept=kept))

    def run(data, call_mode):
        tried, logs, failed_for_recovery = [], [], []
        current_wait = None

        @contextmanager
        def selection_wait(wait):
            nonlocal current_wait
            current_wait = wait
            yield

        def transcribe(model, *_args, **_kwargs):
            tried.append(dict(engine=model, wait=current_wait))
            outcome = data["outcomes"].get(model, "ok")
            if outcome == "before-language":
                raise providers.LanguageSetUnsupported("stand-in unsupported set")
            if outcome == "before-model":
                raise pipeline.STTUnknownModelError("stand-in unknown model")
            if outcome == "before-key":
                raise RuntimeError("stand-in missing key")
            language.note_handed(None if outcome == "after-auto" else "en")
            if outcome in ("after", "after-auto"):
                raise RuntimeError("stand-in provider error")
            return model, True, 1.0

        stt = SimpleNamespace(default_model_id=data.get("own"), transcribe_model_bytes=transcribe)
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline, "bounded_selection_wait", selection_wait))
            stack.enter_context(patch.object(pipeline, "log", lambda event, detail:
                                             logs.append(detail) if event == "sttEngineRepeatSkipped" else None))
            stack.enter_context(patch.object(pipeline, "log_exception", lambda *_a, **_kw: None))
            stack.enter_context(patch.object(pipeline, "time", SimpleNamespace(monotonic=lambda: 1)))
            stack.enter_context(patch.object(pipeline.health, "mark_success"))
            stack.enter_context(patch.object(pipeline.health, "mark_error"))
            stack.enter_context(patch.object(pipeline, "_cache_dir", return_value=root))
            stack.enter_context(patch.object(heard_audio, "retain"))
            if call_mode:
                steps = [dict(engine=engine, skip="language_incompatible")
                         if index in data["pre_skipped"]
                         else dict(engine=engine, language="en", adapted=False)
                         for index, engine in enumerate(data["order"])]
                fields = dict(language={}, engines={}, requested_model=None)
                try:
                    text, _, _, _ = pipeline._run_call_steps(
                        stt, steps, fields, focus=data["focus"], audio_bytes=b"fixture",
                        ctype="audio/webm", prompt=None)
                    result = dict(text=text)
                except Exception as error:  # a terminal fixture error is an observation
                    result = dict(error=str(error))
                return dict(tried=tried, result=result, attempts=fields["report"]["attempts"])

            def fallback(_stt, _say, _engine, *_args, failed=()):
                failed_for_recovery.extend(failed)
                return "recovered", True, 1.0, "server-default", "detected"

            stack.enter_context(patch.object(pipeline, "_language_set_fallback", fallback))
            output = pipeline._run_engines(
                stt, call_stt=None, call_steps=[], requested_model=data["order"][0],
                agent_fallbacks=data["order"][1:], focus=data["focus"], audio_bytes=b"fixture",
                ctype="audio/webm", prompt=None, trace_id="fixture", vocab_run_id=0,
                utterance_id="", client_ts=None, record_voice=lambda *_a, **_kw: None)
            result = dict(text=output[0]) if isinstance(output, tuple) else dict(status=output.status)
            return dict(tried=tried, logs=logs, failed=failed_for_recovery, result=result)

    chat_cases = [
        ([INK, INK2, NOVA], {}, None, "fixture"),
        ([INK, INK2, NOVA], {INK: "after"}, None, "fixture"),
        ([INK, INK2, NOVA], {INK: "after-auto"}, None, ""),
        ([INK, INK2, NOVA], {INK: "before-key"}, None, "fixture"),
        (["cartesia:retired", INK, NOVA], {"cartesia:retired": "before-model"}, None, "fixture"),
        ([INK, INK2], {INK: "before-language"}, None, "fixture"),
        ([INK, INK2], {INK: "after"}, None, "fixture"),
        ([INK, INK, NOVA, NOVA], {INK: "after"}, None, ""),
        ([OWN, "server-default", "local", "faster-whisper:base", NOVA],
         {OWN: "after", "faster-whisper:base": "after"}, OWN, "fixture"),
        ([INK, INK2, NOVA, SCRIBE], {INK: "before-key", INK2: "after", NOVA: "before-model",
                                    SCRIBE: "before-language"}, None, "fixture"),
        ([INK, INK2, NOVA], {INK: "after-auto", NOVA: "before-language"}, None, "fixture"),
        ([OWN, "local", NOVA], {OWN: "before-model", NOVA: "before-language"}, OWN, "fixture"),
        ([INK, INK2, NOVA, SCRIBE, CHIRP], {INK: "after", NOVA: "after", SCRIBE: "after"}, None, ""),
    ]
    chat = []
    for order, outcomes, own, focus in chat_cases:
        case = dict(order=order, outcomes=outcomes, own=own, focus=focus)
        chat.append(dict(**case, **run(case, False)))

    call_cases = [
        ([INK, INK2, NOVA], {}, []),
        ([INK, INK2, NOVA], {INK: "after"}, []),
        ([INK, INK2, NOVA], {INK: "before-key"}, []),
        ([INK, INK2, NOVA], {INK: "after-auto"}, []),
        ([INK, INK2, NOVA], {INK: "after"}, [1]),
        ([INK, INK2], {INK: "after"}, []),
        ([INK, INK2, NOVA], {INK: "after"}, [2]),
        ([INK, INK2, NOVA, SCRIBE], {INK: "after", NOVA: "after", SCRIBE: "after"}, []),
        ([INK, INK2, NOVA], {INK2: "after"}, [0, 2]),
        ([INK, INK2], {}, [0, 1]),
        (["cartesia:retired", INK2], {"cartesia:retired": "before-model"}, []),
    ]
    calls = []
    for order, outcomes, pre_skipped in call_cases:
        case = dict(order=order, outcomes=outcomes, pre_skipped=pre_skipped, focus="fixture")
        calls.append(dict(**case, **run(case, True)))
    return dict(splits=splits, merges=merges, stored=existing, identity=identity,
                distinct=distinct, contacts=contacts, chat=chat, calls=calls, call_policies=call_policies)


if __name__ == "__main__":
    OUT.write_text(json.dumps(reference(), ensure_ascii=True, indent=2, sort_keys=True) + "\n")
