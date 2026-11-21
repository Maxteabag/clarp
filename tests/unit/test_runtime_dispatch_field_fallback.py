"""A Host newer than its runtime (installed without restarting the runtime):
the runtime refuses the user's-word fields as unknown, so the bridge sends the
command again without them; the item queues in arrival order (issue 481)."""
from __future__ import annotations

from lib.runtime_bridge import RuntimeClient, _refused_droppable
from lib.turn_command import DispatchCommand


def _client(answers):
    client = RuntimeClient.__new__(RuntimeClient)
    sent = []

    def request(method, params=None, **_kw):
        sent.append((method, dict(params or {})))
        return answers.pop(0)
    client._request = request
    return client, sent


OK = {"ok": True, "result": {"session": "mike", "backend": "claude", "queued": True}}


def test_an_older_runtime_refusing_the_new_fields_gets_the_command_without_them():
    client, sent = _client([
        {"ok": False, "status": 400, "error": "unknown dispatch fields: taken, user_word"}, OK])
    command = DispatchCommand(text="answer", requested_session="mike", origin="automation",
                              user_word=True, taken=True)
    result = client.dispatch_command(command)
    assert result.queued and result.session == "mike"
    assert [m for m, _ in sent] == ["dispatch", "dispatch"]
    assert sent[0][1]["user_word"] is True and sent[0][1]["taken"] is True
    assert "user_word" not in sent[1][1] and "taken" not in sent[1][1]
    assert sent[1][1]["text"] == "answer" and sent[1][1]["origin"] == "automation"


def test_any_other_refusal_is_not_retried():
    for refusal in ({"ok": False, "status": 400, "error": "unknown dispatch fields: other"},
                    {"ok": False, "status": 400, "error": "unknown dispatch fields: user_word, other"},
                    {"ok": False, "status": 409, "error": "unknown dispatch fields: user_word"},
                    {"ok": False, "status": 400, "error": "dispatch text is required"}):
        assert _refused_droppable(refusal, {"user_word": True, "other": 1},
                                  ("user_word", "taken")) == []


def test_a_command_without_the_new_fields_is_sent_once():
    client, sent = _client([OK])
    client.dispatch_command(DispatchCommand(text="hi", requested_session="mike"))
    assert len(sent) == 1 and "user_word" not in sent[0][1]
