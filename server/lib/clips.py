"""Sidecar metadata for audio clips.

Each `<ts>.mp3` (or `<ts>__<session>.mp3`) the system writes gets a paired
`<ts>.mp3.json` carrying the routing metadata: agent_id, persona, voice_id,
trace_id, source ('pwa'|'local'), bytes, text_len.

The sidecar carries the canonical agent_id plus full provenance, so no consumer
has to parse the filename. Deliveries write it before synthesis starts (no
`bytes` yet) and again on completion; `audio_growing` uses that to tell a
still-growing mp3 from a finished one.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any


def sidecar_path(mp3_path: pathlib.Path) -> pathlib.Path:
    return mp3_path.with_suffix(mp3_path.suffix + ".json")


def write_sidecar(mp3_path: pathlib.Path, *,
                  clip_id: int | None = None,
                  agent_id: str | None = None,
                  persona: str | None = None,
                  voice_id: str | None = None,
                  trace_id: str | None = None,
                  session: str | None = None,
                  source: str | None = None,
                  bytes_: int | None = None,
                  text_len: int | None = None,
                  extra: dict[str, Any] | None = None) -> None:
    """Write the JSON sidecar atomically. Never raises."""
    data: dict[str, Any] = {k: v for k, v in {
        "clip_id":      clip_id,
        "agent_id":     agent_id,
        "persona":      persona,
        "voice_id":     voice_id,
        "trace_id":     trace_id,
        "session": session,
        "source":       source,
        "bytes":        bytes_,
        "text_len":     text_len,
    }.items() if v is not None}
    if extra:
        data.update(extra)
    try:
        side = sidecar_path(mp3_path)
        tmp = side.with_suffix(side.suffix + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(side)
    except OSError:
        pass


def read_sidecar(mp3_path: pathlib.Path) -> dict[str, Any] | None:
    """Return the sidecar dict, or None if not present / unreadable."""
    try:
        return json.loads(sidecar_path(mp3_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
