"""Workspace vocabulary: what a checkout is called, out loud."""
from __future__ import annotations

import subprocess

import pytest

from lib import workspace_vocab
from lib.workspace_vocab import identifiers_from_paths, sources_for


@pytest.fixture(autouse=True)
def _fresh_cache():
    workspace_vocab.reset_for_tests()
    yield
    workspace_vocab.reset_for_tests()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
                        "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin"})


def test_identifiers_split_camel_and_snake_and_rank_by_recurrence():
    paths = [
        "server/lib/clip_store.py", "server/lib/clip_delivery/hls.py",
        "static/lib/ClipStreamBroker.js", "tests/unit/test_clip_store.py",
        "README.md", "src/index.js", "node_modules/left-pad/index.js",
    ]
    ids = identifiers_from_paths(paths)
    assert ids[0] == "clip"                      # appears in four stems
    assert "ClipStreamBroker" in ids and "Stream" in ids and "Broker" in ids
    assert "store" in ids
    for noise in ("README", "index", "test", "left", "lib"):
        assert noise not in ids


def test_repository_yields_project_branches_commits_and_identifiers(tmp_path):
    repo = tmp_path / "EcitServicePortal"
    repo.mkdir()
    (repo / "TelephonyRangeCalculator.cs").write_text("x")
    (repo / "DashboardSliceFactory.cs").write_text("x")
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Add telephony range calculator")
    _git(repo, "branch", "worktree-751-shared-plain-library")

    src = sources_for(repo)
    assert src.project_name == "EcitServicePortal"
    assert "Telephony" in src.identifiers and "Dashboard" in src.identifiers
    assert "master" in src.branches
    assert "worktree-751-shared-plain-library" in src.branches
    assert src.commit_subjects == ("Add telephony range calculator",)
    assert len(src.head) == 40


def test_results_are_cached_until_head_moves(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "AlphaThing.py").write_text("x")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    first = sources_for(repo, now=0.0)
    calls = []
    real = workspace_vocab._collect
    monkeypatch.setattr(workspace_vocab, "_collect",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    assert sources_for(repo, now=1.0) is first
    assert calls == []
    (repo / "BetaThing.py").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    second = sources_for(repo, now=2.0)
    assert calls == [1]
    assert "BetaThing" in second.identifiers


def test_plain_directory_still_names_itself_and_its_files(tmp_path):
    d = tmp_path / "Brainsymph"
    d.mkdir()
    (d / "SynapseGraph.swift").write_text("x")
    src = sources_for(d)
    assert src.project_name == "Brainsymph"
    assert "SynapseGraph" in src.identifiers
    assert src.branches == () and src.commit_subjects == ()


def test_missing_or_empty_cwd_is_harmless(tmp_path):
    assert sources_for(None).project_name == ""
    assert sources_for(tmp_path / "nope").identifiers == ()
