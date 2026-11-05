"""scripts/refactor/ast_identity.py: the S1 move-only gate passes a verbatim move
and fails every kind of edit hiding in one. Uses a throwaway git repository."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "refactor" / "ast_identity.py"
_SPEC = importlib.util.spec_from_file_location("ast_identity_script", _SCRIPT)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)

FEATURE = 'SCHEMA = "CREATE TABLE t (x)"\n\ndef use():\n    return SCHEMA\n'
OLD = ('from .feature import SCHEMA as _FEATURE_SCHEMA\n'
       '_SQL = "A;"\n'
       '_SQL += _FEATURE_SCHEMA\n'
       '_SQL += "B;"\n\n'
       'def build(con):\n    return con.execute(_SQL)\n\n'
       'class Store:\n    size = 2\n')


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, files: dict[str, str | None]) -> str:
    for path, text in files.items():
        target = repo / "server" / path
        if text is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/feature.py": FEATURE, "lib/old.py": OLD})
    return tmp_path, base


def _move(repo, new_text, part_text='SCHEMA = "CREATE TABLE t (x)"\n',
          feature='from .persistence.parts import SCHEMA\n\ndef use():\n    return SCHEMA\n'):
    return _commit(repo, {
        "lib/persistence/__init__.py": "",
        "lib/persistence/parts.py": part_text,
        "lib/feature.py": feature,
        "lib/persistence/new.py": new_text,
        "lib/old.py": "import sys\nfrom .persistence import new as _new\nsys.modules[__name__] = _new\n",
    })


MOVED = OLD.replace("from .feature import SCHEMA", "from .parts import SCHEMA")
MOVES = ["lib/old.py=lib/persistence/new.py", "lib/feature.py=lib/feature.py,lib/persistence/parts.py"]


def _run(path, base, head):
    return tool.main(["--root", str(path), base, head, *MOVES])


def test_a_verbatim_move_with_retargeted_imports_passes(repo, capsys):
    path, base = repo
    head = _move(path, MOVED)
    assert _run(path, base, head) == 0, capsys.readouterr().out
    assert "2 changed imports, 0 problems" in capsys.readouterr().out  # old.py's import and feature.SCHEMA


@pytest.mark.parametrize("edit,expected", [
    (lambda t: t.replace("return con.execute(_SQL)", "return con.executescript(_SQL)"), "build differs"),
    (lambda t: t.replace("def build(", "def build_schema("), "build is missing"),
    (lambda t: t.replace("size = 2", "size = 3"), "Store differs"),
    (lambda t: t.replace('_SQL += _FEATURE_SCHEMA\n_SQL += "B;"\n', '_SQL += "B;"\n_SQL += _FEATURE_SCHEMA\n'),
     "statement order or content differs"),
])
def test_any_edit_inside_a_move_fails(repo, capsys, edit, expected):
    path, base = repo
    head = _move(path, edit(MOVED))
    assert _run(path, base, head) == 1
    assert expected in capsys.readouterr().out


def test_an_import_bound_to_a_different_object_fails(repo, capsys):
    path, base = repo
    # The feature keeps its own SCHEMA and the fragment holds another value: a
    # duplicated, not moved, definition, and old.py now binds a different object.
    head = _move(path, MOVED, part_text='SCHEMA = "CREATE TABLE t (y)"\n',
                 feature='SCHEMA = "CREATE TABLE t (x)"\n\ndef use():\n    return SCHEMA\n')
    assert _run(path, base, head) == 1
    out = capsys.readouterr().out
    assert "lib/persistence/new.py: _FEATURE_SCHEMA: not the same object" in out
    assert "SCHEMA is defined in both lib/feature.py and lib/persistence/parts.py" in out


def _scoped_repo(tmp_path, old_text, files):
    """Base: lib/old.py plus helper modules; head: old.py moved to lib/pkg/new.py."""
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/old.py": old_text, **files})
    return base


def _scoped_head(tmp_path, new_text, extra=None):
    return _commit(tmp_path, {"lib/pkg/__init__.py": "", "lib/pkg/new.py": new_text,
                              "lib/old.py": None, **(extra or {})})


def test_a_dropped_required_module_import_fails(tmp_path, capsys):
    base = _scoped_repo(tmp_path, "from .dependency import VALUE\n\ndef read():\n    return VALUE\n",
                        {"lib/dependency.py": "VALUE = object()\n"})
    head = _scoped_head(tmp_path, "def read():\n    return VALUE\n")
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/pkg/new.py"]) == 1
    assert "uses VALUE but no longer imports it" in capsys.readouterr().out


def test_same_spelled_lazy_imports_are_checked_one_by_one(tmp_path, capsys):
    old = ("def first():\n    from .one import VALUE\n    return VALUE\n\n"
           "def second():\n    from .two import VALUE\n    return VALUE\n")
    base = _scoped_repo(tmp_path, old, {"lib/one.py": "VALUE = object()\n", "lib/two.py": "VALUE = object()\n"})
    # .one inside pkg is a different object; .two re-exports the original.
    head = _scoped_head(tmp_path, old, {"lib/pkg/one.py": "VALUE = object()\n",
                                        "lib/pkg/two.py": "from ..two import VALUE\n"})
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/pkg/new.py"]) == 1
    out = capsys.readouterr().out
    assert "import first: VALUE: not the same object" in out
    assert "second" not in out


def test_a_lazy_relative_import_retargeted_one_package_deeper_passes(tmp_path, capsys):
    base = _scoped_repo(tmp_path, "def read():\n    from .dependency import VALUE\n    return VALUE\n",
                        {"lib/dependency.py": "VALUE = object()\n"})
    head = _scoped_head(tmp_path, "def read():\n    from ..dependency import VALUE\n    return VALUE\n")
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/pkg/new.py"]) == 0, capsys.readouterr().out


def test_an_unexplained_new_module_import_fails(tmp_path, capsys):
    base = _scoped_repo(tmp_path, "def read():\n    return 1\n", {"lib/dependency.py": "VALUE = 1\n"})
    head = _scoped_head(tmp_path, "from ..dependency import VALUE\n\ndef read():\n    return 1\n")
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/pkg/new.py"]) == 1
    assert "adds an import of VALUE" in capsys.readouterr().out


def test_a_lazy_import_replaced_by_pass_fails(tmp_path, capsys):
    base = _scoped_repo(tmp_path, "def read():\n    from .dependency import VALUE\n    return VALUE\n",
                        {"lib/dependency.py": "VALUE = object()\n"})
    head = _scoped_head(tmp_path, "def read():\n    pass\n    return VALUE\n")
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/pkg/new.py"]) == 1
    assert "read differs" in capsys.readouterr().out


def test_a_retained_old_file_keeps_its_required_imports(tmp_path, capsys):
    old = 'from .dependency import VALUE\n\nSCHEMA = "x"\n\ndef read():\n    return VALUE\n'
    base = _scoped_repo(tmp_path, old, {"lib/dependency.py": "VALUE = object()\n"})
    head = _commit(tmp_path, {"lib/parts.py": 'SCHEMA = "x"\n',
                              "lib/old.py": "from .parts import SCHEMA\n\ndef read():\n    return VALUE\n"})
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/old.py,lib/parts.py"]) == 1
    assert "lib/old.py uses VALUE but no longer imports it" in capsys.readouterr().out


def test_compiler_directives_must_follow_the_definitions(tmp_path, capsys):
    base = _scoped_repo(tmp_path, "from __future__ import annotations\n\ndef read(value: NotDefined):\n"
                        "    return value\n", {})
    head = _scoped_head(tmp_path, "def read(value: NotDefined):\n    return value\n")
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/pkg/new.py"]) == 1
    assert "compiler directives differ" in capsys.readouterr().out


def test_a_moved_reader_of_a_rebound_import_fails_in_top_level_mode(tmp_path, capsys):
    """Review B (s3-tools 7f2bb6be, F4a), helpers step: the old file imports
    json and rebinds it; the moved helper would read the module, not the dict."""
    old = "import json\njson = {'kind': 'local'}\n\ndef helper():\n    return json\n"
    base = _scoped_repo(tmp_path, old, {})
    head = _commit(tmp_path, {"lib/routes/__init__.py": "",
                              "lib/routes/helpers.py": "import json\n\ndef helper():\n    return json\n",
                              "lib/old.py": "import json\njson = {'kind': 'local'}\n"
                                            "from .routes.helpers import helper\n"})
    assert tool.main(["--root", str(tmp_path), base, head,
                      "lib/old.py=lib/old.py,lib/routes/helpers.py"]) == 1
    assert "helper reads json, which lib/old.py imports and rebinds" in capsys.readouterr().out


@pytest.mark.parametrize("b_text, code, expected", [
    ("def f():\n    return X\n", 1, "f reads X, which lib/b.py does not bind"),
    ("X = 2\n\ndef f():\n    return X\n", 1, "X is defined in both"),
    ("from .a import X\n\ndef f():\n    return X\n", 0, "0 problems"),
])
def test_a_split_resolves_each_moved_read_in_its_new_home(tmp_path, capsys, b_text, code, expected):
    """A one-to-many split where a moved function reads a definition moved to
    another file: it must import it from there (before, the gate passed a b.py
    that read X without binding it)."""
    base = _scoped_repo(tmp_path, "X = 1\n\ndef f():\n    return X\n", {})
    head = _commit(tmp_path, {"lib/a.py": "X = 1\n", "lib/b.py": b_text, "lib/old.py": None})
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/a.py,lib/b.py"]) == code
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("source, reads", [
    ("X = {v: k for k, v in F.items()}", ["F"]),            # PEP 709 inlined variables are not reads
    ("X = [k for k in A] + [k]", ["A", "k"]),               # k outside the comprehension is
    ("X = [k for k in k]", ["k"]),                          # the first iterable is evaluated outside
    ("X = [[a for a in b] for b in C]", ["C"]),
    ("X = X", ["X"]),
    ("def f(count):\n    ranked = sorted(G)\n    return ', '.join(str(k) for k in ranked[:count])", ["G", "sorted", "str"]),
    ("def f():\n    global Y\n    Y = 1\n    return Y", ["Y"]),
    ("X = [holder.value for holder.value in VALUES]", ["VALUES", "holder"]),  # review A ee5897e1 F2
    ("X = [r for rows[0] in A for r in rows]", ["A", "rows"]),
    # Python 3.14 adds a synthetic __conditional_annotations__ to the symtable
    # of an annotated assignment; it is not a module read (rf-s4, rf-s6).
    ("X: Foo = 1", ["Foo"]),
    # ...but a name the source really reads keeps counting, whatever it is
    # called (review B on 03b0631e).
    ("def run():\n    return __conditional_annotations__()", ["__conditional_annotations__"]),
])
def test_global_reads_are_module_reads_only(source, reads):
    """Python 3.12's symtable lists inlined comprehension variables in the
    enclosing scope and can mark a function's own parameters global; neither
    is a module read (S1 commit 3's feature modules hit both)."""
    assert sorted(tool.global_reads(tool.ast.parse(source).body[0])) == reads


def test_a_global_import_in_a_function_is_a_rebinding(tmp_path, capsys):
    """Review A (ee5897e1, F1): `global dep; import cmath as dep` rebinds dep
    without an assignment; a moved reader of dep must fail."""
    imports, writer = "import math as dep\n", "def rebind():\n    global dep\n    import cmath as dep\n"
    reader = "def read():\n    return dep.__name__\n"
    base = _scoped_repo(tmp_path, imports + writer + reader, {})
    head = _commit(tmp_path, {"lib/old.py": imports + writer + "from .moved import read\n",
                              "lib/moved.py": imports + reader})
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/old.py,lib/moved.py"]) == 1
    assert "read reads dep, which lib/old.py imports and rebinds (written through `global` in rebind)" \
        in capsys.readouterr().out


def test_a_retained_reader_of_a_module_attribute_passes_and_a_moved_one_fails(tmp_path, capsys):
    # S9: a definition that stays in the split's old module reads the same
    # __file__ as before; one that moves would read its new module's.
    _git(tmp_path, "init", "-q")
    old = "def where():\n    return __file__\n\n\ndef helper():\n    return 1\n"
    base = _commit(tmp_path, {"lib/__init__.py": "", "lib/old.py": old})
    head = _commit(tmp_path, {
        "lib/old_part.py": "def helper():\n    return 1\n",
        "lib/old.py": "def where():\n    return __file__\n\n\nfrom .old_part import helper  # noqa: E402,F401\n",
    })
    assert tool.main(["--root", str(tmp_path), base, head, "lib/old.py=lib/old.py,lib/old_part.py"]) == 0
    moved = _commit(tmp_path, {
        "lib/old_part.py": "def helper():\n    return 1\n\n\ndef where():\n    return __file__\n",
        "lib/old.py": "from .old_part import helper, where  # noqa: F401\n",
    })
    assert tool.main(["--root", str(tmp_path), base, moved, "lib/old.py=lib/old.py,lib/old_part.py"]) == 1
    assert "where reads __file__, which differs in lib/old_part.py" in capsys.readouterr().out
