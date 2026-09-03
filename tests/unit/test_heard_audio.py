"""Retained transcription audio: opt-in, traceable, bounded."""
from __future__ import annotations

import json

from lib import heard_audio


def test_nothing_is_kept_until_the_setting_is_on(tmp_path):
    assert heard_audio.retain(tmp_path, trace_id="a" * 16, audio_bytes=b"x",
                              content_type="audio/wav") is None
    assert not (tmp_path / "heard").exists()


def test_clip_and_sidecar_are_kept_and_found_by_trace(tmp_path):
    heard_audio.set_enabled(True)
    path = heard_audio.retain(
        tmp_path, trace_id="abcdef0123456789", audio_bytes=b"RIFF....",
        content_type="audio/wav; codecs=1", session="rachel", run_id=7,
        model="faster-whisper:small.en")
    assert path == tmp_path / "heard" / "abcdef0123456789.wav"
    found = heard_audio.lookup(tmp_path, "abcdef0123456789")
    assert found is not None
    kept_path, meta = found
    assert kept_path == path
    assert meta["session"] == "rachel" and meta["run_id"] == 7
    assert meta["content_type"].startswith("audio/wav")
    assert heard_audio.lookup(tmp_path, "0000000000000000") is None
    assert heard_audio.lookup(tmp_path, "../etc/passwd") is None


def test_bad_trace_ids_and_empty_audio_are_refused(tmp_path):
    heard_audio.set_enabled(True)
    assert heard_audio.retain(tmp_path, trace_id="../x", audio_bytes=b"x",
                              content_type="audio/wav") is None
    assert heard_audio.retain(tmp_path, trace_id="a" * 16, audio_bytes=b"",
                              content_type="audio/wav") is None


def test_sweep_keeps_the_newest_and_drops_the_old(tmp_path, monkeypatch):
    heard_audio.set_enabled(True)
    monkeypatch.setattr(heard_audio, "MAX_CLIPS", 2)
    root = tmp_path / "heard"
    root.mkdir()
    now = 1_800_000_000.0
    for i, age_days in enumerate((30, 3, 2, 1)):
        stem = f"{i:016x}"
        (root / f"{stem}.wav").write_bytes(b"x")
        side = root / f"{stem}.json"
        side.write_text(json.dumps({"trace_id": stem}))
        import os
        mtime = now - age_days * 86400
        os.utime(side, (mtime, mtime))
    removed = heard_audio.sweep(root, now=now)
    remaining = sorted(p.stem for p in root.glob("*.json"))
    assert remaining == [f"{2:016x}", f"{3:016x}"]
    assert removed == 4
