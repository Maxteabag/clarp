"""scripts/refactor/ast_identity.py, method mode (S3 mixin moves): a verbatim
move of Handler methods into a mixin passes; every rule fails on a planted
edit. Uses a throwaway git repository; the top-level mode has its own tests
(test_ast_identity.py)."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "refactor" / "ast_identity.py"
_SPEC = importlib.util.spec_from_file_location("ast_identity_methods_script", _SCRIPT)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)

HELPER = "VALUE = {'k': 1}\nOTHER = {'k': 1}\n"

SERVER = '''import json
from lib import helper
_CFG = {"delivery": "hls"}


class Base:
    pass


class Handler(Base):
    X = 1

    def a(self):
        return json.dumps(helper.VALUE)

    def b(self):
        from lib import helper as late
        return late.OTHER

    def c(self):
        return len([self])

    def d(self):
        return _CFG["delivery"]
'''

# The verbatim move of a, b and c into FeatRoutes.
SERVER_MOVED = '''import json
from lib import helper
from lib.routes.feat import FeatRoutes
_CFG = {"delivery": "hls"}


class Base:
    pass


class Handler(FeatRoutes, Base):
    X = 1

    def d(self):
        return _CFG["delivery"]
'''

FEAT = '''import json
from lib import helper


class FeatRoutes:
    def a(self):
        return json.dumps(helper.VALUE)

    def b(self):
        from lib import helper as late
        return late.OTHER

    def c(self):
        return len([self])
'''

MOVE = "server.py::Handler=lib/routes/feat.py::FeatRoutes"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, files: dict[str, str]) -> str:
    for path, text in files.items():
        target = repo / "server" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/helper.py": HELPER,
                              "lib/routes/__init__.py": "", "server.py": SERVER})
    return tmp_path, base


def _run(repo, base, server=SERVER_MOVED, feat=FEAT, move=MOVE):
    head = _commit(repo, {"server.py": server, "lib/routes/feat.py": feat})
    return tool.main(["--root", str(repo), base, head, move])


def test_a_verbatim_method_move_passes(repo, capsys):
    path, base = repo
    assert _run(path, base) == 0, capsys.readouterr().out
    assert "0 problems" in capsys.readouterr().out


def test_a_verbatim_move_with_a_retargeted_lazy_import_passes(repo, capsys):
    path, base = repo
    feat = FEAT.replace("from lib import helper as late", "from .. import helper as late")
    assert _run(path, base, feat=feat) == 0, capsys.readouterr().out


@pytest.mark.parametrize("server, feat, expected", [
    # a changed body
    (SERVER_MOVED, FEAT.replace("len([self])", "len([self, 1])"), "Handler.c differs"),
    # a missing method: moved out of Handler but not into the mixin
    (SERVER_MOVED, FEAT.replace("    def c(self):\n        return len([self])\n", ""),
     "Handler at HEAD differs from Handler at BASE beyond the moved members"),
    # an extra change to Handler
    (SERVER_MOVED.replace("X = 1", "X = 2"), FEAT,
     "Handler at HEAD differs from Handler at BASE beyond the moved members"),
    # a method that never was in Handler
    (SERVER_MOVED, FEAT + "\n    def e(self):\n        return 5\n",
     "FeatRoutes.e did not exist in Handler at BASE"),
    # a base other than the listed mixin
    (SERVER_MOVED.replace("class Handler(FeatRoutes, Base)", "class Handler(FeatRoutes, Base, object)"),
     FEAT, "expected the listed mixins first"),
    # original bases reordered or dropped
    (SERVER_MOVED.replace("class Handler(FeatRoutes, Base)", "class Handler(FeatRoutes)"),
     FEAT, "expected the listed mixins first"),
    # the mixin after an original base (it could be shadowed)
    (SERVER_MOVED.replace("class Handler(FeatRoutes, Base)", "class Handler(Base, FeatRoutes)"),
     FEAT, "expected the listed mixins first"),
    # the old file edited outside the class
    (SERVER_MOVED.replace('_CFG = {"delivery": "hls"}', '_CFG = {"delivery": "mp3"}'), FEAT,
     "server.py differs outside Handler"),
    # an import the retained old file still needs, dropped
    (SERVER_MOVED.replace("import json\nfrom lib import helper\nfrom lib.routes", "from lib.routes")
     .replace('return _CFG["delivery"]', 'return json.dumps(helper.VALUE)'), FEAT,
     "server.py uses"),
    # an unexplained new import in the old file and in the mixin
    (SERVER_MOVED.replace("import json\n", "import json\nimport os\n", 1), FEAT,
     "server.py adds an import of os"),
    (SERVER_MOVED, FEAT.replace("import json\n", "import json\nimport os\n", 1),
     "lib/routes/feat.py adds an import of os"),
    # compiler directives differ in the mixin file
    (SERVER_MOVED, "from __future__ import annotations\n" + FEAT, "compiler directives differ"),
    # a member defined twice in the mixin
    (SERVER_MOVED, FEAT + "\n    def a(self):\n        return json.dumps(helper.VALUE)\n",
     "a is defined twice"),
    # a decorator on Handler
    (SERVER_MOVED.replace("class Handler(", "@staticmethod\nclass Handler("), FEAT,
     "decorators or keywords changed"),
    # a NameError: the mixin module no longer binds a name a moved method reads
    (SERVER_MOVED, FEAT.replace("import json\n", ""), "lib/routes/feat.py uses json but no longer imports it"),
    # a builtin shadowed in the mixin module
    (SERVER_MOVED, FEAT.replace("import json\n", "import json\nlen = max\n"),
     "binds builtin len that c reads"),
    # zero-argument super() and a private name change meaning in a mixin
    (SERVER_MOVED, FEAT.replace("return len([self])", "return super().c()"), "Handler.c differs"),
])
def test_each_rule_fails_on_a_planted_edit(repo, capsys, server, feat, expected):
    path, base = repo
    assert _run(path, base, server=server, feat=feat) == 1
    assert expected in capsys.readouterr().out


def test_a_rebinding_to_a_different_object_fails(repo, capsys):
    """Same name, different object: the mixin binds `helper` to another module."""
    path, base = repo
    feat = FEAT.replace("from lib import helper\n", "from lib.routes import __init__ as helper\n")
    assert _run(path, base, feat=feat) == 1
    assert "lib/routes/feat.py: helper: not the same object" in capsys.readouterr().out


def test_a_composition_root_global_redefined_in_the_mixin_fails(repo, capsys):
    """Moving d, which reads server.py's _CFG: a look-alike _CFG in the mixin is
    another object, so the move fails until the rebinding commit removes the read."""
    path, base = repo
    server = SERVER_MOVED.replace(
        '    def d(self):\n        return _CFG["delivery"]\n', "    pass\n")
    feat = FEAT.replace("class FeatRoutes:", '_CFG = {"delivery": "hls"}\n\n\nclass FeatRoutes:') + \
        '\n    def d(self):\n        return _CFG["delivery"]\n'
    assert _run(path, base, server=server, feat=feat) == 1
    out = capsys.readouterr().out
    assert "_CFG (read by d): not the same object" in out


@pytest.mark.parametrize("body, expected", [
    ("return super().c()", "uses zero-argument super()"),
    ("return self.__secret", "uses private name __secret"),
])
def test_super_and_private_names_are_refused_even_when_unchanged(tmp_path, capsys, body, expected):
    """A verbatim move of a method that uses zero-argument super() or a private
    name still fails: its meaning depends on the defining class."""
    server = SERVER.replace("return len([self])", body)
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/helper.py": HELPER,
                              "lib/routes/__init__.py": "", "server.py": server})
    feat = FEAT.replace("return len([self])", body)
    assert _run(tmp_path, base, feat=feat) == 1
    assert expected in capsys.readouterr().out


def test_a_member_in_two_mixins_fails(repo, capsys):
    path, base = repo
    head = _commit(path, {
        "server.py": SERVER_MOVED.replace("class Handler(FeatRoutes, Base)",
                                          "class Handler(FeatRoutes, MoreRoutes, Base)").replace(
            "from lib.routes.feat import FeatRoutes",
            "from lib.routes.feat import FeatRoutes\nfrom lib.routes.more import MoreRoutes"),
        "lib/routes/feat.py": FEAT,
        "lib/routes/more.py": "import json\nfrom lib import helper\n\n\nclass MoreRoutes:\n"
                              "    def a(self):\n        return json.dumps(helper.VALUE)\n",
    })
    assert tool.main(["--root", str(path), base, head,
                      "server.py::Handler=lib/routes/feat.py::FeatRoutes,lib/routes/more.py::MoreRoutes"]) == 1
    assert "a is in both" in capsys.readouterr().out


def test_the_existing_cli_never_selects_method_mode_from_a_single_colon(repo, capsys):
    """A single ':' is not method syntax: the default (top-level) mode handles the
    MOVE exactly as before, so it fails on the unknown path instead."""
    path, base = repo
    head = _commit(path, {"server.py": SERVER_MOVED, "lib/routes/feat.py": FEAT})
    with pytest.raises(Exception):
        tool.main(["--root", str(path), base, head,
                   "server.py:Handler=lib/routes/feat.py:FeatRoutes"])


def test_a_moved_method_shadowed_at_head_fails(repo, capsys):
    """Resolution at HEAD: with the bases in order, a moved name must still
    resolve on Handler to the mixin's object. Here the first mixin inherits a
    class that defines c, which shadows FeatRoutes.c in the MRO."""
    path, base = repo
    more = ("import json\nfrom lib import helper\n\n\nclass Shadow:\n    def c(self):\n        return 0\n\n\n"
            "class MoreRoutes(Shadow):\n    def a(self):\n        return json.dumps(helper.VALUE)\n")
    feat = FEAT.replace("    def a(self):\n        return json.dumps(helper.VALUE)\n\n", "")
    server = SERVER_MOVED.replace("class Handler(FeatRoutes, Base)",
                                  "class Handler(MoreRoutes, FeatRoutes, Base)").replace(
        "from lib.routes.feat import FeatRoutes",
        "from lib.routes.feat import FeatRoutes\nfrom lib.routes.more import MoreRoutes")
    head = _commit(path, {"server.py": server, "lib/routes/feat.py": feat, "lib/routes/more.py": more})
    assert tool.main(["--root", str(path), base, head,
                      "server.py::Handler=lib/routes/more.py::MoreRoutes,lib/routes/feat.py::FeatRoutes"]) == 1
    assert "Handler.c resolves to FeatRoutes.c: not the same object" in capsys.readouterr().out


@pytest.mark.parametrize("member", [
    "    @helper\n    def moved(self):\n        return 1\n",
    "    def moved(self, value=helper):\n        return value\n",
    "    def moved(self) -> helper:\n        return 1\n",
    "    VALUE = helper\n",
])
def test_a_definition_time_read_the_mixin_does_not_bind_fails(tmp_path, capsys, member):
    """Review B (5fc04219, F3): names evaluated when the member is defined
    (decorator, default value, annotation, assigned value) count as reads."""
    helper = "def helper(value):\n    return value\n\n\n"
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/routes/__init__.py": "",
                              "server.py": helper + "class Handler:\n" + member +
                              "    def stays(self):\n        return 2\n"})
    head = _commit(tmp_path, {
        "server.py": helper + "from lib.routes.feat import FeatRoutes\n\n\nclass Handler(FeatRoutes):\n"
                     "    def stays(self):\n        return 2\n",
        "lib/routes/feat.py": "class FeatRoutes:\n" + member})
    assert tool.main(["--root", str(tmp_path), base, head,
                      "server.py::Handler=lib/routes/feat.py::FeatRoutes"]) == 1
    assert "reads helper, which lib/routes/feat.py does not bind" in capsys.readouterr().out


@pytest.mark.parametrize("mixin_value, expected", [
    ("", "reads VALUE, which lib/routes/feat.py does not bind"),
    ("VALUE = {'other': 2}\n\n\n", "VALUE (read by VALUE): not the same object"),
])
def test_a_class_attribute_that_reads_its_own_name_from_the_module_is_checked(
        tmp_path, capsys, mixin_value, expected):
    """Review B (7b9eb0a1): in a class body `VALUE = VALUE` reads the module's
    VALUE before binding the class attribute; the read must be accounted for."""
    _git(tmp_path, "init", "-q")
    module_value = "VALUE = {'k': 1}\n\n\n"
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/routes/__init__.py": "",
                              "server.py": module_value + "class Handler:\n    VALUE = VALUE\n\n"
                              "    def stays(self):\n        return 2\n"})
    head = _commit(tmp_path, {
        "server.py": module_value + "from lib.routes.feat import FeatRoutes\n\n\nclass Handler(FeatRoutes):\n"
                     "    def stays(self):\n        return 2\n",
        "lib/routes/feat.py": mixin_value + "class FeatRoutes:\n    VALUE = VALUE\n"})
    assert tool.main(["--root", str(tmp_path), base, head,
                      "server.py::Handler=lib/routes/feat.py::FeatRoutes"]) == 1
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("rebinding, reason", [
    ("json = {'kind': 'server-local'}\n", "also bound at module level"),
    ("from lib.helper import VALUE as json\n", "imported from 2 different places"),
    ("try:\n    import json\nexcept ImportError:\n    pass\n", "imported again inside a block"),
    ("def reset():\n    global json\n    json = None\n", "written through `global` in reset"),
    ("for json in []:\n    pass\n", "also bound at module level"),
    ("del json\nimport json\n", "also bound at module level"),
])
def test_a_moved_reader_of_a_rebound_import_fails(repo, capsys, rebinding, reason):
    """Review B (s3-tools 7f2bb6be, F4a): server.py imports json and also
    rebinds it, so the moved reader sees the module in the mixin but another
    object in server.py; top_imports alone reported 0 problems."""
    path, _ = repo
    rebound = SERVER.replace('_CFG = {"delivery": "hls"}\n', '_CFG = {"delivery": "hls"}\n' + rebinding)
    base = _commit(path, {"server.py": rebound})
    server = SERVER_MOVED.replace('_CFG = {"delivery": "hls"}\n', '_CFG = {"delivery": "hls"}\n' + rebinding)
    assert _run(path, base, server=server) == 1
    out = capsys.readouterr().out
    assert "a reads json, which server.py imports and rebinds" in out and reason in out, out


def test_a_rebinding_in_the_mixin_fails_too(repo, capsys):
    path, base = repo
    assert _run(path, base, feat=FEAT.replace("from lib import helper\n",
                                              "from lib import helper\njson = None\n")) == 1
    assert "a reads json, which lib/routes/feat.py imports and rebinds" in capsys.readouterr().out


@pytest.mark.parametrize("harmless", [
    "import os\nimport os.path\n",                       # one target, bound twice
    "def reader():\n    global json\n    return json\n",  # global read, no write
])
def test_harmless_double_bindings_are_not_rebinding(repo, capsys, harmless):
    path, _ = repo
    base = _commit(path, {"server.py": SERVER.replace("from lib import helper\n", "from lib import helper\n" + harmless)})
    server = SERVER_MOVED.replace("from lib import helper\n", "from lib import helper\n" + harmless)
    assert _run(path, base, server=server) == 0, capsys.readouterr().out
