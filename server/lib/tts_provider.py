"""Provider boundary for a clip and the sink receiving its bytes."""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class AudioSink:
    out_path: pathlib.Path
    on_chunk: Callable[[int, bytes], None] | None = None
    trace_id: str | None = None
    delivery_fields: dict | None = None


class TTSProvider(Protocol):
    id: str
    voice: str | None

    @property
    def available(self) -> bool: ...

    def synthesize(self, text: str, sink: AudioSink) -> int: ...
