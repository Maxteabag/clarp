"""Characterization tests for the stable engine's podcast detours in lib.podcast_live.

Pins episode metadata validation, the bounded playhead context, the Live
session config for podcast mode, the fact-checked image loop (with the
OpenAI calls faked), and the PodcastConversation overrides: resume command,
image trigger gating, the eight-minute cap, and no external actions.
"""
from __future__ import annotations

import base64
import json
import threading
from types import SimpleNamespace

import pytest

from lib import oracle_live_stable, podcast_live as pl

REV = "a" * 64


def _episode(**overrides):
    ep = {
        "version": 1, "revision": REV,
        "transcript": [
            {"start": 0, "end": 30, "text": "intro words"},
            {"start": 30, "end": 90, "text": "middle words"},
            {"start": 90, "end": 150, "text": "late words"},
            {"start": 150, "end": 200, "text": "outro words"},
        ],
        "chapters": [
            {"start": 0, "end": 100, "title": "One", "source": "source one"},
            {"start": 100, "end": 200, "title": "Two", "source": "source two"},
        ],
        "corrections": "the host misspoke at 40s",
        "source_artifact_id": "art-1",
    }
    ep.update(overrides)
    return ep


# ---- validate_episode ------------------------------------------------------


def test_valid_episode_is_returned_unchanged():
    ep = _episode()
    assert pl.validate_episode(ep) is ep
    assert pl.validate_episode(_episode(version=None)) is not None


@pytest.mark.parametrize("mutate,message", [
    (lambda ep: "not a dict", "Unsupported podcast metadata"),
    (lambda ep: {**ep, "version": 2}, "Unsupported podcast metadata"),
    (lambda ep: {**ep, "revision": "abc"}, "audio SHA256"),
    (lambda ep: {**ep, "revision": REV.upper()}, "audio SHA256"),
    (lambda ep: {**ep, "transcript": []}, "bounded transcript"),
    (lambda ep: {**ep, "transcript": "x"}, "bounded transcript"),
    (lambda ep: {**ep, "chapters": [ep["chapters"][0]] * 101}, "bounded transcript"),
    (lambda ep: {**ep, "transcript": [{"start": 10, "end": 5, "text": "t"}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": 0, "end": 5, "text": ""}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": 0, "end": 5, "text": "x" * 2001}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": 0, "end": 86401, "text": "t"}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": -1, "end": 5, "text": "t"}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": True, "end": 5, "text": "t"}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": float("nan"), "end": 5, "text": "t"}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "transcript": [{"start": 50, "end": 60, "text": "b"},
                                       {"start": 10, "end": 20, "text": "a"}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "chapters": [{"start": 0, "end": 10, "source": "s"}]}, "chapter title"),
    (lambda ep: {**ep, "chapters": [{"start": 0, "end": 10, "source": "s", "title": "t" * 301}]}, "chapter title"),
    (lambda ep: {**ep, "chapters": [{"start": 0, "end": 10, "title": "t", "source": "s" * 16001}]}, "Invalid podcast passage"),
    (lambda ep: {**ep, "corrections": "c" * 4001}, "corrections"),
    (lambda ep: {**ep, "corrections": 5}, "corrections"),
    (lambda ep: {**ep, "source_artifact_id": "s" * 161}, "source artifact"),
    (lambda ep: {**ep, "notebook_url": "http://notebooklm.google.com/notebook/1"}, "NotebookLM"),
    (lambda ep: {**ep, "notebook_url": "https://evil.example/notebook/1"}, "NotebookLM"),
    (lambda ep: {**ep, "notebook_url": "https://notebooklm.google.com/other/1"}, "NotebookLM"),
    (lambda ep: {**ep, "notebook_url": "https://notebooklm.google.com/notebook/1?x=1"}, "NotebookLM"),
    (lambda ep: {**ep, "notebook_url": "https://notebooklm.google.com/notebook/1#frag"}, "NotebookLM"),
])
def test_validate_episode_rejections(mutate, message):
    with pytest.raises(ValueError, match=message):
        pl.validate_episode(mutate(_episode()))


def test_validate_episode_allows_equal_starts_and_notebook_link():
    ep = _episode(transcript=[{"start": 0, "end": 5, "text": "a"}, {"start": 0, "end": 6, "text": "b"}],
                  notebook_url="https://notebooklm.google.com/notebook/abc")
    assert pl.validate_episode(ep) is ep


# ---- context_for -------------------------------------------------------------


@pytest.mark.parametrize("position,duration", [
    (-1, 200), (201, 200), (10, 0), (10, 86401), ("10", 200), (10, None), (True, 200),
])
def test_context_for_rejects_bad_playhead(position, duration):
    with pytest.raises(ValueError, match="Invalid podcast playhead"):
        pl.context_for(_episode(), position, duration)


def test_context_for_windows_recent_text_and_marks_upcoming():
    ctx = json.loads(pl.context_for(_episode(), 95, 200))
    assert ctx == {
        "audio_revision": REV, "paused_seconds": 95,
        # Only passages that finished in the last 60 s (35..95); the one playing at 95 is partial.
        "recent_transcript_approximate_alignment": "middle words",
        "current_passages_partial_alignment": [{"start": 90, "end": 150, "text": "late words",
            "heard_extent": "unknown within this segment; text may include unplayed words"}],
        "next_passage_not_yet_heard": "outro words",
        "source_chapter": "One", "authoritative_source": "source one",
        "editorial_corrections": "the host misspoke at 40s",
    }


def test_context_for_at_end_has_no_upcoming_and_nearest_chapter():
    ep = _episode(chapters=[{"start": 0, "end": 50, "title": "Early", "source": "s1"},
                            {"start": 120, "end": 150, "title": "Late", "source": "s2"}])
    ctx = json.loads(pl.context_for(ep, 200, 200))
    assert ctx["next_passage_not_yet_heard"] == ""
    assert ctx["source_chapter"] == "Late"        # no chapter contains 200; nearest start wins


def test_context_for_bounds_text_lengths():
    ep = _episode(
        transcript=[{"start": 0, "end": 10, "text": "r" * 2000}] * 4 + [{"start": 11, "end": 20, "text": "n" * 2000}],
        chapters=[{"start": 0, "end": 20, "title": "T", "source": "s" * 16000}],
        corrections="c" * 4000)
    ctx = json.loads(pl.context_for(ep, 10, 20))
    assert len(ctx["recent_transcript_approximate_alignment"]) == 6000
    assert len(ctx["next_passage_not_yet_heard"]) == 1000
    assert len(ctx["authoritative_source"]) == 12000
    assert len(ctx["editorial_corrections"]) == 4000


def test_context_for_linked_html_source_strips_markup_and_scripts():
    source = {"artifact_id": "art-2", "title": "Plan", "type": "html_form", "updated_at": "2026-01-01",
              "summary": "A plan",
              "payload": {"content": "<h1>Hello</h1><script>alert(1)</script><style>p{}</style><p>World</p>"},
              "plan": {"steps": ["a", "b"]}}
    ctx = json.loads(pl.context_for(_episode(), 10, 200, source=source))
    linked = ctx["linked_source"]
    assert linked["artifact_id"] == "art-2" and linked["title"] == "Plan"
    assert linked["updated_at"] == "2026-01-01"
    assert linked["excerpt_truncated"] is False
    assert "alert" not in linked["excerpt"] and "p{}" not in linked["excerpt"]
    assert linked["excerpt"].startswith("A plan\nHello World")
    assert json.dumps({"steps": ["a", "b"]}, ensure_ascii=False) in linked["excerpt"]
    assert linked["relationship"].startswith("Selected for this conversation")


def test_context_for_linked_plain_source_is_truncated_at_6000():
    source = {"artifact_id": "a", "title": "T", "type": "research", "payload": {"content": "z" * 7000}}
    linked = json.loads(pl.context_for(_episode(), 10, 200, source=source))["linked_source"]
    assert len(linked["excerpt"]) == 6000 and linked["excerpt_truncated"] is True
    assert linked["updated_at"] is None


# ---- session_config ----------------------------------------------------------


def test_session_config_overrides_instructions_and_seeds_reference_input():
    cfg = pl.session_config("CONTEXT-JSON", stable=True)
    assert cfg["instructions"] == pl.STABLE_PROMPT
    assert cfg["model"] == oracle_live_stable.MODEL
    assert cfg["audio"]["output"]["voice"] == oracle_live_stable.VOICE
    assert cfg["input"] == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "Reference data for the paused episode:\nCONTEXT-JSON"}]}]


def test_prompt_forbids_actions_and_greetings():
    text = pl.STABLE_PROMPT.lower()
    assert "no access to agents" in text
    assert "never claim to have resumed playback" in text
    assert "do not greet" in text


# ---- generate_image ----------------------------------------------------------

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class _Resp:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, n=-1):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def image_api(monkeypatch):
    state = {"requests": [], "image": _JPEG}

    def urlopen(request, timeout):
        state["requests"].append((request.full_url, json.loads(request.data), timeout))
        return _Resp(json.dumps({"data": [{"b64_json": base64.b64encode(state["image"]).decode()}]}).encode())

    monkeypatch.setattr(pl, "urlopen", urlopen)
    return state


def test_generate_image_approved_on_first_try(image_api):
    reviews = []

    def review(*, api_key, context, encoded):
        reviews.append((api_key, context, encoded))
        return {"approved": True, "reason": "fine"}

    plan = lambda *, api_key, context, question: "BRIEF: two boxes"
    encoded = pl.generate_image(api_key="k", context="CTX", question="why?", review=review, plan=plan)
    assert base64.b64decode(encoded) == _JPEG
    assert reviews == [("k", "CTX", encoded)]
    [(url, body, timeout)] = image_api["requests"]
    assert url == "https://api.openai.com/v1/images/generations" and timeout == 150
    assert body["model"] == "gpt-image-2" and body["size"] == "1024x1024"
    assert body["output_format"] == "jpeg" and body["n"] == 1
    assert body["prompt"].endswith("Follow this fact-checked drawing brief:\nBRIEF: two boxes")


def test_generate_image_retries_once_with_review_reason_then_fails(image_api):
    verdicts = iter([{"approved": False, "reason": "wrong arrow"}, {"approved": False, "reason": "still wrong"}])
    review = lambda **kw: next(verdicts)
    with pytest.raises(ValueError, match="did not pass accuracy review"):
        pl.generate_image(api_key="k", context="c", question="q", review=review, plan=lambda **kw: "b")
    prompts = [body["prompt"] for _, body, _ in image_api["requests"]]
    assert len(prompts) == 2
    assert "wrong arrow" not in prompts[0]
    assert prompts[1].endswith("The prior attempt failed accuracy review. Correct these issues: wrong arrow")


def test_generate_image_second_attempt_can_pass(image_api):
    verdicts = iter([{"approved": False, "reason": "r"}, {"approved": True, "reason": "ok"}])
    encoded = pl.generate_image(api_key="k", context="c", question="q",
                                review=lambda **kw: next(verdicts), plan=lambda **kw: "b")
    assert base64.b64decode(encoded) == _JPEG
    assert len(image_api["requests"]) == 2


def test_generate_image_rejects_non_jpeg(image_api):
    image_api["image"] = b"\x89PNG\r\n" + b"\x00" * 32
    with pytest.raises(ValueError, match="Invalid diagram image"):
        pl.generate_image(api_key="k", context="c", question="q",
                          review=lambda **kw: pytest.fail("no review of invalid image"),
                          plan=lambda **kw: "b")


def test_generate_image_cancellation_points(image_api):
    with pytest.raises(ValueError, match="Diagram cancelled"):
        pl.generate_image(api_key="k", context="c", question="q", plan=lambda **kw: pytest.fail("no plan"),
                          should_continue=lambda: False)
    assert image_api["requests"] == []
    # Cancelled after the brief but before the image call.
    flags = iter([True, False])
    with pytest.raises(ValueError, match="Diagram cancelled"):
        pl.generate_image(api_key="k", context="c", question="q", plan=lambda **kw: "b",
                          should_continue=lambda: next(flags))
    assert image_api["requests"] == []
    # Cancelled after the image but before review.
    flags = iter([True, True, False])
    with pytest.raises(ValueError, match="Diagram cancelled"):
        pl.generate_image(api_key="k", context="c", question="q", plan=lambda **kw: "b",
                          review=lambda **kw: pytest.fail("no review after cancel"),
                          should_continue=lambda: next(flags))
    assert len(image_api["requests"]) == 1


def test_review_and_brief_request_shapes(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append((request.full_url, json.loads(request.data), request.get_header("Authorization")))
        if len(requests) == 1:
            text = json.dumps({"approved": True, "reason": "clear"})
        else:
            text = "Draw two boxes."
        return _Resp(json.dumps({"output": [{"content": [{"type": "output_text", "text": text},
                                                          {"type": "reasoning", "text": "ignored"}]}]}).encode())

    monkeypatch.setattr(pl, "urlopen", urlopen)
    assert pl.review_image(api_key="k", context="C", encoded="AAAA") == {"approved": True, "reason": "clear"}
    assert pl.image_brief(api_key="k", context="C", question="q" * 2000) == "Draw two boxes."
    (url1, body1, auth1), (url2, body2, _) = requests
    assert url1 == url2 == "https://api.openai.com/v1/responses" and auth1 == "Bearer k"
    assert body1["text"]["format"]["schema"]["required"] == ["approved", "reason"]
    assert body1["input"][0]["content"][1] == {"type": "input_image", "image_url": "data:image/jpeg;base64,AAAA"}
    assert body2["input"].startswith("Listener question: " + "q" * 1500 + "\nReference data:\nC")


def test_review_rejects_malformed_verdict(monkeypatch):
    monkeypatch.setattr(pl, "urlopen", lambda r, timeout: _Resp(json.dumps(
        {"output": [{"content": [{"type": "output_text", "text": json.dumps({"approved": "yes", "reason": 1})}]}]}).encode()))
    with pytest.raises(ValueError, match="Invalid diagram review"):
        pl.review_image(api_key="k", context="C", encoded="AAAA")


@pytest.mark.parametrize("text", ["", "   ", "x" * 6001])
def test_brief_rejects_empty_or_oversized(monkeypatch, text):
    monkeypatch.setattr(pl, "urlopen", lambda r, timeout: _Resp(json.dumps(
        {"output": [{"content": [{"type": "output_text", "text": text}]}]}).encode()))
    with pytest.raises(ValueError, match="Diagram brief unavailable"):
        pl.image_brief(api_key="k", context="C", question="q")


# ---- PodcastConversation -----------------------------------------------------


@pytest.fixture
def podcast(monkeypatch):
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_decisions", lambda: [])
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_completion_notifications", lambda: [])
    sent, down, now = [], [], [1000.0]
    generated = []

    def generate(*, api_key, context, question, should_continue):
        generated.append((api_key, context, question, should_continue()))
        return "IMGB64"

    conv = pl.StablePodcastConversation(SimpleNamespace(send=sent.append), down.append, "key", "CTX",
                                  images=True, clock=lambda: now[0], generate=generate)
    conv.pool = SimpleNamespace(submit=lambda fn, *a: fn(*a), shutdown=lambda **kw: None)
    conv._sent, conv._down, conv._now, conv._generated = sent, down, now, generated
    yield conv
    conv.stop.set()


def _say(conv, text, advance=3.0):
    conv.receive({"type": "session.input_transcript.delta", "delta": text, "start_ms": 0, "end_ms": 100})
    conv._now[0] += advance


def test_podcast_tools_stub_has_no_agent_operations(podcast):
    assert podcast.tools.delegations == {} and podcast.tools.results() == []
    assert podcast.api_key == "key" and podcast.context == "CTX"


@pytest.mark.parametrize("phrase", ["Resume podcast", "resume the podcast!", "Continue the podcast",
                                    "back to the podcast."])
def test_resume_commands_emit_resume_and_no_image(podcast, phrase):
    _say(podcast, phrase)
    podcast.tick()
    assert [e["type"] for e in podcast._down if e["type"].startswith("podcast")] == ["podcast.resume"]
    assert podcast._generated == []
    assert podcast.processed_revision == podcast.revision


def test_question_triggers_one_image_with_pending_event(podcast):
    _say(podcast, "Why does the termite mound stay cool?")
    podcast.tick()
    types = [e["type"] for e in podcast._down if e["type"].startswith("podcast")]
    assert types == ["podcast.image_pending", "podcast.image"]
    pending = next(e for e in podcast._down if e["type"] == "podcast.image_pending")
    assert pending == {"type": "podcast.image_pending", "question": "Why does the termite mound stay cool?",
                       "revision": podcast.revision}
    image = next(e for e in podcast._down if e["type"] == "podcast.image")
    assert image == {"type": "podcast.image", "question": "Why does the termite mound stay cool?",
                     "revision": podcast.revision, "jpeg_base64": "IMGB64"}
    assert podcast._generated == [("key", "CTX", "Why does the termite mound stay cool?", True)]
    assert podcast.image_pending is False and podcast.image_count == 1
    podcast.tick()  # same revision: nothing new
    assert len(podcast._generated) == 1


def test_short_question_or_images_off_does_not_generate(podcast):
    _say(podcast, "Why?")
    podcast.tick()
    assert podcast._generated == [] and podcast.processed_revision == podcast.revision
    podcast.images = False
    _say(podcast, "A perfectly long enough question")
    podcast.tick()
    assert podcast._generated == []


def test_tick_waits_two_seconds_after_last_transcript(podcast):
    _say(podcast, "A perfectly long enough question", advance=1.0)
    podcast.tick()
    assert podcast._generated == [] and podcast.processed_revision == 0
    podcast._now[0] += 1.5
    podcast.tick()
    assert len(podcast._generated) == 1


def test_image_count_is_capped_at_six(podcast):
    for i in range(8):
        _say(podcast, f"Question number {i} about the episode")
        podcast.tick()
    assert podcast.image_count == 6 and len(podcast._generated) == 6


def test_user_text_resets_after_two_second_gap(podcast):
    _say(podcast, "first half ", advance=0.5)
    _say(podcast, "second half", advance=3.0)
    assert podcast.user_text == "first half second half"
    _say(podcast, "new question", advance=3.0)
    assert podcast.user_text == "new question"


def test_image_failure_is_reported_softly(podcast):
    def failing(**kw):
        raise RuntimeError("provider down")

    podcast.generate = failing
    _say(podcast, "A perfectly long enough question")
    podcast.tick()
    failed = [e for e in podcast._down if e["type"] == "podcast.image_failed"]
    assert failed == [{"type": "podcast.image_failed", "revision": podcast.revision,
                       "message": "The concept image could not be generated. Voice remains available."}]
    assert podcast.image_pending is False


def test_stale_image_is_dropped_when_revision_moved_on(podcast):
    def slow(*, api_key, context, question, should_continue):
        _say(podcast, "another long question meanwhile", advance=0)
        assert should_continue() is False
        return "LATE"

    podcast.generate = slow
    _say(podcast, "A perfectly long enough question")
    podcast.tick()
    assert not any(e["type"] == "podcast.image" for e in podcast._down)


def test_delegation_created_is_refused_with_commentary(podcast):
    podcast.receive({"type": "session.delegation.created", "delegation": {"id": "d1"}})
    [event] = [json.loads(raw) for raw in podcast._sent]
    assert event["type"] == "session.commentary.append"
    assert event["content"].startswith("You have no access to external actions in podcast mode.")
    assert podcast.routing == 0 and podcast.seen == set()


def test_eight_minute_cap_closes_session(podcast):
    podcast._now[0] += 8 * 60 + 1
    podcast.tick()
    assert podcast._down[-1] == {"type": "oracle_v2.notice",
                                 "message": "Companion paused after eight minutes. Tap Ask to reconnect."}
    assert json.loads(podcast._sent[-1]) == {"type": "session.close"}
    assert podcast.stop.is_set()


def test_history_recording_failure_stops_companion(podcast, monkeypatch):
    podcast.history_id = "hist-1"

    def record(ident, event):
        raise OSError("disk full")

    monkeypatch.setattr("lib.podcast_history.record", record)
    with pytest.raises(OSError):
        podcast.receive({"type": "session.started"})
    assert podcast._down[-1]["type"] == "podcast.history_error"
    assert podcast.stop.is_set()


def test_history_recording_annotates_events(podcast, monkeypatch):
    podcast.history_id = "hist-1"
    monkeypatch.setattr("lib.podcast_history.record", lambda ident, event: 55)
    podcast.receive({"type": "session.input_transcript.delta", "delta": "hello there", "start_ms": 0, "end_ms": 1})
    assert podcast.last_question_event_id == 55
    assert podcast._down[-1]["history_event_id"] == 55
    assert podcast._down[-1]["conversation_id"] == "hist-1"


def test_podcast_receives_from_the_shared_upstream_pump(podcast):
    """serve() feeds every conversation through pump_upstream, which names the socket."""
    frames = [json.dumps({"type": "session.output_transcript.delta", "delta": "Hello"})]
    podcast.upstream = SimpleNamespace(recv=lambda: frames.pop(0) if frames else "")
    oracle_live_stable.pump_upstream(podcast, TimeoutError)
    assert any(event.get("delta") == "Hello" for event in podcast._down)
