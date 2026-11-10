"""S1 aliases (docs/architecture/s1-layering.md 3.3): an old and a new module
path name one module object, so imports and monkeypatches through either name
reach the same code."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

import lib
import lib.db_schema
import lib.persistence
import lib.persistence.schema
from lib import db, db_schema
from lib.persistence import schema

# (real module, alias) while the code still lives at the old path.
ALIASES = [("lib.db_schema", "lib.persistence.schema")]


def test_every_spelling_names_one_module_object():
    assert schema is db_schema is lib.db_schema is lib.persistence.schema
    assert sys.modules["lib.persistence.schema"] is sys.modules["lib.db_schema"]
    assert importlib.import_module("lib.persistence.schema") is db_schema
    assert lib.persistence.schema is db_schema and lib.db_schema is db_schema


def test_a_patch_through_one_name_is_seen_through_the_other(monkeypatch):
    monkeypatch.setattr(schema, "_SCHEMA_VERSION", -1)
    assert db_schema._SCHEMA_VERSION == -1
    assert db._SCHEMA_VERSION == -1


def test_the_alias_works_when_imported_first_in_a_fresh_interpreter(tmp_path):
    for real, alias in ALIASES:
        probe = (f"import sys, importlib\n"
                 f"a = importlib.import_module({alias!r})\n"
                 f"b = importlib.import_module({real!r})\n"
                 f"assert a is b is sys.modules[{alias!r}] is sys.modules[{real!r}], (a, b)\n"
                 f"assert a.__name__ == {real!r}\n")
        env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_PWA_DB": str(tmp_path / "state.sqlite"),
               "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
               "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
        subprocess.run([sys.executable, "-c", probe], check=True, env=env, cwd=os.path.dirname(lib.__path__[0]))
