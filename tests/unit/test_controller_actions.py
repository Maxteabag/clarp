"""The custom-action registry a physical controller gesture can run."""
from __future__ import annotations

import pytest

from lib import controller_actions as ca

LEAK = {
    "id": "rk-demo-leak", "title": "Demo leak", "type": "http_post",
    "url": "http://127.0.0.1:8765/api/scenario",
    "body": {"feed": "demo-cam-02", "clip": "leak"},
}


def test_actions_and_mappings_round_trip_with_a_revision():
    empty = ca.registry()
    assert empty["actions"] == [] and empty["mappings"] == []
    action = ca.create_action(LEAK, actor="test")
    assert action["id"] == "rk-demo-leak" and action["enabled"] is True
    mapping = ca.set_mapping(device="*", button="secondary", gesture="swipe-up",
                             action_id="rk-demo-leak", actor="test")
    assert mapping == {"device": "*", "button": "secondary",
                       "gesture": "swipe-up", "action_id": "rk-demo-leak"}
    state = ca.registry()
    assert [a["id"] for a in state["actions"]] == ["rk-demo-leak"]
    assert state["mappings"] == [mapping]
    assert state["revision"] > empty["revision"]


def test_duplicate_ids_unknown_types_and_bad_ids_are_refused():
    ca.create_action(LEAK, actor="test")
    with pytest.raises(ca.ActionError, match="exists"):
        ca.create_action(LEAK, actor="test")
    with pytest.raises(ca.ActionError, match="type"):
        ca.create_action({**LEAK, "id": "x", "type": "shell"}, actor="test")
    with pytest.raises(ca.ActionError, match="id"):
        ca.create_action({**LEAK, "id": "Bad Id!"}, actor="test")
    with pytest.raises(ca.ActionError, match="body"):
        ca.create_action({**LEAK, "id": "y", "body": "rm -rf"}, actor="test")


def test_http_post_urls_must_be_allow_listed():
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**LEAK, "id": "evil", "url": "https://example.com/hook"},
                         actor="test")
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**LEAK, "id": "ftp", "url": "file:///etc/passwd"}, actor="test")
    ca.create_action({**LEAK, "id": "ok", "url": "https://example.com/hook"},
                     actor="test", allowed_urls=("https://example.com/",))
    # A prefix is matched on a URL boundary, not as a raw string.
    with pytest.raises(ca.ActionError, match="allow"):
        ca.create_action({**LEAK, "id": "sneaky", "url": "https://example.com.evil.net/x"},
                         actor="test", allowed_urls=("https://example.com",))


def test_agent_prompt_needs_a_session_and_text():
    with pytest.raises(ca.ActionError, match="session"):
        ca.create_action({"id": "ask", "title": "Ask", "type": "agent_prompt",
                          "text": "hi"}, actor="test")
    action = ca.create_action({"id": "ask", "title": "Ask Koko", "type": "agent_prompt",
                               "session": "koko-5850", "text": "status?"}, actor="test")
    assert action["session"] == "koko-5850"


def test_update_keeps_unspecified_fields_and_delete_drops_its_mappings():
    ca.create_action(LEAK, actor="test")
    ca.set_mapping(device="*", button="secondary", gesture="swipe-up",
                   action_id="rk-demo-leak", actor="test")
    updated = ca.update_action("rk-demo-leak", {"title": "Leak!", "enabled": False},
                               actor="test")
    assert updated["title"] == "Leak!" and updated["enabled"] is False
    assert updated["body"] == LEAK["body"]
    assert ca.delete_action("rk-demo-leak", actor="test") == {"deleted": True, "mappings_removed": 1}
    assert ca.registry()["mappings"] == []
    with pytest.raises(ca.ActionError, match="no such action"):
        ca.update_action("rk-demo-leak", {"title": "x"}, actor="test")


def test_mappings_validate_gesture_and_action_and_clear_with_none():
    ca.create_action(LEAK, actor="test")
    with pytest.raises(ca.ActionError, match="gesture"):
        ca.set_mapping(device="*", button="primary", gesture="down",
                       action_id="rk-demo-leak", actor="test")
    with pytest.raises(ca.ActionError, match="button"):
        ca.set_mapping(device="*", button="tertiary", gesture="hold",
                       action_id="rk-demo-leak", actor="test")
    with pytest.raises(ca.ActionError, match="no such action"):
        ca.set_mapping(device="*", button="primary", gesture="hold",
                       action_id="missing", actor="test")
    ca.set_mapping(device="flic-1", button="primary", gesture="hold",
                   action_id="rk-demo-leak", actor="test")
    assert ca.set_mapping(device="flic-1", button="primary", gesture="hold",
                          action_id=None, actor="test") is None
    assert ca.registry()["mappings"] == []


def test_resolve_prefers_a_device_mapping_over_the_wildcard():
    ca.create_action(LEAK, actor="test")
    ca.create_action({**LEAK, "id": "rk-demo-reset",
                      "body": {"feed": "demo-cam-02", "clip": "normal"}}, actor="test")
    ca.set_mapping(device="*", button="secondary", gesture="swipe-up",
                   action_id="rk-demo-leak", actor="test")
    ca.set_mapping(device="flic-2", button="secondary", gesture="swipe-up",
                   action_id="rk-demo-reset", actor="test")
    assert ca.resolve("flic-1", "secondary", "swipe-up") == "rk-demo-leak"
    assert ca.resolve("flic-2", "secondary", "swipe-up") == "rk-demo-reset"
    assert ca.resolve("flic-1", "primary", "swipe-up") is None


def test_run_http_post_sends_the_stored_body_and_audits_the_result():
    ca.create_action(LEAK, actor="test")
    sent = []

    def fake_post(url, body, timeout):
        sent.append((url, body))
        return 200, '{"ok":true}'

    result = ca.run("rk-demo-leak", actor="device:abc", source={"gesture": "swipe-up"},
                    http_post=fake_post)
    assert result["ok"] is True and result["status"] == 200
    assert sent == [("http://127.0.0.1:8765/api/scenario",
                     {"feed": "demo-cam-02", "clip": "leak"})]
    audit = ca.audit_log(limit=10)
    assert audit[0]["event"] == "run" and audit[0]["action_id"] == "rk-demo-leak"
    assert audit[0]["ok"] is True and audit[0]["actor"] == "device:abc"
    assert {"create"} <= {row["event"] for row in audit}


def test_run_reports_failures_and_refuses_disabled_or_unknown_actions():
    ca.create_action(LEAK, actor="test")

    def failing(url, body, timeout):
        raise OSError("connection refused")

    result = ca.run("rk-demo-leak", actor="t", http_post=failing)
    assert result["ok"] is False and "connection refused" in result["error"]
    assert ca.run("rk-demo-leak", actor="t",
                  http_post=lambda *a: (500, "boom"))["ok"] is False
    with pytest.raises(ca.ActionError, match="no such action"):
        ca.run("nope", actor="t")
    ca.update_action("rk-demo-leak", {"enabled": False}, actor="test")
    with pytest.raises(ca.ActionError, match="disabled"):
        ca.run("rk-demo-leak", actor="t", http_post=lambda *a: (200, ""))


def test_run_rechecks_the_allow_list_at_run_time():
    ca.create_action({**LEAK, "id": "remote", "url": "https://example.com/x"},
                     actor="test", allowed_urls=("https://example.com/",))
    called = []
    result = ca.run("remote", actor="t", allowed_urls=(),
                    http_post=lambda *a: called.append(a) or (200, ""))
    assert result["ok"] is False and "allow" in result["error"] and called == []


def test_run_agent_prompt_uses_the_prompt_sender():
    ca.create_action({"id": "ask", "title": "Ask", "type": "agent_prompt",
                      "session": "koko-5850", "text": "status?"}, actor="test")
    sent = []
    result = ca.run("ask", actor="t",
                    send_prompt=lambda session, text: sent.append((session, text)) or {"ok": True})
    assert result["ok"] is True and sent == [("koko-5850", "status?")]


def test_a_repeated_request_id_runs_once():
    ca.create_action(LEAK, actor="test")
    calls = []

    def post(url, body, timeout):
        calls.append(1)
        return 200, ""

    first = ca.run("rk-demo-leak", actor="t", request_id="r1", http_post=post)
    second = ca.run("rk-demo-leak", actor="t", request_id="r1", http_post=post)
    assert calls == [1] and second == {**first, "duplicate": True}


def test_audit_log_is_bounded():
    ca.create_action(LEAK, actor="test")
    for _ in range(ca.AUDIT_KEEP + 20):
        ca.run("rk-demo-leak", actor="t", http_post=lambda *a: (200, ""))
    assert len(ca.audit_log(limit=10_000)) == ca.AUDIT_KEEP
