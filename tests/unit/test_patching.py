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


def test_logical_trees_move_only_exact_named_definitions(tmp_path):
    import ast
    from tests.patching import logical_split_trees
    root = tmp_path / "server"
    (root / "lib").mkdir(parents=True)
    recipes = tmp_path / "splits"
    recipes.mkdir()
    (recipes / "quota.toml").write_text(
        '[[split]]\nname="quota"\nmodule="lib/quota.py"\n'
        '[[split.part]]\nmodule="lib/part.py"\nnames=["CODEX", "moved", "Handle", "FLAG"]\n')
    owner = root / "lib/quota.py"
    owner.write_text("from .part import CODEX, moved, Handle, FLAG\n")
    part = root / "lib/part.py"
    part.write_text('CODEX="codex"\nFLAG: bool = True\ndef moved(): pass\n'
                    'class Handle: pass\ndef new(): pass\nother=1\na,b=2,3\nregister()\n')
    trees = logical_split_trees([owner, part], root, recipes)
    assert {getattr(n, "name", None) for n in trees["lib/quota.py"].body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))} == {"moved", "Handle"}
    actual = trees["lib/part.py"]
    assert [n.name for n in actual.body if isinstance(n, ast.FunctionDef)] == ["new"]
    assert any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) for n in actual.body)
    assert [n.target.id for n in trees["lib/quota.py"].body if isinstance(n, ast.AnnAssign)] == ["FLAG"]
    assert len([n for n in actual.body if isinstance(n, ast.Assign)]) == 2
    # Without a recipe, even a generated-looking file remains a physical owner.
    unsplit = logical_split_trees([owner, part], root, tmp_path / "missing")
    assert [n.name for n in unsplit["lib/part.py"].body if isinstance(n, ast.FunctionDef)] == ["moved", "new"]


def test_patch_audit_still_rejects_direct_rebinding_but_accepts_patch_shared(tmp_path, monkeypatch):
    import pathlib
    import subprocess
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).resolve().parents[2] / "tools/refactor"))
    from split_apply import old_module_rebinds
    (tmp_path / "case.py").write_text(
        'from lib import agent_rings\n'
        'def case(monkeypatch, fake):\n'
        '    patch_shared(monkeypatch, agent_rings, "now_ms", fake)\n'
        '    monkeypatch.setattr(agent_rings, "now_ms", fake)\n')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "case.py"], check=True)
    sites = old_module_rebinds(tmp_path, "lib/agent_rings.py", {"now_ms"}, set())
    assert [(path, line, name) for path, line, name, _ in sites] == [("case.py", 4, "now_ms")]
