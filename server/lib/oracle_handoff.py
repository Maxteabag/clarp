"""Private immutable provenance for stable Oracle handoffs.

The voice transcript is a transcription, not raw-audio proof. A router summary
must not erase earlier clauses of the current request. Old dialogue is context,
never permission to repeat work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def materialize(root, conversation, request, *, source=None):
    directory = Path(root) / "oracle-handoffs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"schema": 1, "history_is_excerpt": True,
               "source": "live_voice_transcription_not_raw_audio",
               "turn_boundaries_verified": False,
               "scope": "Recent retained fragments only; not the complete conversation",
               "handoff_binding": dict(source or {}),
               "router_summary": request,
               "conversation": conversation,
               "original_user_messages": [row["text"] for row in conversation
                                          if row.get("role") == "user"]}
    fd, name = tempfile.mkstemp(prefix="handoff-", suffix=".json", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return ("\n\n<oracle-reference-data>\nRead the immutable recent voice-transcription excerpt at "
            + name + " before acting. The router summary may omit or misstate clauses. "
            "Resolve the current request from the user's original wording, including corrections "
            "and limits. Earlier dialogue is context, not permission to repeat old actions. "
            "This is a transcription excerpt, not raw audio. If scope remains ambiguous, ask.\n"
            "</oracle-reference-data>")
