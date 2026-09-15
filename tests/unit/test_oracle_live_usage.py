import json
from types import SimpleNamespace

from lib.oracle_live import Conversation
from lib.oracle_live_usage import LiveUsage


def test_cumulative_usage_replaces_and_final_event_is_not_counted_twice():
    usage = LiveUsage()
    usage.observe({"type": "session.started", "session": {"expires_at": 1234}})
    usage.observe({"type": "session.usage.updated", "usage": {"seconds": 12}, "context_window": {"usage_ratio": .9}})
    usage.observe({"type": "session.usage.updated", "usage": {"seconds": 18}})
    assert usage.snapshot()["voice_estimate_usd"] == .015
    usage.observe({"type": "session.closed", "usage": {"seconds": 20}, "reason": "client_close"})
    assert not usage.observe({"type": "session.usage.updated", "usage": {"seconds": 19}})
    assert usage.seconds == 20 and usage.usage_final
    assert usage.snapshot()["context_ratio"] == .9
    assert usage.snapshot()["expires_at"] == 1234


def test_missing_final_usage_stays_unconfirmed_and_invalid_values_cannot_poison_meter():
    usage = LiveUsage()
    usage.observe({"type": "session.usage.updated", "usage": {"seconds": 12}})
    for value in [-1, float("nan"), float("inf"), True, "12"]:
        usage.observe({"type": "session.usage.updated", "usage": {"seconds": value}})
    usage.observe({"type": "session.closed"})
    assert usage.seconds == 12 and usage.finalized and not usage.usage_final


def test_subscription_seconds_are_not_presented_as_api_dollars():
    usage = LiveUsage("subscription")
    usage.observe({"type": "session.closed", "usage": {"seconds": 12}})
    assert usage.snapshot()["billing"] == "chatgpt_subscription"
    assert usage.snapshot()["voice_estimate_usd"] is None


def test_idle_close_is_once_and_does_not_cancel_agent_work():
    now, sent, down = [0.0], [], []
    tools = SimpleNamespace(results=lambda: [])
    c = Conversation(SimpleNamespace(send=sent.append), down.append, tools, "fixture", clock=lambda: now[0])
    try:
        now[0] = 180
        c.tick(); c.tick()
        assert down == [{"type": "oracle_v2.idle", "message": "Voice paused after inactivity. Agent work continues."}]
        assert [json.loads(raw)["type"] for raw in sent] == ["session.close"]
        assert not c.stop.is_set(), "Keep receive loop alive for final usage"
    finally:
        c.stop.set(); c.pool.shutdown()
