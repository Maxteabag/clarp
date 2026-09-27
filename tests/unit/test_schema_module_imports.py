"""Every module db_schema pulls a SCHEMA from must import on its own.

db_schema imports those SCHEMA strings while `db` is still loading, so a module
that imports `db` before defining SCHEMA breaks whenever it is imported first
(clarp-admin does exactly that).
"""
import pathlib
import re
import subprocess
import sys

import pytest

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
MODULES = sorted(set(re.findall(r"^from \.(\w+) import SCHEMA",
                                (SERVER / "lib" / "db_schema.py").read_text(), re.M)))


@pytest.mark.parametrize("module", MODULES)
def test_schema_module_imports_first(module):
    result = subprocess.run([sys.executable, "-c", f"import lib.{module}"], cwd=SERVER,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
