"""A hook must find Clarp's `lib` from its own location, not from $HOME.

The container is why: HOME is /home/clarp but the code lives in /opt/clarp, so
a hook that resolved `lib` via $HOME/.local/share/clarp imported nothing
and silently exited 0 — every state event lost, with no error anywhere.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOKS = REPO / "plugin" / "hooks"


def _layout(root: pathlib.Path, *, lib_at: str) -> pathlib.Path:
    """Build a fake install: <root>/<lib_at>/lib plus <root>/plugin/hooks."""
    (root / lib_at).mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / "server" / "lib", root / lib_at / "lib")
    shutil.copytree(HOOKS, root / "plugin" / "hooks")
    return root / "plugin" / "hooks"


def _resolves(hooks_dir: pathlib.Path, *, home: pathlib.Path) -> str:
    """Return the sys.path entry _clarp_lib chose, or '' if it found nothing."""
    code = (
        "import sys; sys.path.insert(0, %r); import _clarp_lib;"
        "import lib.agents; print(sys.path[0])" % str(hooks_dir)
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    return r.stdout.strip() if r.returncode == 0 else ""


def test_container_layout_resolves_without_home(tmp_path):
    """/opt/clarp/plugin/hooks/x.py -> /opt/clarp/lib"""
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    hooks = _layout(tmp_path / "opt", lib_at=".")
    assert _resolves(hooks, home=empty_home) == str(tmp_path / "opt")


def test_repo_layout_resolves(tmp_path):
    """<repo>/plugin/hooks/x.py -> <repo>/server/lib"""
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    hooks = _layout(tmp_path / "repo", lib_at="server")
    assert _resolves(hooks, home=empty_home) == str(tmp_path / "repo" / "server")


def test_every_hook_uses_the_helper():
    """No hook may go back to deriving the path from $HOME."""
    for hook in HOOKS.glob("*.py"):
        if hook.name == "_clarp_lib.py":
            continue
        text = hook.read_text()
        assert "import _clarp_lib" in text, hook.name
        assert ".local/share/clarp" not in text, (
            f"{hook.name} still resolves lib from $HOME")


def test_hook_is_a_silent_noop_when_clarp_is_absent(tmp_path):
    """On a machine without Clarp the hook must exit 0 and stay quiet, so it
    never blocks a tool call."""
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    lone = tmp_path / "lonely" / "plugin" / "hooks"
    lone.mkdir(parents=True)
    shutil.copy(HOOKS / "tool_activity.py", lone / "tool_activity.py")
    shutil.copy(HOOKS / "_clarp_lib.py", lone / "_clarp_lib.py")
    r = subprocess.run([sys.executable, str(lone / "tool_activity.py")],
                       input="{}", capture_output=True, text=True,
                       env={"HOME": str(empty_home), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == "", r.stderr
