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
