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


def test_live_items_get_a_short_muse_prefix():
    from lib import live_hub
    assert live_hub.item_prefix("muse") == "mu"


# --- models: Muse's own catalogue cache --------------------------------------

_CATALOG = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "muse" / "model-catalog.json"


def test_the_catalogue_cache_lists_visible_models_with_their_own_efforts():
    from lib import provider_capabilities as pc
    models = pc.parse_muse_models(_CATALOG.read_text(), observed_at="2026-10-25T00:00:00Z")
    assert [m["id"] for m in models] == [
        "muse-wonder-1.0", "muse-wonder-full-1.0", "muse-spark-1.3", "muse-spark-1.2"]
    by = {m["id"]: m for m in models}
    # Efforts come from each model's variants, in the backend's canonical order.
    assert by["muse-wonder-1.0"]["supported_efforts"] == ["minimal", "low", "medium", "high", "xhigh", "max"]
    assert "max" not in by["muse-spark-1.2"]["supported_efforts"]
    assert by["muse-wonder-1.0"]["default_effort"] == "high"
    assert all(m["source"]["kind"] == "cli_cache" for m in models)
    assert pc.parse_muse_models("not json", observed_at="x") == []


def test_discovery_reads_the_cache_and_falls_back_without_one(tmp_path, monkeypatch):
    from lib import provider_capabilities as pc
    import subprocess
    monkeypatch.setenv("CLAUDE_PWA_MUSE_HOME", str(tmp_path))
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "1.4.13-R6996.1\n", "")
    empty = pc._discover_provider("muse", "2026-10-25T00:00:00Z", resolve=lambda p: "/bin/muse", run=run)
    assert [m["id"] for m in empty["models"]] == ["muse-wonder-1.0"]  # the bundled fallback row
    cache = tmp_path / "model-catalog"
    cache.mkdir()
    (cache / "6d657461__p746268.json").write_text(_CATALOG.read_text())
    found = pc._discover_provider("muse", "2026-10-25T00:00:00Z", resolve=lambda p: "/bin/muse", run=run)
    assert [m["id"] for m in found["models"]][0] == "muse-wonder-1.0"
    assert len(found["models"]) == 4
    assert found["source"]["kind"] == "cli_cache"  # a cache, not a command that lists models
    assert backends.by_id("muse").effort_scope == "model"
