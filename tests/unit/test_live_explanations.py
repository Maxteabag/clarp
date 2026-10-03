"""Tool explanations are a Host setting (docs/live-items.md §6): on, each new
live tool item gets its plain-language explanation patched in; off, the
explainer is never asked and items carry only the raw command."""
from __future__ import annotations

import pytest
import threading

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
