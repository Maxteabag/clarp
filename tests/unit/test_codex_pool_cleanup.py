"""What the conftest fixture for hand-built Codex clients must not do.

It takes a client out of the pools only when that client is an object a
test built by hand. One that is a process stays, so the quiescence gate
still refuses a test that left a process behind. (From the review of #297:
without this, a fixture that dropped processes too passed every test.)
"""
import subprocess
import sys
from unittest.mock import Mock

import pytest

from lib import codex_app_server as cas
from tests import codex_pool_lifecycle as life


def test_the_drop_leaves_a_process_and_the_gate_still_refuses_it(monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real, built = object.__new__(cas._Client), object.__new__(cas._Client)
        real.proc, built.proc = process, Mock()
        monkeypatch.setattr(cas, "_CLIENTS", {"real": real, "built": built})
        monkeypatch.setattr(cas, "_DRAINING", {})
        monkeypatch.setattr(cas, "_RETIRING", {})
        assert life.drop_hand_built_clients() == 1
        assert list(cas._CLIENTS) == ["real"]
        with pytest.raises(life.QuiescenceError):
            life.GATE.check()
    finally:
        process.kill()
        process.wait()
        cas._CLIENTS.clear()
