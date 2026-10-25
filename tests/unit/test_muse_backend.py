"""Muse Code (``muse exec --json``) as a Clarp backend: the catalogue row.

The runner, transcript and session behaviour are pinned in
``test_muse_runner.py`` and ``test_muse_transcript.py``; this file covers what
the registry and the clients see. docs/architecture/muse-backend.md is the
contract.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import backends  # noqa: E402
from lib.backend import registry  # noqa: E402
from lib.backend.base import Unsupported  # noqa: E402
from lib.backend.stream_json import StreamJsonBackend  # noqa: E402
from lib.protocol import AgentBackend  # noqa: E402


def test_muse_is_a_registered_stream_json_backend():
    assert AgentBackend.MUSE == "muse"
    muse = registry.by_id("muse")
    assert muse.id == "muse"
    assert isinstance(muse, StreamJsonBackend)
    assert backends.ids()[-1] == "muse"


@pytest.mark.parametrize("spelling", ["muse", "MUSE", " muse ", "muse-code", "musecode"])
def test_spellings_normalise_to_muse(spelling):
    assert backends.normalize(spelling) == "muse"
    assert backends.is_valid(spelling) is True


def test_catalogue_row():
    muse = backends.by_id("muse")
    assert backends.label("muse") == "Muse Code"
    assert muse.required_binary == "muse"
    assert muse.runner == "muse"
    assert muse.efforts == ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    assert muse.config_model_field == "muse_model"
    assert muse.config_effort_field == "muse_effort"
    assert muse.fallback_models and all(m and label for m, label in muse.fallback_models)


def test_terminal_resumes_the_session_in_the_tui():
    assert backends.by_id("muse").terminal_argv("abc-123") == ["muse", "resume", "abc-123"]


def test_codex_only_protocols_are_unsupported():
    muse = backends.by_id("muse")
    with pytest.raises(Unsupported):
        muse.goal("a1", "status")
    assert muse.account_pool() == ""
    assert muse.recover_usage_limit("anything") is False
