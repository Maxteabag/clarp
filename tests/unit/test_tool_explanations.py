"""Shared narration never runs a tool or rewrites the transcript."""
import threading
import time

import pytest

from lib.tool_explanations import ToolExplanations, normalize_activity


@pytest.fixture(autouse=True)
def configured_explainer():
    from lib import janitor_builtins
    janitor_builtins.ensure_builtins(cwd="/tmp")


def test_refined_prompts_match_user_selected_lab_and_keep_low(monkeypatch):
    import hashlib
    from pathlib import Path
    from lib import tool_explanations as module
    expected = {
        1: '6affcc6f06e4a2362da99a56d37c2cc83f402e2c12bdf3ac31fb95f361f5d898',
        2: 'da2571dc9fcc80e1ec34e390f9b8f5e2f7c6a0b989ab8c61d85905a1259d3d94',
        3: 'e26fa022c6f04baf94fe943fe56e285ba4d4b6b7b4e03b1f6b9ace6eb5227507',
        4: '79b52c28db2bb285ba1410e2b851119d3fe93c6eaeb91743a9f1429a9fe55463',
    }
    class Captured(Exception): pass
    captured=[]
    def spawn(args, **kwargs):
        root=Path(kwargs['cwd'])
        captured.append(root.joinpath('instructions.txt').read_text())
        assert args[args.index('--model')+1]=='gpt-5.3-codex-spark'
        assert 'model_reasoning_effort="low"' in args
        assert '--ignore-user-config' in args and '--ignore-rules' in args
        assert args[args.index('--sandbox')+1]=='read-only'
        raise Captured()
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    with ToolExplanations() as service:
        for level in expected:
            run = module.janitor_builtins.begin_run('tool-explainer', f'prompt-{level}')
            with pytest.raises(Captured): service._run_codex(level,[{'id':'1','activity':{'command':'ls'}}], run=run)
            module.janitor_builtins.complete_run(run['run_id'], result={'summary':'Verified audience prompt'})
    for level,text in enumerate(captured,1):
        assert hashlib.sha256(text.encode()).hexdigest()==expected[level]


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
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert script_evidence({"command": f"python {script}"}, str(workspace))[0]["file"] == "search.py"
    link = tmp_path / "link.py"
    link.symlink_to(script)
    assert script_evidence({"command": "python link.py"}, str(tmp_path)) == []
    script.write_bytes(b"x" * 65537)
    assert script_evidence({"command": "python search.py"}, str(tmp_path)) == []


def test_shutdown_does_not_accept_or_restart_jobs():
    service = ToolExplanations(translate=lambda *_: pytest.fail("closed worker"))
    service.close()
    assert service.request(3, [{"id": "1", "activity": {}}])["items"][0]["reason"] == "service_stopping"


def test_viewport_release_respects_other_view_and_late_request():
    with ToolExplanations(translate=lambda *_: {}, debounce=5) as service:
        def request(owner):
            return service.request(3, [{"id":"1", "demand_id":owner, "activity":{"command":"ls"}}])
        request('desktop-view')
        request('phone-view')
        service.request(3, [], release=['desktop-view'])
        from lib.db import conn
        assert conn().execute("SELECT count(*) FROM tool_explanation_jobs WHERE status='queued'").fetchone()[0]==1
        assert request('desktop-view')['items'][0]['status']=='cancelled'
        service.request(3, [], release=['phone-view'])
        assert conn().execute("SELECT count(*) FROM tool_explanation_jobs WHERE status='queued'").fetchone()[0]==0
        assert request('new-view')['items'][0]['status']=='pending'


def test_expired_viewport_demand_drops_queued_not_legacy_work():
    with ToolExplanations(translate=lambda *_: {}, debounce=5) as service:
        service.request(3,[{'id':'1','demand_id':'view','activity':{'command':'ls'}}])
        service.request(3,[{'id':'2','activity':{'command':'pwd'}}])
        from lib import tool_explanation_queue as queue, tool_explanation_cache as cache
        with queue.transaction() as db:
            queue._prune(db,cache.now_ms()+6000)
            rows=db.execute("SELECT activity_json FROM tool_explanation_jobs WHERE status='queued'").fetchall()
            assert len(rows)==1
            assert 'pwd' in rows[0][0]


def test_releasing_running_activity_keeps_completed_cache():
    entered=threading.Event()
    finish=threading.Event()
    def translate(level, items):
        entered.set()
        assert finish.wait(2)
        return {i['id']:'List the files.' for i in items}
    with ToolExplanations(translate=translate,debounce=.001) as service:
        activity={'id':'1','demand_id':'view','activity':{'command':'ls'}}
        service.request(3,[activity])
        assert entered.wait(2)
        service.request(3,[],release=['view'])
        finish.set()
        result=wait_ready(service)
        assert result['status']=='ready'


def test_failure_can_be_retried_after_cooldown():
    calls = []
    def recover(level, items):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary")
        return {i["id"]: "List the files." for i in items}
    with ToolExplanations(translate=recover, debounce=.001, failure_ttl=.02) as service:
        assert wait_ready(service)["status"] == "failed"
        time.sleep(.03)
        assert wait_ready(service)["status"] == "ready"
