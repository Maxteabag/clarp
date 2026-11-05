"""Moved out of server/lib/tts_providers.py by tools/refactor/split_apply.py (split 'tts-providers-audio-codec', recipe tools/refactor/splits/s7-voice.toml).
tts_providers.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

from pathlib import Path


def _wav_to_mp3(source: Path, destination: Path) -> None:
    import av
    from av.audio.resampler import AudioResampler
    destination.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(source)) as input_container:
        with av.open(str(destination), mode="w", format="mp3") as output_container:
            input_stream = input_container.streams.audio[0]
            output_stream = output_container.add_stream("mp3", rate=24_000)
            output_stream.layout = "mono"
            resampler = AudioResampler(
                format="fltp", layout="mono", rate=24_000)
            for frame in input_container.decode(input_stream):
                for converted in resampler.resample(frame):
                    converted.pts = None
                    for packet in output_stream.encode(converted):
                        output_container.mux(packet)
            for converted in resampler.resample(None):
                converted.pts = None
                for packet in output_stream.encode(converted):
                    output_container.mux(packet)
            for packet in output_stream.encode(None):
                output_container.mux(packet)
