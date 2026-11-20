"""Lifetime of scratch stores used by the Rust Host's Python tools."""
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import tempfile


@contextmanager
def directory(prefix='clarp-'):
    """Own one directory until normal return, an exception, or SystemExit."""
    parent = os.environ.get('TMPDIR') or '/var/tmp'
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as name:
        yield name


def case(prefix):
    """Pass an owned store to a generator case, in the process running it."""
    def decorate(run):
        @wraps(run)
        def wrapped(*args, **kwargs):
            with directory(prefix) as name:
                return run(Path(name), *args, **kwargs)
        return wrapped
    return decorate


def child_env(env):
    """Carry the caller's scratch placement through an isolated child's allowlist."""
    return {**env, 'TMPDIR': os.environ.get('TMPDIR') or '/var/tmp'}
