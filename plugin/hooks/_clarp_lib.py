"""Put Clarp's `lib` package on sys.path for a hook script.

A hook runs as a bare `python3 <plugin>/hooks/<name>.py`, so it gets none of
the server's import setup and has to find `lib` itself. Deriving that from
__file__ rather than from $HOME is what makes the hooks work in a container,
where HOME is /home/clarp but the code lives in /opt/clarp:

    native install   <share>/current/plugin/hooks/x.py  ->  <share>/current/lib
    container        /opt/clarp/plugin/hooks/x.py       ->  /opt/clarp/lib
    repo checkout    <repo>/plugin/hooks/x.py           ->  <repo>/server/lib

Import this for its side effect, before importing anything from `lib`. It is
silent when no candidate matches: the caller's `except ImportError` then makes
the hook a no-op, which is the right behaviour on a machine where Clarp is not
installed.
"""
from __future__ import annotations

import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]

_CANDIDATES = (
    _ROOT,                                    # installed release / container
    _ROOT / "server",                         # repo checkout
    pathlib.Path(os.environ.get("CLARP_SHARE_DIR", "")) if os.environ.get(
        "CLARP_SHARE_DIR") else None,
    pathlib.Path.home() / ".local/share/clarp",   # legacy layout
)

for _candidate in _CANDIDATES:
    if _candidate is None:
        continue
    if (_candidate / "lib" / "__init__.py").is_file() or (
            _candidate / "lib" / "agents.py").is_file():
        _path = str(_candidate)
        if _path not in sys.path:
            sys.path.insert(0, _path)
        break
