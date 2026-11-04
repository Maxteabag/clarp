"""S7 step 1a, G0: what `_wav_to_mp3` does today, pinned before it moves to
lib/audio_codec.py (tts_providers keeps the name). Written against the old code."""
from __future__ import annotations

import math
import struct
import wave

import av
import pytest

from lib import tts_providers


def _wav(path, *, channels, rate, seconds, tone=440.0):
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        samples = []
        for n in range(frames):
            value = int(8000 * math.sin(2 * math.pi * tone * n / rate))
            samples.extend([value] * channels)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _probe(path):
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        decoded = sum(frame.samples for frame in container.decode(stream))
        return {
            "format": container.format.name,
            "codec": stream.codec_context.name,
            "rate": stream.codec_context.sample_rate,
            "channels": stream.codec_context.channels,
            "seconds": decoded / stream.codec_context.sample_rate,
        }


@pytest.mark.parametrize("channels,rate", [(1, 16_000), (2, 44_100), (1, 24_000), (2, 48_000)])
def test_any_pcm_wav_becomes_mono_24khz_mp3_of_the_same_length(tmp_path, channels, rate):
    source, target = tmp_path / "in.wav", tmp_path / "out.mp3"
    _wav(source, channels=channels, rate=rate, seconds=1.0)
    tts_providers._wav_to_mp3(source, target)
    probe = _probe(target)
    assert (probe["format"], probe["rate"], probe["channels"]) == ("mp3", 24_000, 1)
    assert probe["codec"].startswith("mp3")
    assert probe["seconds"] == pytest.approx(1.0, abs=0.12)


def test_the_destination_folder_is_created(tmp_path):
    source = tmp_path / "in.wav"
    target = tmp_path / "nested" / "deeper" / "out.mp3"
    _wav(source, channels=1, rate=16_000, seconds=0.25)
    tts_providers._wav_to_mp3(source, target)
    assert target.stat().st_size > 0


def test_an_existing_destination_is_overwritten(tmp_path):
    source, target = tmp_path / "in.wav", tmp_path / "out.mp3"
    target.write_bytes(b"stale" * 1000)
    _wav(source, channels=1, rate=16_000, seconds=0.5)
    tts_providers._wav_to_mp3(source, target)
    assert not target.read_bytes().startswith(b"stale")
    assert _probe(target)["seconds"] == pytest.approx(0.5, abs=0.12)


def test_a_file_that_is_not_audio_raises_and_leaves_no_usable_mp3(tmp_path):
    source, target = tmp_path / "in.wav", tmp_path / "out.mp3"
    source.write_bytes(b"not a wav file at all")
    with pytest.raises(Exception):
        tts_providers._wav_to_mp3(source, target)
