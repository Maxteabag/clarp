"""Shared narration never runs a tool or rewrites the transcript."""
import threading
import time

import pytest

from lib.tool_explanations import ToolExplanations, normalize_activity


def wait_ready(service, level=3):
    for _ in range(100):
        result = service.request(level, [{"id": "phone", "activity": {"command": "ls"}}])
        if result["items"][0]["status"] != "pending":
            return result["items"][0]
        time.sleep(.01)
    pytest.fail("worker did not finish")


def test_clients_share_work_but_audiences_do_not():
    calls = []
    gate = threading.Event()

    def translate(level, items):
        calls.append((level, items))
        gate.wait(2)
        return {item["id"]: "List the files." for item in items}

    with ToolExplanations(translate=translate, debounce=.001) as service:
        for client in ["desktop", "phone"]:
            assert service.request(3, [{"id": client, "activity": {"command": "ls"}}])["items"][0]["status"] == "pending"
        gate.set()
        assert wait_ready(service)["text"] == "List the files."
        assert len(calls) == 1
        assert calls[0][1][0]["id"] == "1"
        assert wait_ready(service, 4)["status"] == "ready"
        assert len(calls) == 2


def test_developer_never_invokes_model_and_rejects_invalid_input():
    with ToolExplanations(translate=lambda *_: pytest.fail("must not run")) as service:
        assert service.request(0, [{"id": "1", "activity": {"command": "ls"}}])["items"][0]["status"] == "disabled"
        for level in [-1, 5, True, "3"]:
            with pytest.raises(ValueError):
                service.request(level, [])
        with pytest.raises(ValueError):
            service.request(3, [{"id": "x", "activity": {}}] * 9)


def test_failure_is_explicit_not_fabricated_or_retried_by_polling():
    calls = []

    def broken(*args):
        calls.append(1)
        raise RuntimeError("secret command must not escape")

    with ToolExplanations(translate=broken, debounce=.001) as service:
        assert wait_ready(service) == {"id": "phone", "status": "failed", "reason": "translator_failed"}
        assert wait_ready(service)["status"] == "failed"
        assert len(calls) == 1


def test_payload_excludes_results_secrets_and_untrusted_script_excerpts():
    result = normalize_activity({"command": "curl --token=secret-value", "result": "private output", "scripts": [{"source_excerpt": "secret"}]})
    assert "result" not in result and "scripts" not in result
    assert "secret-value" not in result["command"]


def test_ios_empty_optionals_share_the_desktop_cache_identity():
    assert normalize_activity({"kind": "command", "summary": "ls"}) == normalize_activity({
        "kind": "command", "name": "", "summary": "ls", "input": {}, "operations": []})


def test_script_evidence_is_bounded_and_rejects_symlinks(tmp_path):
    from lib.tool_explanations import script_evidence
    script = tmp_path / "search.py"
    script.write_text("print('search catalogue')")
    assert script_evidence({"command": "python search.py"}, str(tmp_path))[0]["source_excerpt"] == script.read_text()
    link = tmp_path / "link.py"
    link.symlink_to(script)
    assert script_evidence({"command": "python link.py"}, str(tmp_path)) == []
    script.write_bytes(b"x" * 65537)
    assert script_evidence({"command": "python search.py"}, str(tmp_path)) == []


def test_shutdown_does_not_accept_or_restart_jobs():
    service = ToolExplanations(translate=lambda *_: pytest.fail("closed worker"))
    service.close()
    assert service.request(3, [{"id": "1", "activity": {}}])["items"][0]["reason"] == "service_stopping"
