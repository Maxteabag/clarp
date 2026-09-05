"""The fleet map's rule table and archetype library."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

import pytest  # noqa: E402

from lib import viz_archetypes, viz_normalize  # noqa: E402


@pytest.fixture()
def any_checkout_exists(monkeypatch):
    """Repo naming consults the filesystem; keep unit tests hermetic."""
    monkeypatch.setattr(viz_normalize, "_is_checkout", lambda path: True)
    yield


def test_every_emitted_verb_has_an_archetype():
    """A rule added without an archetype would silently render as a pulse."""
    emitted = {v for v, _ in viz_normalize.EXE_RULES.values()}
    emitted |= {v for v, _ in viz_normalize.NATIVE_RULES.values()}
    emitted |= {"push", "github", "vcs"}          # produced by special cases
    missing = sorted(emitted - set(viz_archetypes.VERB_ARCHETYPE))
    assert not missing, f"verbs with no archetype: {missing}"


def test_every_archetype_has_a_render_spec():
    for name in viz_archetypes.ARCHETYPES:
        assert name in viz_archetypes.SPEC, name
        spec = viz_archetypes.SPEC[name]
        assert {"travel", "decay", "persist", "weight", "trail"} <= set(spec)


def test_scans_past_shell_keywords_to_the_real_command(any_checkout_exists):
    """Leading-segment parsing scored 33% on live data; token scan reaches 96%."""
    assert viz_normalize.classify(
        "Bash", {"command": "if [ -f X ]; then cat /home/p/GIT/clarp/a.py; fi"}
    ) == ("read", "repo:clarp")


def test_environment_preamble_does_not_win_over_the_real_command():
    """`eval "$(brew shellenv)" && git commit` is a commit, not a build."""
    verb, _ = viz_normalize.classify(
        "Bash", {"command": 'eval "$(brew shellenv)" && git commit -m x'})
    assert verb == "vcs"
    assert viz_normalize.classify("Bash", {"command": "brew install jq"}) == (
        "build", "toolchain")


def test_git_push_and_gh_run_reach_distinct_services():
    assert viz_normalize.classify("Bash", {"command": "git push"}) == (
        "push", "service:github")
    assert viz_normalize.classify("Bash", {"command": "gh run watch 1"}) == (
        "github", "service:github-actions")


def test_unclassifiable_command_is_dropped_not_guessed():
    assert viz_normalize.classify("Bash", {"command": "set -euo pipefail"}) is None


def test_authored_archetype_must_come_from_the_library():
    ok, _ = viz_archetypes.validate_assignment("github", "process")
    assert ok
    bad, why = viz_archetypes.validate_assignment("github", "explode")
    assert not bad and "library" in why


def test_normalize_dedupes_duplicate_dispatch_rows():
    # Real dispatch rows carry the command wrapped in a shell invocation;
    # a bare command never appears in that field.
    row = {"agent_id": "a1", "ts": 1000,
           "detail": '{"dispatch":"codex",'
                     '"tool":"/usr/bin/bash -lc \'git push\'","trace_id":"t1"}'}
    events = viz_normalize.normalize([row, dict(row)], {"a1": "Josh"})
    assert len(events) == 1
    assert events[0]["agent"] == "Josh"
    assert events[0]["archetype"] == viz_archetypes.PULSE
    assert events[0]["verb"] == "push"


def test_codex_lowercase_tool_names_classify(any_checkout_exists):
    """Codex spells some native tools lowercase; they were being dropped."""
    assert viz_normalize.classify("read", {"file_path": "/home/p/GIT/clarp/a.py"}) == (
        "read", "repo:clarp")
    assert viz_normalize.classify("edit", {"file_path": "/tmp/x"})[0] == "write"


def test_repo_node_requires_the_checkout_to_exist(monkeypatch):
    """Live data minted `repo:null` and a half-truncated name from stray text."""
    monkeypatch.setattr(viz_normalize, "_is_checkout", lambda path: False)
    assert viz_normalize.repo_of("/home/p/GIT/null") is None
    monkeypatch.setattr(viz_normalize, "_is_checkout", lambda path: True)
    assert viz_normalize.repo_of("/home/p/GIT/clarp/x.py") == "repo:clarp"
