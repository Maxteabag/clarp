import json

from lib import session_catalog


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_claude_catalog_uses_latest_custom_title_and_all_projects(tmp_path):
    root = tmp_path / "projects"
    first = root / "-work-one" / "aaa.jsonl"
    second = root / "-work-two" / "bbb.jsonl"
    _write(first, [
        {"type": "user", "cwd": "/work/one",
         "message": {"content": "Original request"}},
        {"type": "custom-title", "cwd": "/work/one",
         "customTitle": "Renamed feature"},
    ])
    _write(second, [
        {"type": "user", "cwd": "/work/two",
         "message": {"content": [{"type": "text", "text": "Other request"}]}},
        {"type": "assistant", "cwd": "/work/two", "aiTitle": "Generated title"},
    ])

    scoped = session_catalog.list_claude_sessions(
        "/work/one", projects_root=root)
    assert [(item["id"], item["title"], item["cwd"]) for item in scoped] == [
        ("aaa", "Renamed feature", "/work/one")]

    all_sessions = session_catalog.list_claude_sessions(
        "/work/one", all_projects=True, projects_root=root)
    assert {item["id"] for item in all_sessions} == {"aaa", "bbb"}
    assert next(item for item in all_sessions if item["id"] == "bbb")["title"] \
        == "Generated title"


def test_claude_catalog_keeps_uuid_when_session_has_rename(tmp_path):
    root = tmp_path / "projects"
    path = root / "-repo" / "stable-uuid.jsonl"
    _write(path, [
        {"type": "user", "cwd": "/repo",
         "message": {"content": "Start"}},
        {"type": "custom-title", "cwd": "/repo", "customTitle": "Friendly name"},
    ])
    [item] = session_catalog.list_claude_sessions("/repo", projects_root=root)
    assert item["id"] == "stable-uuid"
    assert item["title"] == "Friendly name"


def test_claude_catalog_prefers_lightweight_session_index(tmp_path):
    root = tmp_path / "projects"
    project = root / "-repo"
    project.mkdir(parents=True)
    (project / "sessions-index.json").write_text(json.dumps({
        "version": 1,
        "entries": [{
            "sessionId": "indexed-uuid",
            "fileMtime": 1_780_000_000_000,
            "firstPrompt": "Initial request",
            "summary": "Generated summary",
            "customTitle": "Renamed in Claude",
            "projectPath": "/repo",
            "isSidechain": False,
        }],
    }))
    # An index-backed listing must not need to open the transcript body.
    transcript = project / "indexed-uuid.jsonl"
    transcript.write_text("not valid json and deliberately irrelevant\n")

    [item] = session_catalog.list_claude_sessions("/repo", projects_root=root)
    assert item == {
        "id": "indexed-uuid", "mtime": 1_780_000_000,
        "preview": "Initial request", "title": "Renamed in Claude",
        "cwd": "/repo",
    }


def test_claude_index_folder_scope_filters_moved_sessions(tmp_path):
    root = tmp_path / "projects"
    project = root / "-repo"
    project.mkdir(parents=True)
    (project / "sessions-index.json").write_text(json.dumps({
        "entries": [
            {"sessionId": "here", "projectPath": "/repo", "fileMtime": 2},
            {"sessionId": "moved", "projectPath": "/elsewhere", "fileMtime": 3},
        ],
    }))
    (project / "here.jsonl").write_text("{}\n")
    (project / "moved.jsonl").write_text("{}\n")
    assert [item["id"] for item in session_catalog.list_claude_sessions(
        "/repo", projects_root=root)] == ["here"]


def test_all_projects_merges_indexed_and_legacy_directories(tmp_path):
    root = tmp_path / "projects"
    indexed = root / "-indexed"
    indexed.mkdir(parents=True)
    (indexed / "sessions-index.json").write_text(json.dumps({
        "entries": [{
            "sessionId": "indexed", "projectPath": "/indexed",
            "fileMtime": 3_000,
        }],
    }))
    (indexed / "indexed.jsonl").write_text("{}\n")
    _write(root / "-legacy" / "legacy.jsonl", [{
        "type": "user", "cwd": "/legacy",
        "message": {"content": "Legacy request"},
    }])
    ids = {
        item["id"] for item in session_catalog.list_claude_sessions(
            "/indexed", all_projects=True, projects_root=root)
    }
    assert ids == {"indexed", "legacy"}


def test_claude_index_omits_missing_transcripts(tmp_path):
    root = tmp_path / "projects"
    project = root / "-repo"
    project.mkdir(parents=True)
    (project / "sessions-index.json").write_text(json.dumps({
        "entries": [{
            "sessionId": "deleted", "projectPath": "/repo", "fileMtime": 1,
            "fullPath": str(project / "deleted.jsonl"),
        }],
    }))
    assert session_catalog.list_claude_sessions("/repo", projects_root=root) == []


def test_claude_partial_index_merges_unindexed_transcript(tmp_path):
    root = tmp_path / "projects"
    project = root / "-repo"
    project.mkdir(parents=True)
    (project / "sessions-index.json").write_text(json.dumps({
        "entries": [{
            "sessionId": "indexed", "projectPath": "/repo", "fileMtime": 2,
        }],
    }))
    (project / "indexed.jsonl").write_text("{}\n")
    _write(project / "unindexed.jsonl", [{
        "type": "user", "cwd": "/repo", "message": {"content": "Still resumable"},
    }])
    assert {
        item["id"] for item in session_catalog.list_claude_sessions(
            "/repo", projects_root=root)
    } == {"indexed", "unindexed"}
