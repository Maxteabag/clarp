"""tools/refactor/s3_merge.sh: merging main into integration re-applies the S3
moves instead of merging server.py by hand. A main edit to a moved member
reaches the mixin; a replay that does not apply, a conflict outside server.py
and a refused move each stop with their exit code. Uses a throwaway repository
with a tiny server.py; the local gates skip because their test files are absent."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]

SERVER = '''import json


def _helper():
    return 1


class Handler:
    def do_GET(self):
        path = self.path
        if path == "/a":
            return self.a()
        return self.send(404)

    def a(self):
        return json.dumps({"a": 1})

    def stays(self):
        return 2
'''

RECIPE = '''[[group]]
name = "feat"
module = "lib/routes/feat.py"
class = "FeatRoutes"
members = ["a"]
'''


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel in ("tools/refactor/s3_apply.py", "tools/refactor/s3_merge.sh", "scripts/refactor/ast_identity.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO / rel, root / rel)
    (root / "tools" / "refactor" / "s3_moves.toml").write_text(RECIPE)
    (root / "tools" / "refactor" / "s3_replay.txt").write_text("# replay\n")
    (root / "server" / "lib" / "routes").mkdir(parents=True)
    (root / "server" / "lib" / "__init__.py").write_text("")
    (root / "server" / "lib" / "routes" / "__init__.py").write_text("")
    (root / "server" / "server.py").write_text(SERVER)
    (root / "README").write_text("base\n")
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base")
    # integration: a seam-like edit of server.py, listed for replay, then the move.
    _git(root, "switch", "-q", "-c", "integration")
    text = (root / "server" / "server.py").read_text().replace(
        '        if path == "/a":\n            return self.a()\n        return self.send(404)\n',
        '        return dispatch(self, path)\n')
    (root / "server" / "server.py").write_text(text.replace("import json\n", "import json\nfrom lib.dispatch import dispatch\n"))
    (root / "server" / "lib" / "dispatch.py").write_text(
        'def dispatch(handler, path):\n    if path == "/a":\n        return handler.a()\n    return handler.send(404)\n')
    seam = _commit(root, "seam")
    (root / "tools" / "refactor" / "s3_replay.txt").write_text(f"# replay\n{seam}\n")
    _commit(root, "replay list")
    env = dict(os.environ, S3_PYTHON=sys.executable)
    subprocess.run([sys.executable, "tools/refactor/s3_apply.py", "--all"], cwd=root, check=True,
                   capture_output=True, env=env)
    _commit(root, "move feat")
    _git(root, "switch", "-q", "main")
    # main gets the tooling too (it lands on main), with the same replay list.
    _git(root, "checkout", "integration", "--", "tools/refactor/s3_replay.txt")
    _commit(root, "tools on main")
    return root


def _merge(root, work="work"):
    _git(root, "switch", "-q", "integration")
    env = dict(os.environ, S3_PYTHON=sys.executable)
    script = root.parent / "s3_merge.sh"           # run a copy: the merge rewrites the tree
    shutil.copy(root / "tools" / "refactor" / "s3_merge.sh", script)
    result = subprocess.run(["bash", str(script), "main", "integration", work], cwd=root,
                            capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


def test_a_main_edit_to_a_moved_member_reaches_the_mixin(repo):
    text = (repo / "server" / "server.py").read_text().replace('{"a": 1}', '{"a": 2}')
    (repo / "server" / "server.py").write_text(text)
    _commit(repo, "main edits a")
    code, out = _merge(repo)
    assert code == 0, out
    assert "ast_identity: 1 moves" in out and "0 problems" in out
    assert '{"a": 2}' in (repo / "server" / "lib" / "routes" / "feat.py").read_text()
    server = (repo / "server" / "server.py").read_text()
    assert "def a(" not in server and "return dispatch(self, path)" in server
    assert "class Handler(FeatRoutes):" in server


def test_a_replay_that_no_longer_applies_stops_with_4(repo):
    text = (repo / "server" / "server.py").read_text().replace(
        '        return self.send(404)\n', '        return self.send(410)\n')
    (repo / "server" / "server.py").write_text(text)
    _commit(repo, "main edits the chain the seam replaced")
    code, out = _merge(repo)
    assert code == 4, out
    assert "does not apply to main's server.py" in out


def test_a_conflict_outside_server_py_stops_with_3(repo):
    _git(repo, "switch", "-q", "integration")
    (repo / "README").write_text("integration\n")
    _commit(repo, "integration README")
    _git(repo, "switch", "-q", "main")
    (repo / "README").write_text("main\n")
    _commit(repo, "main README")
    code, out = _merge(repo)
    assert code == 3, out
    assert "conflict outside server.py and the generated files: README" in out


def test_a_main_edit_the_generator_refuses_stops_with_5(repo):
    text = (repo / "server" / "server.py").read_text().replace(
        'return json.dumps({"a": 1})', 'return json.dumps({"a": _helper()})')
    (repo / "server" / "server.py").write_text(text)
    _commit(repo, "main makes a read a server.py helper")
    code, out = _merge(repo)
    assert code == 5, out
    assert "reads _helper, which server.py defines" in out


def test_a_merge_git_refuses_stops_with_3_and_no_commit(repo):
    """F1 (review B): unrelated histories make git merge refuse; nothing may
    continue as if main had been merged."""
    _git(repo, "switch", "-q", "--orphan", "other")
    (repo / "README").write_text("unrelated\n")
    _commit(repo, "unrelated root")
    _git(repo, "branch", "-f", "main", "other")
    before = _git(repo, "rev-parse", "integration")
    code, out = _merge(repo)
    assert code == 3, out
    assert "did not start a merge" in out
    assert _git(repo, "rev-parse", "work") == before


def test_untracked_work_under_server_is_refused_before_merging(repo):
    """F2 (review B): an untracked server file is never swept into a commit."""
    _git(repo, "switch", "-q", "integration")
    (repo / "server" / "unrelated_work.py").write_text("x = 1\n")
    env = dict(os.environ, S3_PYTHON=sys.executable)
    script = repo.parent / "s3_merge.sh"
    shutil.copy(repo / "tools" / "refactor" / "s3_merge.sh", script)
    result = subprocess.run(["bash", str(script), "main", "integration", "work"], cwd=repo,
                            capture_output=True, text=True, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "server/unrelated_work.py" in result.stderr
    assert (repo / "server" / "unrelated_work.py").exists()
    branches = _git(repo, "branch", "--list", "work")
    assert branches == ""


HELPERS_RECIPE = '[helpers]\nnames = ["_helper"]\n\n' + RECIPE


@pytest.fixture
def repo_with_helpers(tmp_path):
    """Like `repo`, but member a reads the server.py helper and the recipe moves
    the helper too: integration carries both stages."""
    root = tmp_path / "repo"
    for rel in ("tools/refactor/s3_apply.py", "tools/refactor/s3_merge.sh", "scripts/refactor/ast_identity.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO / rel, root / rel)
    (root / "tools" / "refactor" / "s3_moves.toml").write_text(HELPERS_RECIPE)
    (root / "tools" / "refactor" / "s3_replay.txt").write_text("# replay\n")
    (root / "server" / "lib" / "routes").mkdir(parents=True)
    (root / "server" / "lib" / "__init__.py").write_text("")
    (root / "server" / "lib" / "routes" / "__init__.py").write_text("")
    (root / "server" / "server.py").write_text(
        SERVER.replace('return json.dumps({"a": 1})', 'return json.dumps({"a": _helper()})'))
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base")
    _git(root, "switch", "-q", "-c", "integration")
    env = dict(os.environ, S3_PYTHON=sys.executable)
    subprocess.run([sys.executable, "tools/refactor/s3_apply.py", "--helpers"], cwd=root, check=True,
                   capture_output=True, env=env)
    _commit(root, "helpers")
    subprocess.run([sys.executable, "tools/refactor/s3_apply.py", "--all"], cwd=root, check=True,
                   capture_output=True, env=env)
    _commit(root, "groups")
    _git(root, "switch", "-q", "main")
    return root


def test_a_merge_with_helpers_and_groups_commits_both_stages(repo_with_helpers):
    """Review B (40747475) F1: stage B stages only server.py and helpers.py, stage
    C the group mixins; a main edit to a moved member that reads a helper reaches
    the mixin."""
    repo = repo_with_helpers
    text = (repo / "server" / "server.py").read_text().replace('{"a": _helper()}', '{"a": _helper() + 1}')
    (repo / "server" / "server.py").write_text(text)
    _commit(repo, "main edits a")
    code, out = _merge(repo)
    assert code == 0, out
    assert out.count("0 problems") == 2
    log = _git(repo, "log", "--format=%s", "-3", "work")
    assert log.splitlines()[:2] == ["s3_apply: group feat", "s3_apply: helpers step"]
    feat = (repo / "server" / "lib" / "routes" / "feat.py").read_text()
    assert '{"a": _helper() + 1}' in feat and "from lib.routes.helpers import _helper" in feat


def test_a_failing_status_query_is_never_read_as_clean(repo, tmp_path):
    """Review B (40747475) F3: git status failing (128) on the tracked-tree query
    must stop the script, not certify a clean tree."""
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text("#!/bin/sh\n"
                    'if [ "$1" = status ] && [ "$3" = "--untracked-files=no" ]; then exit 128; fi\n'
                    f'exec {real_git} "$@"\n')
    shim.chmod(0o755)
    _git(repo, "switch", "-q", "integration")
    env = dict(os.environ, S3_PYTHON=sys.executable, PATH=f"{shim_dir}:{os.environ['PATH']}")
    script = repo.parent / "s3_merge.sh"
    shutil.copy(repo / "tools" / "refactor" / "s3_merge.sh", script)
    result = subprocess.run(["bash", str(script), "main", "integration", "work"], cwd=repo,
                            capture_output=True, text=True, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "git status failed" in result.stderr
    assert _git(repo, "branch", "--list", "work") == ""


def test_each_group_is_its_own_verified_commit(repo):
    """A recipe with two groups gives two move-only commits in recipe order,
    each touching server.py and its own mixin only, each checked by ast_identity."""
    (repo / "tools" / "refactor" / "s3_moves.toml").write_text(
        RECIPE + '\n[[group]]\nname = "more"\nmodule = "lib/routes/more.py"\n'
                 'class = "MoreRoutes"\nmembers = ["stays"]\n')
    _commit(repo, "recipe: second group")
    code, out = _merge(repo)
    assert code == 0, out
    assert out.count("0 problems") == 2
    subjects = _git(repo, "log", "--format=%s", "-2", "work").splitlines()
    assert subjects == ["s3_apply: group more", "s3_apply: group feat"]
    touched = [sorted(_git(repo, "show", "--name-only", "--format=", f"work~{n}").splitlines())
               for n in (1, 0)]
    assert touched == [["server/lib/routes/feat.py", "server/server.py"],
                       ["server/lib/routes/more.py", "server/server.py"]]


# ---- S9 module splits (stage D) ----------------------------------------------------

BIG = 'import json\n\n\ndef helper():\n    return json.dumps(1)\n\n\ndef stays():\n    return helper()\n'
SPLITS = ('[[split]]\nname = "big"\nmodule = "lib/big.py"\n'
          '[[split.part]]\nmodule = "lib/big_helpers.py"\nnames = ["helper"]\n')


@pytest.fixture
def repo_with_split(repo):
    """main has lib/big.py, the split tooling and its recipe; integration has the
    split applied (as the previous merge left it)."""
    for rel in ("tools/refactor/split_apply.py",):
        shutil.copy(_REPO / rel, repo / rel)
    (repo / "tools" / "refactor" / "splits").mkdir()
    (repo / "tools" / "refactor" / "splits" / "s9-test.toml").write_text(SPLITS)
    (repo / "server" / "lib" / "big.py").write_text(BIG)
    _commit(repo, "split tooling and big.py on main")
    _git(repo, "switch", "-q", "integration")
    _git(repo, "merge", "-q", "--no-ff", "-m", "earlier merge", "main")
    subprocess.run([sys.executable, "tools/refactor/split_apply.py", "--all"], cwd=repo, check=True,
                   capture_output=True)
    _commit(repo, "split_apply: big")
    _git(repo, "switch", "-q", "main")
    return repo


def test_a_main_edit_to_a_split_definition_reaches_its_part(repo_with_split):
    big = (repo_with_split / "server" / "lib" / "big.py").read_text().replace("dumps(1)", "dumps(2)")
    (repo_with_split / "server" / "lib" / "big.py").write_text(big)
    _commit(repo_with_split, "main edits helper")
    code, out = _merge(repo_with_split)
    assert code == 0, out
    assert "split big: ast_identity lib/big.py=lib/big.py,lib/big_helpers.py ok" in out
    assert "dumps(2)" in (repo_with_split / "server" / "lib" / "big_helpers.py").read_text()
    facade = (repo_with_split / "server" / "lib" / "big.py").read_text()
    assert "def helper" not in facade and "from .big_helpers import (" in facade
    assert _git(repo_with_split, "log", "-1", "--format=%s") == "split_apply: big"


def test_an_integration_only_edit_to_a_split_module_stops_with_8(repo_with_split):
    _git(repo_with_split, "switch", "-q", "integration")
    facade = repo_with_split / "server" / "lib" / "big.py"
    facade.write_text(facade.read_text().replace("return helper()", "return helper() + '!'"))
    _commit(repo_with_split, "integration-only edit")
    _git(repo_with_split, "switch", "-q", "main")
    (repo_with_split / "README").write_text("main moved\n")
    _commit(repo_with_split, "main moves")
    code, out = _merge(repo_with_split)
    assert code == 8, out
    assert "integration-only edit in server/lib/big.py (split big)" in out


def test_an_integration_only_edit_before_the_first_split_stops_with_8(repo_with_split):
    # Review B F3: with no part on integration yet, stage A would still take
    # main's text of the old module and drop integration's edit.
    _git(repo_with_split, "switch", "-q", "integration")
    _git(repo_with_split, "reset", "-q", "--hard", "integration~1")
    big = repo_with_split / "server" / "lib" / "big.py"
    assert not (repo_with_split / "server" / "lib" / "big_helpers.py").exists()
    big.write_text(big.read_text().replace("return helper()", "return helper() + '!'"))
    _commit(repo_with_split, "integration edit before the first split")
    _git(repo_with_split, "switch", "-q", "main")
    (repo_with_split / "README").write_text("main moved\n")
    _commit(repo_with_split, "main moves")
    code, out = _merge(repo_with_split)
    assert code == 8, out
    assert "integration-only edit in server/lib/big.py (split big, not yet applied)" in out


def _conflict_in_other_module(repo):
    """main and integration both edit server/lib/other.py on the same line."""
    _git(repo, "switch", "-q", "main")
    (repo / "server" / "lib" / "other.py").write_text("VALUE = 1\n")
    _commit(repo, "other on main")
    _git(repo, "switch", "-q", "integration")
    _git(repo, "merge", "-q", "--no-edit", "main")
    (repo / "server" / "lib" / "other.py").write_text("VALUE = 2  # integration\n")
    _commit(repo, "integration edits other")
    _git(repo, "switch", "-q", "main")
    (repo / "server" / "lib" / "other.py").write_text("VALUE = 3  # main\n")
    _commit(repo, "main edits other")


def _resolution(repo, blobs, resolved="VALUE = 23  # both\n"):
    folder = repo / "tools" / "refactor" / "merge_resolutions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "other.py.resolved").write_text(resolved)
    base, main_blob, integration_blob = blobs
    (folder / "other.toml").write_text(
        f'path = "server/lib/other.py"\nresolved = "other.py.resolved"\n'
        f'base_blob = "{base}"\nmain_blob = "{main_blob}"\nintegration_blob = "{integration_blob}"\n')
    _commit(repo, "resolution on main")


def _blobs(repo):
    base = _git(repo, "merge-base", "main", "integration")
    return tuple(_git(repo, "rev-parse", f"{ref}:server/lib/other.py") for ref in (base, "main", "integration"))


def test_a_reviewed_resolution_applies_when_all_three_blobs_match(repo):
    _conflict_in_other_module(repo)
    _resolution(repo, _blobs(repo))
    code, out = _merge(repo)
    assert code == 0, out
    assert "merge_resolution other: applied to server/lib/other.py, blobs base/main/int = " in out
    assert _git(repo, "show", "work:server/lib/other.py") == "VALUE = 23  # both"


def test_a_stale_resolution_stops_with_10(repo):
    _conflict_in_other_module(repo)
    base, main_blob, _ = _blobs(repo)
    _resolution(repo, (base, main_blob, "0" * 40))
    code, out = _merge(repo)
    assert code == 10, out
    assert "STALE other.toml server/lib/other.py" in out
