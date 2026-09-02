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
