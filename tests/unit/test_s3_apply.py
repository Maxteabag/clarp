"""tools/refactor/s3_apply.py: the S3 move generator. A recipe applied to a
fixture server.py gives exactly the expected files, a second run is a no-op,
--check reports without writing, every refusal fires, and the generated move
passes ast_identity's method mode. Uses throwaway directories (and a throwaway
git repository for the gate)."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _REPO / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s3 = _load("s3_apply_under_test", "tools/refactor/s3_apply.py")
gate = _load("ast_identity_for_s3_tests", "scripts/refactor/ast_identity.py")

SERVER = '''from __future__ import annotations
import json
import os, sys
from lib import helper as helpers_mod
from lib.helper import VALUE, OTHER
_CFG = {"delivery": "hls"}


def _module_helper():
    return 1


class Base:
    pass


class Handler(Base):
    X = 1

    # Comment that belongs to a.
    def a(self):
        return json.dumps(VALUE)

    @staticmethod
    def b(value):
        return os.path.join(value, sys.prefix)

    def c(self):
        from lib import helper as late
        return late.OTHER

    def stays(self):
        return helpers_mod.VALUE
'''

RECIPE = '''[[group]]
name = "feat"
module = "lib/routes/feat.py"
class = "FeatRoutes"
members = ["a", "b", "c"]
'''

EXPECTED_MIXIN = '''"""FeatRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "feat"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
import os
import sys
from lib.helper import VALUE


class FeatRoutes:
    # Comment that belongs to a.
    def a(self):
        return json.dumps(VALUE)

    @staticmethod
    def b(value):
        return os.path.join(value, sys.prefix)

    def c(self):
        from lib import helper as late
        return late.OTHER
'''

EXPECTED_SERVER = '''from __future__ import annotations
import json
import os, sys
from lib import helper as helpers_mod
from lib.helper import VALUE, OTHER
_CFG = {"delivery": "hls"}


def _module_helper():
    return 1


class Base:
    pass


from lib.routes.feat import FeatRoutes  # noqa: E402  (s3_apply)


class Handler(FeatRoutes, Base):
    X = 1

    def stays(self):
        return helpers_mod.VALUE
'''


def _tree(tmp_path, server=SERVER, recipe=RECIPE, routes_package=True):
    (tmp_path / "server" / "lib").mkdir(parents=True)
    (tmp_path / "server" / "lib" / "__init__.py").write_text("")
    (tmp_path / "server" / "lib" / "helper.py").write_text("VALUE = {'k': 1}\nOTHER = {'k': 2}\n")
    if routes_package:
        (tmp_path / "server" / "lib" / "routes").mkdir()
        (tmp_path / "server" / "lib" / "routes" / "__init__.py").write_text("")
    (tmp_path / "server" / "server.py").write_text(server)
    (tmp_path / "recipe.toml").write_text(recipe)
    return tmp_path


def _run(root, *extra):
    return s3.main(["--root", str(root), "--recipe", str(root / "recipe.toml"), *extra])


def test_the_generated_files_are_byte_identical_to_the_expected_ones(tmp_path):
    root = _tree(tmp_path)
    assert _run(root, "--all") == 0
    assert (root / "server" / "lib" / "routes" / "feat.py").read_text() == EXPECTED_MIXIN
    assert (root / "server" / "server.py").read_text() == EXPECTED_SERVER


def test_a_second_run_is_a_no_op_and_check_reports_nothing(tmp_path, capsys):
    root = _tree(tmp_path)
    assert _run(root, "--all") == 0
    capsys.readouterr()
    assert _run(root, "--all") == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _run(root, "--check", "--all") == 0
    assert (root / "server" / "server.py").read_text() == EXPECTED_SERVER


def test_check_reports_pending_changes_without_writing(tmp_path, capsys):
    root = _tree(tmp_path)
    assert _run(root, "--check", "--all") == 1
    assert "would write server/server.py" in capsys.readouterr().out
    assert (root / "server" / "server.py").read_text() == SERVER
    assert not (root / "server" / "lib" / "routes" / "feat.py").exists()


def test_two_runs_on_two_copies_give_the_same_bytes(tmp_path):
    first, second = _tree(tmp_path / "one"), _tree(tmp_path / "two")
    assert _run(first, "--all") == 0 and _run(second, "--all") == 0
    for rel in ("server/server.py", "server/lib/routes/feat.py"):
        assert (first / rel).read_bytes() == (second / rel).read_bytes()


@pytest.mark.parametrize("server, recipe, routes_package, expected", [
    (SERVER, RECIPE.replace('"c"]', '"c", "gone"]'), True, "gone is not a member of Handler"),
    (SERVER.replace("return late.OTHER", "return _CFG"), RECIPE, True,
     "c: reads _CFG, which server.py defines"),
    (SERVER.replace("return late.OTHER", "return _module_helper()"), RECIPE, True,
     "c: reads _module_helper, which server.py defines"),
    (SERVER.replace("import json\n", "try:\n    import json\nexcept ImportError:\n    json = None\n"),
     RECIPE, True, "a: reads json, which server.py imports and also rebinds"),
    (SERVER.replace("return late.OTHER", "return super().c()"), RECIPE, True, "c: uses zero-argument super()"),
    (SERVER.replace("return late.OTHER", "return self.__secret"), RECIPE, True, "c: uses private name __secret"),
    (SERVER, RECIPE, False, "server/lib/routes/__init__.py is missing"),
    (SERVER, RECIPE + RECIPE.replace('name = "feat"', 'name = "more"').replace(
        "feat.py", "more.py").replace("FeatRoutes", "MoreRoutes"), True,
     "members listed in more than one place"),
    (SERVER, RECIPE.replace("lib/routes/feat.py", "lib/feat.py"), True,
     "module must be lib/routes/<name>.py"),
])
def test_each_refusal_fires_and_writes_nothing(tmp_path, capsys, server, recipe, routes_package, expected):
    root = _tree(tmp_path, server=server, recipe=recipe, routes_package=routes_package)
    assert _run(root, "--all") == 2
    assert expected in capsys.readouterr().out
    assert (root / "server" / "server.py").read_text() == server
    assert not (root / "server" / "lib" / "routes" / "feat.py").exists()


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_the_generated_move_passes_the_ast_identity_method_mode(tmp_path, capsys):
    root = _tree(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    assert _run(root, "--all") == 0
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "move")
    head = _git(root, "rev-parse", "HEAD")
    capsys.readouterr()
    assert gate.main(["--root", str(root), base, head,
                      "server.py::Handler=lib/routes/feat.py::FeatRoutes"]) == 0, capsys.readouterr().out


# ---- [helpers] step ----------------------------------------------------------

HELPER_SERVER = '''from __future__ import annotations
import json
_LIMIT = 3
_SEEN = {}


def _note(key):
    _SEEN[key] = _LIMIT
    return json.dumps(_SEEN)


def _plain():
    return 2


class Handler:
    def a(self):
        return _note("x") + str(_plain())
'''

HELPER_RECIPE = '[helpers]\nnames = ["_note", "_SEEN", "_LIMIT", "_plain"]\n'


def test_helpers_move_verbatim_with_import_back_and_pass_the_top_level_gate(tmp_path, capsys):
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=HELPER_RECIPE)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    assert _run(root, "--helpers") == 0
    helpers = (root / "server" / "lib" / "routes" / "helpers.py").read_text()
    server = (root / "server" / "server.py").read_text()
    assert "import json\n" in helpers and "def _note(key):\n    _SEEN[key] = _LIMIT" in helpers
    assert ("from lib.routes.helpers import _LIMIT, _SEEN, _note, _plain  # noqa: E402  (s3_apply)"
            in server)
    assert "def _note" not in server and "class Handler" in server
    capsys.readouterr()
    assert _run(root, "--helpers") == 0 and "nothing to do" in capsys.readouterr().out
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "helpers")
    head = _git(root, "rev-parse", "HEAD")
    assert gate.main(["--root", str(root), base, head,
                      "server.py=server.py,lib/routes/helpers.py"]) == 0, capsys.readouterr().out


def test_a_helper_reading_an_unlisted_server_definition_is_refused(tmp_path, capsys):
    root = _tree(tmp_path, server=HELPER_SERVER, recipe='[helpers]\nnames = ["_note"]\n')
    assert _run(root, "--helpers") == 2
    assert "_note reads _LIMIT, which server.py defines; list _LIMIT too" in capsys.readouterr().out


@pytest.mark.parametrize("where, text, expected", [
    ("server/server.py", "\n_SEEN = {}\n", "_SEEN is not a single plain module-level definition"),
    ("server/server.py", "\n\ndef reset():\n    global _LIMIT\n    _LIMIT = 9\n", "global write"),
    ("tests/test_x.py", "import server\nserver._plain = None\n", "assigns or deletes ._plain"),
    ("tests/test_x.py", "import server\ndel server._plain\n", "assigns or deletes ._plain"),
    ("tests/test_x.py", "def t(monkeypatch, server_module):\n"
                        "    monkeypatch.setattr(server_module, '_note', None)\n",
     "monkeypatch.setattr(server_module, '_note')"),
    ("tests/test_x.py", "def t(monkeypatch):\n    monkeypatch.setattr('server._LIMIT', 5)\n",
     "monkeypatch.setattr('server._LIMIT')"),
    ("tests/test_x.py", "from unittest import mock\nmock.patch('server._plain')\n",
     "mock.patch('server._plain')"),
    ("tests/test_x.py", "from unittest import mock\nimport server\nmock.patch.object(server, '_note')\n",
     "mock.patch.object(server, '_note')"),
    ("plugin/hook.py", "import server\nsetattr(server, '_SEEN', {})\n", "setattr(server, '_SEEN')"),
])
def test_every_rebinding_site_is_listed_and_refused(tmp_path, capsys, where, text, expected):
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=HELPER_RECIPE)
    target = root / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((target.read_text() if target.exists() else "") + text)
    assert _run(root, "--helpers") == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out and expected in out
    assert not (root / "server" / "lib" / "routes" / "helpers.py").exists()


def test_a_patch_retargeted_to_lib_routes_helpers_is_allowed(tmp_path):
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=HELPER_RECIPE)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(
        "from lib.routes import helpers\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(helpers, '_note', None)\n"
        "    monkeypatch.setattr('lib.routes.helpers._LIMIT', 5)\n")
    assert _run(root, "--helpers") == 0


def test_all_applies_helpers_first_and_groups_on_top(tmp_path):
    recipe = HELPER_RECIPE + ('\n[[group]]\nname = "feat"\nmodule = "lib/routes/feat.py"\n'
                              'class = "FeatRoutes"\nmembers = ["a"]\n')
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=recipe)
    assert _run(root, "--all") == 0
    feat = (root / "server" / "lib" / "routes" / "feat.py").read_text()
    assert "from lib.routes.helpers import _note, _plain" in feat
    assert "class Handler(FeatRoutes):\n" in (root / "server" / "server.py").read_text()
    assert not list(root.glob("*stage*"))


# ---- review B, 7f2bb6be ------------------------------------------------------

def test_check_never_touches_a_pre_existing_directory(tmp_path):
    """F3: the staging directory is the invocation's own, never a fixed path."""
    recipe = HELPER_RECIPE + ('\n[[group]]\nname = "feat"\nmodule = "lib/routes/feat.py"\n'
                              'class = "FeatRoutes"\nmembers = ["a"]\n')
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=recipe)
    keep = root / ".s3_apply_stage" / "unrelated-work.txt"
    keep.parent.mkdir()
    keep.write_text("mine")
    assert _run(root, "--check", "--all") == 1
    assert keep.read_text() == "mine"


@pytest.mark.parametrize("rebinding", [
    "json = {'kind': 'server-local'}\n",
    "import pathlib as json\n",
    "try:\n    import ujson as json\nexcept ImportError:\n    pass\n",
    "def reset():\n    global json\n    json = None\n",
    "try:\n    pass\nexcept ValueError as json:\n    pass\n",
    "(json := {'kind': 'local'})\n",
    "match 1:\n    case json:\n        pass\n",
    "if True:\n    def json():\n        return 1\n",
    "_x = [(json := 1) for _ in range(1)]\n",
    "class C:\n    def m(self):\n        def inner():\n            global json\n            json = 2\n",
])
def test_a_member_reading_a_rebound_import_is_refused(tmp_path, capsys, rebinding):
    """F4a: an import that server.py also rebinds has no single owner to copy."""
    server = SERVER.replace("_CFG = {", rebinding + "_CFG = {", 1)
    root = _tree(tmp_path, server=server)
    assert _run(root, "--all") == 2
    assert "a: reads json, which server.py imports and also rebinds" in capsys.readouterr().out


@pytest.mark.parametrize("text, refused", [
    ("import server as server_helpers\nserver_helpers._plain = None\n", True),
    ("import lib.routes.helpers as h\nh._plain = None\n", False),
    ("from lib.routes import helpers as h\nh._plain = None\n", False),
    ("import lib.routes.helpers\nlib.routes.helpers._plain = None\n", False),
    ("from unittest import mock\nmock.patch('my_helpers_server._plain')\n", True),
    ("from unittest import mock\nmock.patch('lib.routes.helpers._plain')\n", False),
    # review B, 40747475: scope and later bindings decide, not the spelling
    ("def unrelated():\n    from lib.routes import helpers as h\n\nimport server as h\nh._plain = None\n", True),
    ("from lib.routes import helpers as h\nimport server\nh = server\nh._plain = None\n", True),
    ("from lib.routes import helpers as h\ndef t(h):\n    h._plain = None\n", True),
    ("import server\nlib = server\nimport lib.routes.helpers\nlib.routes.helpers._plain = None\n", True),
    ("import server\ndef t(monkeypatch):\n    monkeypatch.setattr(server, name='_plain', value=None)\n", True),
    ("import server\ndef t(monkeypatch):\n    monkeypatch.setattr(target=server, name='_plain', value=None)\n", True),
    ("import server\ndef t(monkeypatch):\n    monkeypatch.delattr(server, name='_plain')\n", True),
    ("import server\nfrom unittest import mock\nmock.patch.object(server, attribute='_plain')\n", True),
    ("import server\nfrom unittest import mock\nmock.patch.multiple(server, _plain=None)\n", True),
    ("from unittest import mock\nmock.patch(target='server._plain')\n", True),
    ("from lib.routes import helpers\nfrom unittest import mock\nmock.patch.multiple(helpers, _plain=None)\n", False),
])
def test_only_a_positively_identified_helpers_target_is_exempt(tmp_path, capsys, text, refused):
    """F4b: the word 'helpers' in an alias or string is not proof of the target."""
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=HELPER_RECIPE)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(text)
    assert _run(root, "--helpers") == (2 if refused else 0), capsys.readouterr().out


@pytest.mark.parametrize("module", [
    "lib/routes/../escape.py", "lib/routes/sub/x.py", "lib/routes/helpers.py",
    "lib/routes/__init__.py", "lib/routes/registry.py", "lib/routes/bad-name.py", "/lib/routes/x.py",
])
def test_a_module_path_outside_lib_routes_is_refused_before_any_write(tmp_path, capsys, module):
    """F5: exactly lib/routes/<identifier>.py, not a reserved module."""
    root = _tree(tmp_path, recipe=RECIPE.replace("lib/routes/feat.py", module))
    assert _run(root, "--all") == 2
    assert "module must be lib/routes/<name>.py" in capsys.readouterr().out
    assert (root / "server" / "server.py").read_text() == SERVER


def test_dynamic_access_to_module_globals_is_refused(tmp_path, capsys):
    root = _tree(tmp_path, server=SERVER.replace("_CFG = {", "globals()['x'] = 1\n_CFG = {", 1))
    assert _run(root, "--all") == 2
    assert "dynamic access to module globals" in capsys.readouterr().out


@pytest.mark.parametrize("wildcard", [
    "from math import *\n", "if True:\n    from math import *\n",
])
def test_a_wildcard_import_in_server_py_is_refused_for_groups_and_helpers(tmp_path, capsys, wildcard):
    """Review B (52900070): `from math import *` shadows the builtin pow; a moved
    reader of pow would silently see the builtin in its new module."""
    root = _tree(tmp_path, server=SERVER.replace("_CFG = {", wildcard + "_CFG = {", 1))
    assert _run(root, "--all") == 2
    assert "wildcard import from math" in capsys.readouterr().out
    helpers = _tree(tmp_path / "h", server=HELPER_SERVER.replace("_LIMIT = 3", wildcard + "_LIMIT = 3", 1),
                    recipe=HELPER_RECIPE)
    assert _run(helpers, "--helpers") == 2
    assert "wildcard import from math" in capsys.readouterr().out


# ---- found by the first real application (dispatch helpers patched in tests) ----

@pytest.mark.parametrize("text", [
    "def t(monkeypatch, server_module):\n    monkeypatch.setattr(server_module, 'json', None)\n",
    "import server\nserver.json = None\n",
    "import importlib.util as u\nspec = u.spec_from_file_location('x', 'server/server.py')\n"
    "mod = u.module_from_spec(spec)\nmod.json = None\n",
    "from unittest import mock\nmock.patch('server.json')\n",
])
def test_a_group_member_whose_imported_read_is_patched_through_the_server_module_is_refused(
        tmp_path, capsys, text):
    """A test patching server.<imported name> stops reaching a member once it
    reads that name in its mixin module; refuse until the patch is retargeted."""
    root = _tree(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(text)
    assert _run(root, "--all") == 2
    out = capsys.readouterr().out
    assert "patched through the server module" in out and "tests/test_x.py" in out


def test_a_patch_of_the_same_name_on_another_module_is_not_a_server_patch(tmp_path):
    root = _tree(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(
        "from lib import helper\ndef t(monkeypatch):\n    monkeypatch.setattr(helper, 'json', None)\n")
    assert _run(root, "--all") == 0


def test_a_helper_whose_imported_read_is_patched_through_the_server_module_is_refused(tmp_path, capsys):
    root = _tree(tmp_path, server=HELPER_SERVER, recipe=HELPER_RECIPE)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("import server\nserver.json = None\n")
    assert _run(root, "--helpers") == 2
    assert "moved helpers read are patched through the server module" in capsys.readouterr().out


def test_the_patch_scan_covers_the_repository_when_helpers_and_groups_apply_together(tmp_path, capsys):
    recipe = HELPER_RECIPE + ('\n[[group]]\nname = "feat"\nmodule = "lib/routes/feat.py"\n'
                              'class = "FeatRoutes"\nmembers = ["a"]\n')
    server = HELPER_SERVER.replace('return _note("x") + str(_plain())',
                                   'return _note("x") + str(_plain()) + json.dumps(1)')
    root = _tree(tmp_path, server=server, recipe=recipe)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("import server\nserver.json = None\n")
    assert _run(root, "--all") == 2
    assert "patched through the server module" in capsys.readouterr().out


def test_an_acknowledged_patch_site_with_a_reason_is_allowed(tmp_path, capsys):
    ack = ('acknowledged_patches = [{path = "tests/test_x.py", name = "json", '
           'reason = "patches the server for a function that stays there", '
           'evidence = "trace: no frame of lib/routes/feat.py ran in that test"}]\n')
    root = _tree(tmp_path, recipe=RECIPE + ack)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("import server\nserver.json = None\n")
    assert _run(root, "--all") == 0


@pytest.mark.parametrize("ack", [
    'acknowledged_patches = [{path = "tests/test_x.py", name = "json", evidence = "e"}]\n',
    'acknowledged_patches = [{path = "tests/test_x.py", name = "json", reason = " ", evidence = "e"}]\n',
    'acknowledged_patches = [{path = "tests/test_x.py", name = "json", reason = "r"}]\n',
    'acknowledged_patches = [{path = "tests/test_x.py", name = "json", reason = "r", evidence = " "}]\n',
    'acknowledged_patches = [{path = "tests/other.py", name = "json", reason = "wrong file", evidence = "e"}]\n',
    'acknowledged_patches = [{path = "tests/test_x.py", name = "os", reason = "wrong name", evidence = "e"}]\n',
])
def test_an_acknowledgement_needs_reason_and_evidence_and_must_match_path_and_name(tmp_path, capsys, ack):
    root = _tree(tmp_path, recipe=RECIPE + ack)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("import server\nserver.json = None\n")
    assert _run(root, "--all") == 2


# ---- review B, 858211b3 ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "from unittest import mock\nmock.patch.multiple('server', json=None)\n",
    "from unittest import mock\nmock.patch.multiple(target='server', json=None)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr('server.json', 'a string value')\n",
])
def test_string_server_targets_are_judged_by_their_string(tmp_path, capsys, text):
    root = _tree(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(text)
    assert _run(root, "--all") == 2
    assert "patched through the server module" in capsys.readouterr().out


def test_an_acknowledgement_matches_the_patched_name_not_the_file_name(tmp_path, capsys):
    """An acknowledgement for json in tests/json.py must not waive a patch of
    another name in that file."""
    ack = ('acknowledged_patches = [{path = "tests/json.py", name = "json", reason = "r", '
           'evidence = "e"}]\n')
    server = SERVER.replace("return json.dumps(VALUE)", "return json.dumps(VALUE) + str(OTHER)")
    root = _tree(tmp_path, server=server, recipe=RECIPE + ack)
    (root / "tests").mkdir()
    (root / "tests" / "json.py").write_text("import server\nserver.OTHER = None\n")
    assert _run(root, "--all") == 2
    assert "tests/json.py:2: assigns or deletes .OTHER" in capsys.readouterr().out
