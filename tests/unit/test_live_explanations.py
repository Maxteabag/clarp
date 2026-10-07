"""Tool explanations are a Host setting (docs/live-items.md §6): on, each new
live tool item gets its plain-language explanation patched in; off, the
explainer is never asked and items carry only the raw command."""
from __future__ import annotations

import pytest
import threading
import time

from lib import tool_explanation_settings as settings
from lib.live_explain import LiveExplainer


class FakeService:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, level, items, *, cwd=None, target_agent_id=None, **_kw):
        self.calls.append((level, items, target_agent_id))
        status = self.answers.pop(0) if self.answers else "ready"
        return {"detail_level": level, "items": [
            {"id": item["id"], "status": status,
             **({"text": "Runs the test suite once"} if status == "ready" else {})}
            for item in items]}


def _tool_event(command="npm test"):
    return {"type": "live", "agent_id": "a1", "session": "rachel", "conv": "c", "epoch": "e",
            "lseq": 1, "ops": [{"op": "upsert", "conv": "c", "id": "cl:toolu_1", "kind": "tool",
                                "rev": 1, "item": {"tool": {
                                    "name": "Bash", "call_id": "toolu_1", "category": "exec",
                                    "label": command, "command": command,
                                    "input_preview": {"command": command}}}}]}


@pytest.fixture
def started():
    """Explainers with a worker thread, closed after the test."""
    made = []

    def make(service, patches, **kwargs):
        explainer = LiveExplainer(lambda: service, lambda aid, iid, f: patches.append((aid, iid, f)),
                                  poll_interval=0.02, **kwargs)
        made.append(explainer)
        return explainer
    yield make
    for explainer in made:
        explainer.close()


def _explainer(service, patches):
    return LiveExplainer(lambda: service,
                         lambda agent_id, item_id, fields: patches.append((agent_id, item_id, fields)),
                         poll_interval=0.0, synchronous=True)


def test_the_setting_defaults_on_and_persists():
    assert settings.get() == {"enabled": True, "detail_level": 2}
    assert settings.update({"enabled": False}) == {"enabled": False, "detail_level": 2}
    assert settings.get()["enabled"] is False
    assert settings.update({"detail_level": 4})["detail_level"] == 4
    with pytest.raises(ValueError):
        settings.update({"detail_level": 9})
    with pytest.raises(ValueError):
        settings.update({"enabled": "yes"})


def test_an_enabled_setting_explains_each_new_tool_item():
    service, patches = FakeService(["pending", "ready"]), []
    _explainer(service, patches).observe(_tool_event())
    level, items, agent_id = service.calls[0]
    assert (level, agent_id) == (2, "a1")
    assert items[0]["activity"]["command"] == "npm test"
    assert [p[2]["tool"]["explain"]["status"] for p in patches] == ["pending", "ready"]
    assert patches[-1] == ("a1", "cl:toolu_1", {"tool": {"explain": {
        "text": "Runs the test suite once", "level": 2, "status": "ready"}}})


def test_a_disabled_setting_never_asks_the_explainer():
    settings.update({"enabled": False})
    service, patches = FakeService([]), []
    _explainer(service, patches).observe(_tool_event())
    assert service.calls == [] and patches == []


def test_an_item_is_explained_once_per_command():
    service, patches = FakeService([]), []
    explainer = _explainer(service, patches)
    explainer.observe(_tool_event())
    explainer.observe(_tool_event())
    explainer.observe(_tool_event("npm run lint"))
    assert len(service.calls) == 2


def test_later_tools_show_pending_while_an_earlier_explanation_is_waiting():
    entered, release = threading.Event(), threading.Event()
    patches = []

    class WaitingService(FakeService):
        def request(self, *args, **kwargs):
            entered.set()
            release.wait(2)
            return super().request(*args, **kwargs)

    service = WaitingService([])
    explainer = LiveExplainer(lambda: service,
                             lambda aid, iid, fields: patches.append((aid, iid, fields)))
    try:
        explainer.observe(_tool_event())
        assert entered.wait(1)
        second = _tool_event("rg -n needle file.py")
        second["ops"][0]["id"] = "cl:toolu_2"
        explainer.observe(second)
        assert any(iid == "cl:toolu_2" and fields["tool"]["explain"]["status"] == "pending"
                   for _, iid, fields in patches)
    finally:
        release.set()


def _tool(item_id, command, agent_id="a1"):
    event = _tool_event(command)
    event["agent_id"] = agent_id
    event["ops"][0]["id"] = item_id
    return event


def _turn(status, turn_id="t1", agent_id="a1"):
    return {"type": "live", "agent_id": agent_id, "ops": [{"op": "turn", "conv": "c", "turn": {
        "turn_id": turn_id, "status": status}}]}


def _final(patches):
    """The last explain status each item was given."""
    return {iid: fields["tool"]["explain"] for _, iid, fields in patches}


def _wait(predicate, timeout=10.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class PendingService:
    """Never answers; records each request's item count and releases."""

    def __init__(self):
        self.calls = []

    def request(self, level, items, *, release=None, **_kw):
        self.calls.append((len(items), list(release or [])))
        return {"items": [{"id": item["id"], "status": "pending"} for item in items]}


def test_a_busy_turn_is_explained_in_batches_not_one_model_call_per_tool(started):
    """The live Host showed count=1 per model call: tools outran the explainer."""
    from lib import agents as agents_db, janitor_builtins
    from lib.tool_explanations import ToolExplanations
    janitor_builtins.ensure_builtins(cwd="/tmp")
    agent_id = agents_db.create_agent(persona="Jasper", voice_id="v", cwd="/tmp", session="jasper", backend="claude")
    calls = []

    def translate(level, items):
        import time
        calls.append(len(items))
        time.sleep(0.3)
        return {item["id"]: f"Explains {item['activity'].get('command', '')}" for item in items}

    patches = []
    with ToolExplanations(translate=translate, debounce=.05) as service:
        explainer = started(service, patches)
        for n in range(16):
            # Tools keep arriving while a model call runs; an item already
            # asked for must still collect its answer.
            explainer.observe(_tool(f"cl:toolu_{n}", f"frobnicate-{n} --zork {n}", agent_id))
            time.sleep(0.05)
        assert _wait(lambda: len([e for e in _final(patches).values() if e["status"] == "ready"]) == 16, 20), _final(patches)
    assert sum(calls) == 16 and len(calls) <= 6, calls


def test_each_round_asks_for_the_newest_waiting_items_together(started):
    service, patches = PendingService(), []
    explainer = started(service, patches)
    for n in range(12):
        explainer.observe(_tool(f"cl:toolu_{n}", f"frobnicate-{n}"))
    assert _wait(lambda: any(count == 8 for count, _ in service.calls))
    assert max(count for count, _ in service.calls) == 8


def test_items_that_wait_too_long_settle_as_skipped_not_pending(started):
    import lib.live_explain as module
    now = [1000.0]
    service, patches = PendingService(), []
    explainer = started(service, patches, clock=lambda: now[0])
    explainer.observe(_tool("cl:toolu_1", "frobnicate"))
    assert _wait(lambda: service.calls)
    now[0] += module.MAX_WAIT_SEC + 1
    assert _wait(lambda: _final(patches)["cl:toolu_1"]["status"] == "failed")
    assert _final(patches)["cl:toolu_1"]["reason"] == "skipped"
    assert _wait(lambda: any(release for _, release in service.calls)), "the dropped demand is released"


def test_the_turn_ending_settles_every_item_after_a_short_grace(started):
    import lib.live_explain as module
    now = [1000.0]
    service, patches = PendingService(), []
    explainer = started(service, patches, clock=lambda: now[0])
    explainer.observe(_turn("running"))
    for n in range(3):
        explainer.observe(_tool(f"cl:toolu_{n}", f"frobnicate-{n}"))
    explainer.observe(_turn("completed"))
    now[0] += module.TURN_END_GRACE_SEC + 0.1
    assert _wait(lambda: all(e["status"] == "failed" and e["reason"] == "skipped"
                             for e in _final(patches).values()))
    assert len(_final(patches)) == 3


def test_a_full_backlog_skips_the_oldest_items(started):
    import lib.live_explain as module
    service, patches = PendingService(), []
    explainer = started(service, patches)
    for n in range(module.BACKLOG + 3):
        explainer.observe(_tool(f"cl:toolu_{n}", f"frobnicate-{n}"))
    final = _final(patches)
    assert [iid for iid, e in final.items() if e["status"] == "failed"] == [f"cl:toolu_{n}" for n in range(3)]
    assert final["cl:toolu_0"]["reason"] == "skipped"


def test_a_locked_database_is_retried_and_logged(monkeypatch, started):
    import sqlite3
    import lib.live_explain as module
    from lib import log as log_module
    monkeypatch.setattr(module, "LOCK_BACKOFF_SEC", (0.01,))
    logged = []
    monkeypatch.setattr(log_module, "log", lambda event, detail="": logged.append(event))

    class LockedOnce(FakeService):
        def request(self, *args, **kwargs):
            if not self.calls:
                self.calls.append(None)
                raise sqlite3.OperationalError("database is locked")
            return super().request(*args, **kwargs)

    service, patches = LockedOnce([]), []
    explainer = started(service, patches)
    explainer.observe(_tool_event())
    assert _wait(lambda: _final(patches)["cl:toolu_1"]["status"] == "ready")
    assert "liveExplainLocked" in logged


def test_any_other_error_settles_the_batch_instead_of_leaving_it_pending(monkeypatch, started):
    from lib import log as log_module
    logged = []
    monkeypatch.setattr(log_module, "log_exception", lambda event, exc, detail="": logged.append(event))

    class Broken:
        def request(self, *args, **kwargs):
            raise RuntimeError("boom")

    patches = []
    explainer = started(Broken(), patches)
    explainer.observe(_tool_event())
    assert _wait(lambda: _final(patches)["cl:toolu_1"]["status"] == "failed")
    assert logged == ["liveExplainFail"]
