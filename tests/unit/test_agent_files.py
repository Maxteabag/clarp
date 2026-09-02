import os
import pathlib

import pytest

from lib.agent_files import (
    AgentFileForbidden, AgentFileTooLarge, AgentFileUnsupported,
    MAX_FILE_BYTES, list_directory, read_text_file,
)
from server import _raw_query_value


def test_lists_directories_first_and_reads_utf8(tmp_path: pathlib.Path):
    (tmp_path / "z.txt").write_text("hello")
    (tmp_path / "docs").mkdir()
    (tmp_path / "a.swift").write_text("let answer = 42")

    result = list_directory(tmp_path)
    assert result["root_path"] == str(tmp_path)
    assert [row["name"] for row in result["entries"]] == ["docs", "a.swift", "z.txt"]
    assert read_text_file(tmp_path, "a.swift")["content"] == "let answer = 42"


def test_rejects_symlink_that_escapes_root(tmp_path: pathlib.Path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    (tmp_path / "escape").symlink_to(outside)
    with pytest.raises(AgentFileForbidden):
        read_text_file(tmp_path, "escape")


def test_rejects_binary_and_large_files(tmp_path: pathlib.Path):
    (tmp_path / "binary").write_bytes(b"a\0b")
    with pytest.raises(AgentFileUnsupported):
        read_text_file(tmp_path, "binary")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(AgentFileTooLarge):
        read_text_file(tmp_path, "large.txt")


def test_preserves_whitespace_in_names(tmp_path: pathlib.Path):
    (tmp_path / " notes ").write_text("spaced")
    (tmp_path / "notes").write_text("plain")
    assert read_text_file(tmp_path, " notes ")["content"] == "spaced"


def test_rejects_nul_and_hides_fifo(tmp_path: pathlib.Path):
    with pytest.raises(AgentFileForbidden):
        read_text_file(tmp_path, "bad\0path")
    os.mkfifo(tmp_path / "pipe")
    assert "pipe" not in {row["name"] for row in list_directory(tmp_path)["entries"]}


def test_rejects_opened_root_outside_container_confinement(tmp_path: pathlib.Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(AgentFileForbidden):
        list_directory(outside, confinement_root=workspace)


def test_raw_path_query_preserves_plus_signs():
    assert _raw_query_value("/agent-files?root=%2Fhome%2Fuser%2FC++", "root") \
        == "/home/user/C++"
