"""Muse Code (``muse exec --json``): Meta's coding CLI, one headless run per turn.

docs/architecture/muse-backend.md is the contract: the wire it reads, how a
session id is minted and continued, and where the transcript lives.
"""
from __future__ import annotations

from .. import muse_transcript
from .base import BackendBrand
from .stream_json import StreamJsonBackend


class MuseBackend(StreamJsonBackend):
    """Runs ``muse exec --json --session-id <id>`` once per turn."""
    # --- catalogue data ---------------------------------------------------
    id = 'muse'
    label = 'Muse Code'
    required_binary = 'muse'
    aliases = ('muse-code', 'musecode')
    badge = 'BackendMuse'
    detail = 'Runs on Meta Muse Code.'
    symbol = 'sparkles'
    brand = BackendBrand('#1c2b4a', '#0d1526', '#8fb4ff', '#2b4a8a')
    efforts = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
    runner = 'muse'
    config_model_field = 'muse_model'
    config_effort_field = 'muse_effort'
    model_family = 'muse'
    fallback_models = (
        ('muse-wonder-1.0', 'Muse Wonder 1.0'),
    )

    transcript = muse_transcript

    # --- interactive terminal ----------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        """``muse resume <id>``, or a fresh TUI when there is no session yet."""
        argv = [self.required_binary]
        if session_id:
            argv += ["resume", session_id]
        return argv
