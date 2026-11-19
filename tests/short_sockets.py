"""A short directory for a test's Unix sockets.

A Unix socket path holds at most 108 bytes (Linux's sun_path). A test's tmp_path under a long
temporary directory, such as a pytest base under /var/tmp/<user>/scratch/tmp/pytest-of-<user>/,
can exceed it, and the bind then fails with 'AF_UNIX path too long' whatever the code. Every
test that binds or connects a Unix socket takes its path from here: one short directory per test
(created on first use, removed after the test by the autouse fixture in tests/conftest.py), so
the same name in one test is the same path.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

_current: pathlib.Path | None = None


def socket_path(name: str) -> pathlib.Path:
    """``name`` (a file name, or a relative path whose directories the caller creates) inside
    this test's short socket directory."""
    global _current
    if _current is None:
        _current = pathlib.Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp" if os.path.isdir("/tmp") else None))
    return _current / name


def cleanup() -> None:
    global _current
    if _current is not None:
        shutil.rmtree(_current, ignore_errors=True)
        _current = None
