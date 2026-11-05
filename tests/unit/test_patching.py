"""tests/patching.py: patch_shared reaches a module's split family and nothing else."""
from __future__ import annotations

from lib import agent_rings, clock, heard_audio, message_live
from tests.patching import patch_shared, split_family


def _fake():
    return 42


def test_patching_now_ms_leaves_another_modules_now_ms_untouched(monkeypatch):
    # The shared clock: other modules' now_ms keep meaning what they mean today.
    before = (message_live.now_ms, clock.now_ms)
    patch_shared(monkeypatch, agent_rings, "now_ms", _fake)
    assert agent_rings.now_ms is _fake
    assert (message_live.now_ms, clock.now_ms) == before


def test_the_family_is_the_recipes_parts_only(monkeypatch, tmp_path):
    # A recipe names message_live as a part of agent_rings; heard_audio is not
    # in it. Both hold agent_rings' object; only the family member is patched.
    (tmp_path / "s9.toml").write_text(
        '[[split]]\nname = "x"\nmodule = "lib/agent_rings.py"\n'
        '[[split.part]]\nmodule = "lib/message_live.py"\nnames = ["now_ms"]\n')
    original = agent_rings.now_ms
    monkeypatch.setattr(message_live, "now_ms", original)
    monkeypatch.setattr(heard_audio, "now_ms", original)
    assert split_family(agent_rings, tmp_path) == [message_live]
    patch_shared(monkeypatch, agent_rings, "now_ms", _fake, recipes=tmp_path)
    assert agent_rings.now_ms is _fake and message_live.now_ms is _fake
    assert heard_audio.now_ms is original


def test_a_part_holding_another_object_is_left_alone(monkeypatch, tmp_path):
    (tmp_path / "s9.toml").write_text(
        '[[split]]\nname = "x"\nmodule = "lib/agent_rings.py"\n'
        '[[split.part]]\nmodule = "lib/message_live.py"\nnames = ["now_ms"]\n')
    own = message_live.now_ms
    patch_shared(monkeypatch, agent_rings, "now_ms", _fake, recipes=tmp_path)
    assert message_live.now_ms is own


def test_without_an_applied_split_it_is_monkeypatch_setattr(monkeypatch, tmp_path):
    (tmp_path / "s9.toml").write_text(
        '[[split]]\nname = "x"\nmodule = "lib/agent_rings.py"\n'
        '[[split.part]]\nmodule = "lib/agent_rings_not_there.py"\nnames = ["now_ms"]\n')
    assert split_family(agent_rings, tmp_path) == []
    patch_shared(monkeypatch, agent_rings, "now_ms", _fake, recipes=tmp_path)
    assert agent_rings.now_ms is _fake
