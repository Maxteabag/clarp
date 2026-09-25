"""Each split module must import on its own, in a fresh interpreter.

`db` is split into `db_schema` / `db_migrations` and `message_store` into
`message_context` / `message_writes` / `message_live` / `message_previews`.
The sub-schema providers import `db` back and the message modules look the
facade up at call time, so a name bound at import time from a half-initialised
sibling would only surface for whoever imports that module first. Importing
each one alone, before anything else, is the cheapest way to prove there is no
such order dependency.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
MODULES = (
    "db", "db_schema", "db_migrations",
    "message_store", "message_context", "message_writes", "message_live",
    "message_previews",
)


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_alone_in_a_fresh_interpreter(module):
    result = subprocess.run(
        [sys.executable, "-c",
         f"import server.lib.{module} as m; print(m.__name__)"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"server.lib.{module}"


def test_facade_names_are_the_same_objects():
    from lib import (  # noqa: E402
        db, db_migrations, db_schema, message_context, message_live,
        message_previews, message_store, message_writes,
    )
    assert db._SCHEMA_SQL is db_schema._SCHEMA_SQL
    assert db._SCHEMA_VERSION == db_schema._SCHEMA_VERSION
    assert db._migrate is db_migrations._migrate
    assert db._create_schema is db_migrations._create_schema
    for module in (message_context, message_writes, message_live, message_previews):
        for name, value in vars(module).items():
            if name.startswith("__") or name == "_facade" or not callable(value):
                continue
            if getattr(value, "__module__", None) != module.__name__:
                continue
            assert getattr(message_store, name) is value, name
