"""tools/refactor/split_apply.py: S9 module splits as recipe data plus a generator."""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import textwrap

import pytest

_TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "refactor" / "split_apply.py"
_spec = importlib.util.spec_from_file_location("split_apply_under_test", _TOOL)
split_apply = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(split_apply)
Refusal = split_apply.Refusal


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    # The rebinding scan lists files with `git ls-files`; these trees are not
    # repositories, and a local gate may not start subprocesses.
    monkeypatch.setattr(split_apply, "_tracked_python",
                        lambda root: sorted(p for p in pathlib.Path(root).rglob("*.py")))

OLD = textwrap.dedent('''\
    """An old module."""
    from __future__ import annotations

    import json
    import time

    from .log import log

    LIMIT = 3
    A = "a"
    B = "b"
    C, D = 1, 2
    HERE = __file__


    def helper(x):
        return json.dumps([x, LIMIT])


    # A comment that travels with its class.
    class Store:
        def save(self, x):
            return helper(x) + A


    def run(x):
        log("run", Store().save(x))
        return time.time()
''')


def _tree(tmp_path, old=OLD, extra=None):
    lib = tmp_path / "server" / "lib"
    lib.mkdir(parents=True)
    (lib / "old.py").write_text(old)
    for rel, text in (extra or {}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _split(parts, **kw):
    return {"name": "t", "module": "lib/old.py", "recipe": "tools/refactor/splits/t.toml",
            "part": [{"module": m, "names": n} for m, n in parts], **kw}


def _write(root, changes):
    for rel, text in changes.items():
        (root / rel).write_text(text)


def test_a_split_moves_definitions_verbatim_and_leaves_a_facade(tmp_path):
    root = _tree(tmp_path)
    split = _split([("lib/old_base.py", ["LIMIT", "A", "B", "helper"]), ("lib/old_store.py", ["Store"])])
    changes = split_apply.apply_split(root, split)
    base, store, old = (changes["server/lib/old_base.py"], changes["server/lib/old_store.py"],
                        changes["server/lib/old.py"])
    assert 'A = "a"\nB = "b"\n' in base and "def helper(x):" in base and "import json" in base
    assert "import time" not in base and "from __future__ import annotations" in base
    assert "# A comment that travels with its class." in store
    assert "from .old_base import A, helper" in store
    for text in (base, store, old):
        ast.parse(text)
    assert "def helper" not in old and "class Store" not in old and "def run(x):" in old
    assert "from .old_base import (" in old and "from .old_store import (" in old
    _write(root, changes)
    assert split_apply.apply_split(root, split) == {}       # applied: a no-op


def test_partly_applied_is_refused(tmp_path):
    root = _tree(tmp_path, extra={"server/lib/old_base.py": "LIMIT = 3\n"})
    with pytest.raises(Refusal, match="partly applied"):
        split_apply.apply_split(root, _split([("lib/old_base.py", ["LIMIT"])]))


@pytest.mark.parametrize("parts,message", [
    ([("lib/old_store.py", ["Store"])], "which stays in lib/old.py"),
    ([("lib/old_store.py", ["Store"]), ("lib/old_base.py", ["helper", "LIMIT", "A", "B"])], "later part"),
    ([("lib/old_base.py", ["C"])], "assigned together"),
    ([("lib/old_base.py", ["HERE"])], "reads __file__"),
    ([("lib/old_base.py", ["missing"])], "not a single plain top-level definition"),
])
def test_unprovable_moves_are_refused(tmp_path, parts, message):
    root = _tree(tmp_path)
    with pytest.raises(Refusal, match=message):
        split_apply.apply_split(root, _split(parts))


def test_a_patch_through_the_old_module_of_a_name_moved_code_reads_is_refused(tmp_path):
    patch = "from lib import old\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(old, 'helper', None)\n"
    root = _tree(tmp_path, extra={"tests/test_old.py": patch})
    split = _split([("lib/old_base.py", ["LIMIT", "A", "B", "helper"]), ("lib/old_store.py", ["Store"])])
    with pytest.raises(Refusal, match="test_old.py:4"):
        split_apply.apply_split(root, split)
    acknowledged = {"acknowledged_patches": [{"path": "tests/test_old.py", "name": "helper",
                                              "reason": "the test never reaches Store", "evidence": "trace"}]}
    assert split_apply.apply_split(root, {**split, **acknowledged})


def test_a_patch_of_a_name_only_staying_code_reads_still_works_and_passes(tmp_path):
    # run() stays and reads Store through the facade, so patching old.Store still reaches it.
    patch = "from lib import old\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(old, 'Store', None)\n"
    root = _tree(tmp_path, extra={"tests/test_old.py": patch})
    split = _split([("lib/old_base.py", ["LIMIT", "A", "B", "helper"]), ("lib/old_store.py", ["Store"])])
    assert split_apply.apply_split(root, split)


def test_a_part_outside_the_old_modules_directory_is_a_bad_recipe(tmp_path):
    (tmp_path / "t.toml").write_text(
        '[[split]]\nname = "t"\nmodule = "lib/old.py"\n[[split.part]]\nmodule = "lib/x/new.py"\nnames = ["A"]\n')
    with pytest.raises(Refusal, match="same|new .py file"):
        split_apply.load_recipes(tmp_path)


def test_lanes_cannot_claim_the_same_name(tmp_path):
    entry = '[[split]]\nname = "{n}"\nmodule = "lib/old.py"\n[[split.part]]\nmodule = "lib/{m}.py"\nnames = ["LIMIT"]\n'
    (tmp_path / "s7-voice.toml").write_text(entry.format(n="a", m="one"))
    (tmp_path / "s9-x.toml").write_text(entry.format(n="b", m="two"))
    with pytest.raises(Refusal, match="listed by 'a' and 'b'"):
        split_apply.load_recipes(tmp_path)


def test_a_repoint_rewrites_only_the_named_import_and_is_idempotent(tmp_path):
    user = ("def f():\n    from .tts_providers import _wav_to_mp3\n    return _wav_to_mp3\n\n"
            "def g():\n    from .tts_providers import other\n    return other\n")
    root = _tree(tmp_path, extra={"server/lib/user.py": user})
    entry = {"name": "r", "file": "lib/user.py", "from_module": ".tts_providers",
             "to_module": ".audio_codec", "names": ["_wav_to_mp3"]}
    changes = split_apply.apply_repoint(root, entry)
    text = changes["server/lib/user.py"]
    assert "from .audio_codec import _wav_to_mp3" in text and "from .tts_providers import other" in text
    _write(root, changes)
    assert split_apply.apply_repoint(root, entry) == {}


def test_the_shipped_recipes_load():
    splits, repoints = split_apply.load_recipes(_TOOL.parent / "splits")
    assert any(s["module"] == "lib/agent_live_tools.py" for s in splits)


def test_a_repoint_of_server_py_or_an_s3_mixin_is_a_bad_recipe(tmp_path):
    for target in ("server.py", "lib/routes/vocab.py"):
        (tmp_path / "t.toml").write_text(
            f'[[repoint]]\nname = "r"\nfile = "{target}"\nfrom_module = ".a"\nto_module = ".b"\nnames = ["x"]\n')
        with pytest.raises(Refusal, match="S3 stages"):
            split_apply.load_recipes(tmp_path)


def test_another_spelling_of_a_protected_repoint_target_is_refused(tmp_path):
    # Review B F1: "./server.py" named server.py and passed the literal check.
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.py").write_text("from lib.old import f\n")
    for target in ("./server.py", "lib/../server.py", "lib//routes/x.py", "/abs.py"):
        recipes = tmp_path / "recipes"
        recipes.mkdir(exist_ok=True)
        (recipes / "t.toml").write_text(
            f'[[repoint]]\nname = "r"\nfile = "{target}"\nfrom_module = "lib.old"\nto_module = "lib.new"\nnames = ["f"]\n')
        assert split_apply.main(["--root", str(tmp_path), "--recipes", str(recipes), "--all"]) == 2
    assert (tmp_path / "server" / "server.py").read_text() == "from lib.old import f\n"


def test_a_patch_of_a_recursive_function_through_the_old_module_is_refused(tmp_path):
    # Review B F2: f's call to itself is a global lookup; after the move a patch
    # of old.f would no longer reach it.
    old = "def f(n):\n    return 0 if n == 0 else f(n - 1) + 1\n"
    patch = "from lib import old\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(old, 'f', lambda n: 100)\n"
    root = _tree(tmp_path, old=old, extra={"tests/test_old.py": patch})
    with pytest.raises(Refusal, match="test_old.py:4"):
        split_apply.apply_split(root, _split([("lib/old_part.py", ["f"])]))


def test_a_patch_through_the_old_module_with_a_computed_name_is_refused(tmp_path):
    # Review of cut 1b: `for name, value in ...: monkeypatch.setattr(old, name, value)`
    # cannot be checked name by name, so it fails closed.
    patch = ("from lib import old\n\ndef test_x(monkeypatch):\n"
             "    for name in ('helper',):\n        monkeypatch.setattr(old, name, None)\n")
    root = _tree(tmp_path, extra={"tests/test_old.py": patch})
    split = _split([("lib/old_base.py", ["LIMIT", "A", "B", "helper"]), ("lib/old_store.py", ["Store"])])
    with pytest.raises(Refusal, match="computed name"):
        split_apply.apply_split(root, split)


def test_a_moved_definition_that_rebinds_a_staying_global_is_refused(tmp_path):
    # Cut 1c trial G1: set_advance_request_callback moved and its `global`
    # write split the state from the readers left in the old module.
    old = ("_CALLBACK = None\n\n\ndef set_callback(cb):\n    global _CALLBACK\n    _CALLBACK = cb\n\n\n"
           "def call():\n    return _CALLBACK and _CALLBACK()\n")
    root = _tree(tmp_path, old=old)
    with pytest.raises(Refusal, match="rebinds module global"):
        split_apply.apply_split(root, _split([("lib/old_part.py", ["set_callback"])]))
    # The global itself cannot move either: its `global` write is a second binding.
    with pytest.raises(Refusal, match="not a single plain top-level definition"):
        split_apply.apply_split(root, _split([("lib/old_part.py", ["_CALLBACK", "set_callback", "call"])]))
