"""Agent helper requests without touching a running Host or credentials."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("agent_questions_cli", _ROOT / "scripts/agent_artifacts.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


OPTIONS = [{"id": "keep", "label": "Keep the current layout"},
           {"id": "change", "label": "Try the new layout", "description": "More work"}]


@pytest.fixture
def requests(monkeypatch):
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"decision_format": 2, "items": [], "count": 0}
        return {"artifact": {"artifact_id": "new", **body}}

    monkeypatch.setattr(cli, "_request", request)
    return calls


def test_old_approval_invocation_is_preserved(requests, capsys):
    assert cli.main(["cli", "decision", "theo", "Send", "Send this draft?",
                     "Yes", "No", '{"draft":"123"}']) == 0
    assert len(requests) == 1
    method, path, body = requests[0]
    assert (method, path) == ("POST", "/decisions")
    assert body["yes_label"] == "Yes" and body["no_label"] == "No"
    assert body["payload"] == {"draft": "123"}
    assert "response_type" not in body
    assert json.loads(capsys.readouterr().out)["artifact_id"] == "new"


def test_question_checks_support_and_creates_custom_answer_card(requests):
    args = ["cli", "question", "theo", "Layout", "Which layout?", json.dumps(OPTIONS),
            "--recommend", "keep", "--blocks-progress", "--priority-reason",
            "The implementation depends on this layout.", "--effort", "quick"]
    assert cli.main(args) == 0
    assert requests[0] == ("GET", "/attention?decision_format=2", None)
    body = requests[1][2]
    assert body["response_type"] == "single_choice"
    assert body["allow_custom_text"] is True
    assert body["recommended_option_id"] == "keep"
    assert body["blocks_progress"] is True
    assert body["response_effort"] == "quick"


def test_question_refuses_old_host_before_any_mutation(monkeypatch, capsys):
    calls = []

    def old_host(method, path, body=None):
        calls.append(method)
        return {"items": [], "count": 0}

    monkeypatch.setattr(cli, "_request", old_host)
    assert cli.main(["cli", "question", "theo", "Layout", "Which?", json.dumps(OPTIONS)]) == 1
    assert calls == ["GET"]
    assert "does not support native questions" in capsys.readouterr().err


def test_dry_run_has_no_network_and_preserves_optional_metadata(requests, capsys):
    assert cli.main(["cli", "question", "theo", "Layout", "Which?", json.dumps(OPTIONS),
                     "--urgency", "time_sensitive", "--priority-reason", "Deadline today",
                     "--deadline-at", "1894000000000", "--expires-at", "1895000000000",
                     "--context", "Reviewed both alternatives", "--reference", "layout-1",
                     "--payload", '{"project":"demo"}', "--dry-run"]) == 0
    assert requests == []
    request = json.loads(capsys.readouterr().out)
    assert request["method"] == "POST" and request["path"] == "/decisions"
    assert request["body"]["deadline_at"] == 1894000000000
    assert request["body"]["payload"] == {"project": "demo"}


@pytest.mark.parametrize("options", [[], OPTIONS[:1], OPTIONS * 2,
    [{"id": "a", "label": "One"}, {"id": "a", "label": "Two"}],
    [{"id": "a", "label": " "}, {"id": "b", "label": "Two"}],
    [{"id": "a", "label": "One"}, {"id": "b", "label": "Two", "rank": 1}],
    [{"id": 1, "label": "One"}, {"id": "b", "label": "Two"}],
])
def test_invalid_options_fail_without_network(options, requests):
    assert cli.main(["cli", "question", "theo", "Layout", "Which?", json.dumps(options)]) == 1
    assert requests == []


@pytest.mark.parametrize("flags", [
    ["--blocks-progress"], ["--urgency", "time_sensitive"],
    ["--recommend", "missing"], ["--effort", "urgent"],
    ["--deadline-at", "-1"], ["--payload", "[]"],
])
def test_invalid_metadata_fails_without_network(flags, requests):
    assert cli.main(["cli", "question", "theo", "Layout", "Which?", json.dumps(OPTIONS), *flags]) == 1
    assert requests == []


def test_attention_inspects_all_priorities_then_optionally_filters_session(monkeypatch, capsys):
    calls = []

    def pending(method, path, body=None):
        calls.append((method, path, body))
        return {"decision_format": 2, "count": 2,
                "items": [{"session": "other", "priority": 210}, {"session": "theo", "priority": 100}]}

    monkeypatch.setattr(cli, "_request", pending)
    assert cli.main(["cli", "attention", "--include-archived", "--session", "theo"]) == 0
    assert calls == [("GET", "/attention?decision_format=2&include_archived=1", None)]
    output = json.loads(capsys.readouterr().out)
    assert output["count"] == 1 and output["items"][0]["session"] == "theo"


def test_both_app_instructions_allow_native_questions_but_prohibit_cli_popups(monkeypatch):
    from lib import codex_runner

    monkeypatch.syspath_prepend(str(_ROOT / "plugin/hooks"))
    spec = importlib.util.spec_from_file_location("question_pwa_hook", _ROOT / "plugin/hooks/pwa_source_flag.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    for text in (codex_runner._NO_INTERACTIVE_QUESTIONS,
                 hook._build_additional_context(app_dispatched=True, voiced=False)):
        assert "clarp-agent-artifacts question" in text
        assert "supports native" in text
        assert "AskUserQuestion" in text and "request_user_input" in text
        assert "Never" in text and "self-resolve" in text
        assert "not blanket authorization" in text
