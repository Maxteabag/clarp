"""Characterization of the Claude and Codex usage collectors' edges: the
Claude credentials file, an empty Claude answer, the Codex app-server probe
and its JSON-RPC reads. Pins current behaviour before S6 moves these
collectors out of backend_usage (G0)."""
from __future__ import annotations

import io
import json
import os
import subprocess

import pytest

from lib import backend_usage
from tests.patching import patch_shared


def _write_claude_credentials(tmp_path, monkeypatch, login) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": login}))


def test_claude_credentials_give_token_expiry_and_rate_limit_tier(tmp_path, monkeypatch):
    _write_claude_credentials(tmp_path, monkeypatch, {
        "accessToken": " tok ", "expiresAt": 4_000_000_000_000,
        "rateLimitTier": "max_20x", "subscriptionType": "max"})
    assert backend_usage._read_claude_auth() == ("tok", 4_000_000_000_000, "max_20x")


def test_claude_credentials_fall_back_to_subscription_type(tmp_path, monkeypatch):
    _write_claude_credentials(tmp_path, monkeypatch, {
        "accessToken": "tok", "subscriptionType": "pro"})
    assert backend_usage._read_claude_auth() == ("tok", 0, "pro")


@pytest.mark.parametrize("login, message", [
    ("not-a-dict", "Claude OAuth credentials unavailable"),
    ({"accessToken": "  "}, "Claude OAuth access token unavailable"),
    ({"accessToken": "tok", "expiresAt": 1_000}, "Claude sign-in expired"),
])
def test_unusable_claude_credentials_raise(tmp_path, monkeypatch, login, message):
    _write_claude_credentials(tmp_path, monkeypatch, login)
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 2_000)
    with pytest.raises(RuntimeError, match=message):
        backend_usage._read_claude_auth()


def test_claude_answer_without_usable_windows_fails_and_writes_nothing(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return json.dumps({"five_hour": {"utilization": None}}).encode()

    patch_shared(monkeypatch, backend_usage, "_read_claude_auth", lambda: ("tok", 0, "pro"))
    monkeypatch.setattr(backend_usage.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response())
    with pytest.raises(RuntimeError, match="no usable limits"):
        backend_usage.fetch_claude_usage()
    assert backend_usage._row(backend_usage.CLAUDE) is None


class _PipeProcess:
    """A stand-in for the app-server process: stdin collects what was sent,
    stdout is a real pipe so select() works on it."""

    def __init__(self, lines: list[dict | str]):
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "w") as writer:
            for line in lines:
                writer.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        self.stdout = os.fdopen(read_fd)
        self.stdin = io.StringIO()

    def sent(self) -> list[dict]:
        return [json.loads(line) for line in self.stdin.getvalue().splitlines()]


def test_codex_rpc_skips_other_ids_and_returns_the_matching_reply():
    proc = _PipeProcess([{"method": "note"}, {"id": 9, "result": "other"},
                         {"id": 4, "result": {"ok": True}}])
    reply = backend_usage._codex_rpc(proc, 4, "account/read", {"x": 1}, timeout=2)
    assert reply == {"id": 4, "result": {"ok": True}}
    assert proc.sent() == [{"id": 4, "method": "account/read", "params": {"x": 1}}]


def test_codex_rpc_end_of_output_is_a_timeout_error():
    proc = _PipeProcess([{"id": 1, "result": {}}])
    with pytest.raises(RuntimeError, match="timed out during initialize"):
        backend_usage._codex_rpc(proc, 2, "initialize", timeout=2)
    assert proc.sent() == [{"id": 2, "method": "initialize", "params": {}}]


class _FakeAppServer:
    """Popen stand-in recording the argv; its JSON-RPC is scripted through
    the patched _codex_rpc."""

    def __init__(self, calls: list, argv: list[str], *, hang: bool = False):
        self.argv = argv
        self.stdin = io.StringIO()
        self.hang = hang
        self.events: list[str] = []
        calls.append(self)

    def terminate(self):
        self.events.append("terminate")

    def wait(self, timeout=None):
        self.events.append("wait")
        if self.hang:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return 0

    def kill(self):
        self.events.append("kill")


def _script_app_server(monkeypatch, replies, *, hang=False):
    calls: list[_FakeAppServer] = []
    monkeypatch.setattr(backend_usage.subprocess, "Popen",
                        lambda argv, **_kwargs: _FakeAppServer(calls, argv, hang=hang))
    replies = list(replies)

    def rpc(proc, request_id, method, params=None, timeout=5.0):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    patch_shared(monkeypatch, backend_usage, "_codex_rpc", rpc)
    captured = []
    patch_shared(monkeypatch, backend_usage, "capture_codex_rate_limits",
                 lambda limits, **kwargs: captured.append((limits, kwargs)) or {"captured": True})
    return calls, captured


def test_app_server_probe_reads_rate_limits_once_initialized(monkeypatch):
    calls, captured = _script_app_server(monkeypatch, [
        {"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"primary": {}}}}])
    assert backend_usage.fetch_codex_usage_app_server() == {"captured": True}
    assert [proc.argv for proc in calls] == [["codex", "-s", "read-only", "-a", "never", "app-server"]]
    assert json.loads(calls[0].stdin.getvalue()) == {"method": "initialized", "params": {}}
    assert captured == [({"primary": {}}, {"source_detail": "account/rateLimits/read"})]
    assert calls[0].events == ["terminate", "wait"]


def test_app_server_probe_tries_the_next_approval_spelling_on_an_invalid_value(monkeypatch):
    calls, captured = _script_app_server(monkeypatch, [
        RuntimeError("invalid value 'never' for '--ask-for-approval'"),
        RuntimeError("approval policy unknown"),
        {"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"p": 1}}}])
    assert backend_usage.fetch_codex_usage_app_server() == {"captured": True}
    assert [proc.argv for proc in calls] == [
        ["codex", "-s", "read-only", "-a", "never", "app-server"],
        ["codex", "-s", "read-only", "-a", "on-request", "app-server"],
        ["codex", "-s", "read-only", "app-server"],
    ]
    assert len(captured) == 1


def test_app_server_probe_stops_at_an_unrelated_error_and_kills_a_hung_process(monkeypatch):
    calls, captured = _script_app_server(monkeypatch, [
        {"id": 1, "result": {}}, {"id": 2, "result": {}}], hang=True)
    with pytest.raises(RuntimeError, match="returned no rate limits"):
        backend_usage.fetch_codex_usage_app_server()
    assert len(calls) == 1
    assert calls[0].events == ["terminate", "wait", "kill"]
    assert captured == []


def test_app_server_probe_reports_a_missing_cli(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("codex not found")

    monkeypatch.setattr(backend_usage.subprocess, "Popen", missing)
    with pytest.raises(RuntimeError, match="codex not found"):
        backend_usage.fetch_codex_usage_app_server()
