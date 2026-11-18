"""Tests for fork.fork_session."""
import json
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.fork import fork_session, encoded_project_dir  # noqa: E402


def _seed_jsonl(projects: pathlib.Path, cwd: str, sid: str, lines: list[dict]):
    d = encoded_project_dir(cwd, projects)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


def test_fork_creates_new_file_with_rewritten_session_id(tmp_path):
    projects = tmp_path / "projects"
    _seed_jsonl(projects, "/home/example", "abc", [
        {"type": "permission-mode", "sessionId": "abc", "permissionMode": "bypassPermissions"},
        {"type": "user", "sessionId": "abc", "message": {"content": "hi"}},
    ])
    new_id = fork_session("abc", "/home/example", projects_root=projects,
                         new_id="def")
    assert new_id == "def"
    dst = encoded_project_dir("/home/example", projects) / "def.jsonl"
    assert dst.is_file()
    written = [json.loads(ln) for ln in dst.read_text().splitlines() if ln]
    assert all(line["sessionId"] == "def" for line in written)
    assert written[1]["message"]["content"] == "hi"


def test_fork_preserves_original(tmp_path):
    projects = tmp_path / "projects"
    p = _seed_jsonl(projects, "/home/example", "abc",
                    [{"sessionId": "abc", "type": "user", "x": 1}])
    fork_session("abc", "/home/example", projects_root=projects, new_id="new")
    assert json.loads(p.read_text().strip())["sessionId"] == "abc"


def test_fork_falls_back_to_global_scan(tmp_path):
    projects = tmp_path / "projects"
    _seed_jsonl(projects, "/home/example/GIT/sqlit", "abc",
                [{"sessionId": "abc", "type": "user"}])
    # saved cwd is /home/example, but the jsonl lives elsewhere — should still work.
    new_id = fork_session("abc", "/home/example", projects_root=projects, new_id="z")
    dst = encoded_project_dir("/home/example/GIT/sqlit", projects) / "z.jsonl"
    assert dst.is_file()
    assert new_id == "z"


def test_fork_raises_when_source_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        fork_session("missing", "/home/example", projects_root=tmp_path)


def test_fork_generates_uuid_when_none_provided(tmp_path):
    projects = tmp_path / "projects"
    _seed_jsonl(projects, "/home/example", "abc",
                [{"sessionId": "abc", "type": "user"}])
    new_id = fork_session("abc", "/home/example", projects_root=projects)
    assert len(new_id) == 36   # uuid4 string
    assert new_id != "abc"


def test_fork_skips_malformed_lines(tmp_path, capsys):
    projects = tmp_path / "projects"
    d = encoded_project_dir("/home/example", projects)
    d.mkdir(parents=True)
    p = d / "abc.jsonl"
    p.write_text('{"sessionId":"abc","type":"user"}\nnot-json\n{"sessionId":"abc","type":"assistant"}\n')
    new_id = fork_session("abc", "/home/example", projects_root=projects, new_id="new")
    written = (d / f"{new_id}.jsonl").read_text().strip().splitlines()
    assert len(written) == 2  # malformed line skipped
    assert "forkLineSkip" in capsys.readouterr().err


def test_fork_into_another_directory_lands_where_claude_code_resumes_it(tmp_path):
    projects = tmp_path / "projects"
    _seed_jsonl(projects, "/home/example", "abc",
                [{"sessionId": "abc", "type": "user", "cwd": "/home/example"}])
    new_id = fork_session("abc", "/home/example", projects_root=projects,
                          new_id="kid", dest_cwd="/home/example/my.repo-worktrees/feat_a")
    # Claude Code names a project dir by replacing every non-alphanumeric
    # character of the cwd with "-" (~/.gemini -> "--gemini").
    dst = projects / "-home-example-my-repo-worktrees-feat-a" / "kid.jsonl"
    assert new_id == "kid" and dst.is_file()
    assert json.loads(dst.read_text())["sessionId"] == "kid"


# R70 review: the copy takes a source id only by the shared rule, and copies and writes only
# inside the store. Anything else is "cannot fork that session": nothing copied, nothing made.

REFUSED_IDS = {"absolute": "/etc/passwd-like", "glob-star": "*", "glob-q": "ab?", "glob-class": "[a]bc",
               "glob-suffix": "ab*", "dotdot": "..", "dotdot-path": "../abc", "separator": "a/b",
               "backslash": "a\\b", "nul": "a\x00b", "empty": "", "leading-dot": ".abc",
               "len-129": "a" * 129}


def _files(*roots):
    return sorted(str(p) for root in roots if root.exists() for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("name", sorted(REFUSED_IDS))
def test_a_source_id_the_rule_refuses_copies_nothing(tmp_path, name):
    projects = tmp_path / "projects"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd-like.jsonl").write_text('{"sessionId": "x"}\n')
    _seed_jsonl(projects, "/home/example", "abc", [{"sessionId": "abc"}])
    _seed_jsonl(projects, "/home/other", "abd", [{"sessionId": "abd"}])
    before = _files(projects, outside)
    source = REFUSED_IDS[name].replace("/etc/passwd-like", str(outside / "passwd-like"))
    with pytest.raises(ValueError, match="cannot fork that session"):
        fork_session(source, "/home/example", projects_root=projects)
    assert _files(projects, outside) == before


def test_a_source_file_that_leads_out_of_the_store_is_not_copied(tmp_path):
    projects = tmp_path / "projects"
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"sessionId": "secret"}\n')
    project = encoded_project_dir("/home/example", projects)
    project.mkdir(parents=True)
    (project / "abc.jsonl").symlink_to(outside)
    before = _files(projects)
    with pytest.raises(ValueError, match="cannot fork that session"):
        fork_session("abc", "/home/example", projects_root=projects)
    assert _files(projects) == before


def test_a_copy_that_would_land_out_of_the_store_is_not_written(tmp_path):
    projects = tmp_path / "projects"
    _seed_jsonl(projects, "/home/example", "abc", [{"sessionId": "abc"}])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    encoded_project_dir("/home/child", projects).symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot fork that session"):
        fork_session("abc", "/home/example", projects_root=projects, dest_cwd="/home/child")
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("new_id", ["../../escaped", "a*b", "-x"])
def test_a_new_id_the_rule_refuses_writes_nothing(tmp_path, new_id):
    """Also inside the store: an id the rule refuses is never written, wherever it would land."""
    projects = tmp_path / "projects"
    _seed_jsonl(projects, "/home/example", "abc", [{"sessionId": "abc"}])
    before = _files(projects, tmp_path)
    with pytest.raises(ValueError, match="cannot fork that session"):
        fork_session("abc", "/home/example", projects_root=projects, new_id=new_id)
    assert _files(projects, tmp_path) == before
