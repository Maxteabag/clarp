"""The cold path that authors rules. No model is called here."""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib import viz_archetypes, viz_rule_author as author  # noqa: E402

CLUSTERS = [
    {"hint": "ruff", "count": 40, "clamped": 0, "example": "ruff check server/"},
    {"hint": "kubectl", "count": 12, "clamped": 0, "example": "kubectl get pods"},
    {"hint": "gone", "count": 9, "clamped": 9, "example": "/usr/bin/bash -lc 'x"},
]


def test_clamped_only_clusters_are_not_asked_about():
    """A truncated command cannot be identified, so asking wastes the call."""
    prompt = author.build_prompt(CLUSTERS)
    assert "ruff" in prompt and "kubectl" in prompt
    assert "'gone'" not in prompt


def test_prompt_states_the_closed_vocabulary():
    prompt = author.build_prompt(CLUSTERS)
    for verb in ("review", "ops", "build"):
        assert verb in prompt
    assert "Reply with ONLY a JSON array" in prompt


def test_accepts_valid_rules_and_assigns_an_archetype():
    reply = json.dumps([
        {"exe": "ruff", "verb": "review", "kind": "toolchain", "why": "linter"},
    ])
    accepted, rejected = author.parse_proposals(reply)
    assert not rejected
    assert accepted[0]["exe"] == "ruff"
    assert accepted[0]["archetype"] == viz_archetypes.archetype_for("review")


def test_rejects_invented_vocabulary():
    reply = json.dumps([{"exe": "ruff", "verb": "sparkle", "kind": "toolchain"}])
    accepted, rejected = author.parse_proposals(reply)
    assert not accepted and "outside the vocabulary" in rejected[0]


def test_rejects_shell_shaped_executable_names():
    reply = json.dumps([{"exe": "rm -rf /", "verb": "ops", "kind": "host"}])
    accepted, rejected = author.parse_proposals(reply)
    assert not accepted and "plain executable" in rejected[0]


def test_refuses_to_overwrite_an_existing_rule():
    reply = json.dumps([{"exe": "git", "verb": "read", "kind": "file"}])
    accepted, rejected = author.parse_proposals(reply)
    assert not accepted and "already has a rule" in rejected[0]


def test_tolerates_a_fenced_or_chatty_reply():
    accepted, _ = author.parse_proposals(
        'Sure!\n```json\n[{"exe":"ruff","verb":"review","kind":"toolchain"}]\n```')
    assert accepted[0]["exe"] == "ruff"


def test_malformed_output_is_reported_not_raised():
    accepted, rejected = author.parse_proposals("I could not do that")
    assert not accepted and rejected


def test_a_failing_model_degrades_quietly():
    def boom(_prompt):
        raise RuntimeError("no model here")
    out = author.propose(CLUSTERS, model=boom)
    assert out["proposals"] == []
    assert "model call failed" in out["rejected"][0]


def test_nothing_is_asked_when_coverage_is_complete():
    assert author.propose([], model=lambda p: "[]")["asked"] == 0


def test_duplicate_proposals_in_one_reply_are_collapsed():
    reply = json.dumps([
        {"exe": "ruff", "verb": "review", "kind": "toolchain"},
        {"exe": "ruff", "verb": "build", "kind": "toolchain"},
    ])
    accepted, rejected = author.parse_proposals(reply)
    assert len(accepted) == 1 and "duplicate" in rejected[0]


def test_promoting_a_proposal_is_a_reviewable_diff():
    src = author.as_source_lines(
        [{"exe": "ruff", "verb": "review", "kind": "toolchain", "why": "linter"}])
    assert '"ruff": ("review", "toolchain"),' in src
