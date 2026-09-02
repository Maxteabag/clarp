"""Background (silent) sync pushes — P7 server half. Hints only, budgeted."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import apns, config  # noqa: E402


class _Resp:
    status_code = 200
    def json(self):
        return {}


class _FakeClient:
    def __init__(self, calls):
        self.calls = calls
    def post(self, url, headers=None, content=None):
        self.calls.append({"url": url, "headers": headers, "content": content})
        return _Resp()
    def close(self):
        pass


def _cfg(tmp_path, monkeypatch, background_sync: bool):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        '[apns]\nkey_path = "/nonexistent.p8"\nkey_id = "K"\nteam_id = "T"\n'
        f'background_sync = {"true" if background_sync else "false"}\n')
    config.reset_cache_for_tests()
    cfg = config.load(cfgfile)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(apns, "_auth_jwt", lambda cfg: "jwt")
    monkeypatch.setattr(apns, "active_tokens", lambda: [{"token": "tok1", "environment": "production"}])
    return cfg


def test_background_sync_uses_background_push_type_and_priority_5(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, True)
    apns._reset_background_budget()
    apns._reset_pooled_client()
    calls = []
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(calls))
    summary = apns.send_background_sync("mike", "a1")
    assert summary["sent"] == 1
    hdr = calls[0]["headers"]
    assert hdr["apns-push-type"] == "background"
    assert hdr["apns-priority"] == "5"
    assert hdr["apns-collapse-id"] == "sync-mike"
    import json
    body = json.loads(calls[0]["content"])
    assert body["aps"] == {"content-available": 1}
    assert body["kind"] == "sync" and body["session"] == "mike"


def test_background_sync_is_off_by_default(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, False)
    apns._reset_pooled_client()
    calls = []
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(calls))
    assert apns.send_background_sync("mike", "a1")["sent"] == 0
    assert calls == []


def test_background_budget_caps_per_hour_and_spaces_per_session():
    apns._reset_background_budget()
    assert apns._background_budget_allows("a", now=0.0)
    assert not apns._background_budget_allows("a", now=60.0), "same session within 10 min"
    assert apns._background_budget_allows("b", now=60.0)
    assert apns._background_budget_allows("c", now=120.0)
    assert not apns._background_budget_allows("d", now=180.0), "4th push within the hour"
    assert apns._background_budget_allows("d", now=3700.0), "window slid; budget refreshed"


def test_turn_done_without_alert_falls_back_to_background_sync(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, True)
    apns._reset_background_budget()
    apns._reset_pooled_client()
    calls = []
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(calls))
    from lib import user_notifications
    monkeypatch.setattr(user_notifications, "classify_completed_turn",
                        lambda **kw: {"push": False, "notify": False, "session": kw["session"]})
    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=1)
    assert summary["sent"] == 1
    assert calls[0]["headers"]["apns-push-type"] == "background"
