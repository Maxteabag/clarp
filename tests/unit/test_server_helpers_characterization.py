"""Characterization of server.py module helpers the S3 [helpers] step moves
(route-registry.md section 11), in process: no server is started. Pins the
branches the full suite did not reach (G0 per moved function)."""
from __future__ import annotations

import pytest

from tests.integration.test_server_di import server_module  # noqa: F401 - the loaded server.py


@pytest.mark.parametrize("raw, expected", [
    (None, None), ("", None), ("12", 12), ("7.9", 7), ("x", None), ("nan", None)])
def test_header_int(raw, expected):
    assert server_module._header_int(raw) == expected


def test_clip_session_and_trace_without_a_clip_or_a_database(monkeypatch):
    assert server_module._clip_session_and_trace(None) == (None, None)
    assert server_module._clip_session_and_trace(987654) == (None, None)

    class Broken:
        def execute(self, *args):
            raise RuntimeError("database unavailable")
    monkeypatch.setattr(server_module.db, "conn", lambda: Broken())
    assert server_module._clip_session_and_trace(1) == (None, None)


def test_note_outdated_client_emits_once_per_platform_and_build(monkeypatch):
    emitted = []
    monkeypatch.setattr(server_module.eventlog, "emit",
                        lambda *args, **kwargs: emitted.append((args, kwargs)))
    monkeypatch.setattr(server_module, "_OUTDATED_CLIENTS_NOTED", set())
    client = {"platform": "ios", "build": "9", "contract": 1, "reason": "old"}
    server_module._note_outdated_client(client)
    server_module._note_outdated_client(dict(client))
    server_module._note_outdated_client({**client, "build": "10"})
    assert [a[1] for a, _ in emitted] == ["clientContractOutdated", "clientContractOutdated"]
    assert emitted[0][1]["detail"] == {"platform": "ios", "build": "9", "client_contract": 1,
                                       "reason": "old"}


def test_voice_metrics_failure_is_logged_and_never_raised(monkeypatch):
    logged = []

    def analyze(audio, ctype):
        raise ValueError("not audio")
    monkeypatch.setattr(server_module.audio_metrics, "analyze", analyze)
    monkeypatch.setattr(server_module, "log_exception",
                        lambda event, exc, detail="": logged.append((event, str(exc), detail)))

    class Inline:
        def __init__(self, target, name, daemon):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(server_module.threading, "Thread", Inline)
    server_module._spawn_voice_metrics(b"x", "audio/wav", session="s", trace_id="t",
                                       utterance_id="u1", transcript="hi")
    assert logged == [("voiceMetricsFail", "not audio", "u1")]
