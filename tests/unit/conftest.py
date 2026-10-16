"""Unit-test session setup that must happen before any ``lib`` import."""
from __future__ import annotations

import os

import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    """Custom TTS adapters live in ``CLARP_TTS_ADAPTERS_DIR``, read once when
    ``lib.custom_tts_adapters`` is first imported. Point it at an empty
    directory of this session before any test module imports it, so no
    installed (live) adapter is ever discovered or run; a test that needs
    adapters installs its own and points ``ROOT`` at them."""
    empty = config._tmp_path_factory.getbasetemp() / "tts-adapters-empty"
    empty.mkdir(exist_ok=True)
    os.environ["CLARP_TTS_ADAPTERS_DIR"] = str(empty)
