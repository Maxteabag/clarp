"""Regression test for ROOT resolution.

The HTTP server defaults its filesystem ROOT by probing for `static/index.html`
next to it. Two real-world layouts:
  - repo:    <repo>/server/lib/context.py → root = <repo>
  - install: <share>/lib/context.py       → root = <share>

Before this was implemented the server hardcoded `__file__.parent.parent`,
which silently resolved to ~/.local/share/ after install (no static/ there) →
every GET returned 404.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.context import resolve_root  # noqa: E402


def test_repo_layout_resolves_to_repo_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / "server" / "lib").mkdir(parents=True)
    (repo / "static").mkdir()
    (repo / "static" / "index.html").write_text("ok")
    ctx_file = repo / "server" / "lib" / "context.py"
    ctx_file.write_text("")
    assert resolve_root(ctx_file, env={}) == repo


def test_install_layout_resolves_to_share_dir(tmp_path):
    share = tmp_path / "share" / "clarp"
    (share / "lib").mkdir(parents=True)
    (share / "static").mkdir()
    (share / "static" / "index.html").write_text("ok")
    ctx_file = share / "lib" / "context.py"
    ctx_file.write_text("")
    assert resolve_root(ctx_file, env={}) == share


def test_env_override_wins(tmp_path):
    override = tmp_path / "elsewhere"
    override.mkdir()
    assert resolve_root(tmp_path / "anywhere.py",
                        env={"CLAUDE_PWA_ROOT": str(override)}) == override


def test_falls_back_when_no_layout_matches(tmp_path):
    # No static/index.html in either candidate location → fall back to repo
    # convention so the missing-file error surfaces loudly at first request.
    nowhere = tmp_path / "nowhere"
    (nowhere / "lib").mkdir(parents=True)
    fake = nowhere / "lib" / "context.py"
    fake.write_text("")
    result = resolve_root(fake, env={})
    # repo_root = nowhere.parent
    assert result == tmp_path
