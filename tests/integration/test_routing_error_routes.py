"""An error stored before R82 (a routing CLI's timeout, its command line in its words) is answered
without its command line by both routes that list decisions; the row itself is not rewritten."""
from __future__ import annotations

import json

from lib import db
from tests.integration.test_attention_questions_http import _request, host, server_module  # noqa: F401

MARKER = "PROMPT-MARKER-6f1c"
OLD = f"Command '['opencode', 'run', '--format', 'json', '{MARKER} words']' timed out after 30.0 seconds"


def test_both_decision_listings_answer_an_old_timeout_without_its_command_line(host):
    db.conn().execute(
        "INSERT INTO orchestrator_decisions (trace_id, utterance, requested_session, hands_free, enabled, "
        "provider, model, effort, latency_ms, decision_kind, target_session, final_action, error, created_at) "
        "VALUES ('t', 'u', 'theo', 1, 1, 'opencode', '', '', 0, 'error', 'theo', 'ignored', ?, 1)", (OLD,))
    for path in ("/orchestrator/decisions", "/orchestrator/settings"):
        code, body = _request(host, path)
        assert code == 200, (path, body)
        assert MARKER not in json.dumps(body), path
        assert "opencode timed out after 30 s" in json.dumps(body), path
    assert db.conn().execute("SELECT error FROM orchestrator_decisions").fetchone()[0] == OLD
