from __future__ import annotations

import json
from types import SimpleNamespace

from lib import backend_auth


def test_status_uses_cli_owned_auth_state(monkeypatch):
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        backend_auth, "_credential_metadata", lambda _backend: (True, 9_999_999_999_999))
    monkeypatch.setattr(
        backend_auth, "_validation", lambda _backend, force=False: ("valid", ""))

    def fake_run(argv, timeout=8):
        if argv[0].endswith("claude"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "loggedIn": True, "email": "person@example.com",
                "authMethod": "claude.ai"}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="Logged in using ChatGPT\n")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    rows = {row["id"]: row for row in backend_auth.status()}
    assert rows["claude"]["account"] == "person@example.com"
    assert rows["claude"]["logged_in"] is True
    assert rows["codex"]["method"] == "ChatGPT"
    assert rows["codex"]["logged_in"] is True
    assert rows["codex"]["state"] == "valid"


def test_status_rejects_expired_claude_metadata(monkeypatch):
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        backend_auth, "_credential_metadata",
        lambda backend: (True, 1) if backend == "claude" else (False, 0))

    def fake_run(argv, timeout=8):
        if argv[0].endswith("claude"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "loggedIn": True, "email": "expired@example.com",
                "authMethod": "claude.ai"}), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    rows = {row["id"]: row for row in backend_auth.status()}
    assert rows["claude"]["logged_in"] is False
    assert rows["claude"]["state"] == "expired"
    assert rows["claude"]["can_logout"] is True


def test_status_surfaces_codex_configuration_failure(monkeypatch):
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        backend_auth, "_credential_metadata",
        lambda backend: (True, 9_999_999_999_999) if backend == "codex" else (False, 0))

    def fake_run(argv, timeout=8):
        if argv[0].endswith("claude"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="Error loading configuration: config.toml:1:9")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    row = {value["id"]: value for value in backend_auth.status()}["codex"]
    assert row["logged_in"] is False
    assert row["state"] == "error"
    assert "configuration" in row["error"]


def test_status_reports_missing_cli(monkeypatch):
    monkeypatch.setattr(backend_auth.shutil, "which", lambda _name: None)
    assert all(not row["installed"] for row in backend_auth.status())


def test_logout_uses_cli_owned_commands_and_clears_task(monkeypatch):
    commands = []
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(backend_auth, "_tasks", {
        "claude": backend_auth._task_value("claude", "failed", "old")})
    monkeypatch.setattr(backend_auth, "_validation_cache", {
        "claude": (1.0, "invalid", "old")})
    monkeypatch.setattr(
        backend_auth, "_credential_metadata", lambda _backend: (False, 0))

    def fake_run(argv, timeout=8):
        commands.append(argv)
        if argv[-2:] == ["auth", "status"]:
            raise AssertionError("unexpected unstructured Claude status")
        if argv[-3:] == ["auth", "status", "--json"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"loggedIn": False}), stderr="")
        if argv[-2:] == ["login", "status"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")
        return SimpleNamespace(returncode=0, stdout="Logged out", stderr="")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    result = backend_auth.logout("claude")
    assert commands[0] == ["/bin/claude", "auth", "logout"]
    assert result["state"] == "signed_out"
    assert "claude" not in backend_auth._tasks
    assert "claude" not in backend_auth._validation_cache


def test_device_login_output_is_structured_and_ansi_free():
    value = backend_auth._task_value(
        "codex", "running",
        "\x1b[90mOpen https://auth.openai.com/codex/device\x1b[0m\n"
        "Enter \x1b[94mAWM1-RJNOD\x1b[0m",
        started_at=1_000,
    )
    assert value["verification_url"] == "https://auth.openai.com/codex/device"
    assert value["user_code"] == "AWM1-RJNOD"
    assert "\x1b" not in value["output"]
    assert value["expires_at"] == 601_000


def test_task_defaults_include_structured_fields(monkeypatch):
    monkeypatch.setattr(backend_auth, "_tasks", {})
    value = backend_auth.task("codex")
    assert value["status"] == "idle"
    assert value["verification_url"] == ""
    assert value["user_code"] == ""


def test_login_output_strips_osc_hyperlinks_and_controls():
    value = backend_auth._task_value(
        "codex", "running",
        "\x1b]8;;https://auth.openai.com/codex/device\x1b\\"
        "Open sign-in\x1b]8;;\x1b\\\x00",
    )
    assert value["output"] == "Open sign-in"
    assert "\x1b" not in value["output"]
    assert "\x00" not in value["output"]


def test_login_output_hides_incomplete_streaming_osc():
    value = backend_auth._task_value(
        "codex", "running",
        "Visible\n\x1b]8;;https://auth.openai.com/not-finished",
    )
    assert value["output"] == "Visible"
    assert value["verification_url"] == ""


def test_stream_buffer_preserves_token_prefix_until_publish_redaction():
    value = "x" * 11_999
    for character in "sk-abcdefghijklmnop":
        value = backend_auth._append_output(value, character)
    published = backend_auth._task_value("codex", "running", value)["output"]
    assert "abcdefghijklmnop" not in published
    assert "[redacted]" in published


def test_stream_buffer_keeps_latest_device_instructions():
    value = "x" * backend_auth._RAW_OUTPUT_LIMIT
    suffix = "\nVisit https://example.com/auth and enter ABCD-12345"
    for character in suffix:
        value = backend_auth._append_output(value, character)
    assert len(value) == backend_auth._RAW_OUTPUT_LIMIT
    assert suffix in value


_CLAUDE_PROMPT = (
    "Opening browser to sign in\n"
    "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true\n"
    "Paste code here if prompted > "
)


def test_claude_login_asks_for_the_pasted_code():
    value = backend_auth._task_value(
        "claude", "running", _CLAUDE_PROMPT, started_at=1_000)
    assert value["awaiting_code"] is True
    assert value["code_submitted"] is False
    assert value["verification_url"].startswith("https://claude.com/cai/oauth/authorize")
    # A browser-code sign-in has no device code, but it still expires.
    assert value["expires_at"] == 601_000


def test_answered_prompt_stops_asking_for_the_same_code():
    answered = backend_auth._task_value("claude", "running", _CLAUDE_PROMPT)["output"]
    value = backend_auth._task_value(
        "claude", "running", _CLAUDE_PROMPT, submitted_output=answered)
    assert value["awaiting_code"] is False
    assert value["code_submitted"] is True

    # A second prompt after a rejected code re-arms the request.
    again = backend_auth._task_value(
        "claude", "running", _CLAUDE_PROMPT + "\nInvalid code.\nPaste code here > ",
        submitted_output=answered)
    assert again["awaiting_code"] is True


def test_idle_task_is_not_awaiting_a_code(monkeypatch):
    monkeypatch.setattr(backend_auth, "_tasks", {})
    assert backend_auth.task("claude")["awaiting_code"] is False


class _FakeStdin:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, value):
        self.written.append(value)

    def flush(self):
        pass


def _running_claude_task(monkeypatch):
    stdin = _FakeStdin()
    monkeypatch.setattr(backend_auth, "_tasks", {
        "claude": backend_auth._task_value(
            "claude", "running", _CLAUDE_PROMPT, started_at=1_000)})
    monkeypatch.setattr(backend_auth, "_processes", {
        "claude": SimpleNamespace(stdin=stdin)})
    monkeypatch.setattr(backend_auth, "_code_submitted", {})
    return stdin


def test_submit_login_code_reaches_the_cli_stdin(monkeypatch):
    stdin = _running_claude_task(monkeypatch)
    result = backend_auth.submit_login_code("claude", "  abc123#state  ")
    assert stdin.written == ["abc123#state\n"]
    assert result["awaiting_code"] is False
    assert result["code_submitted"] is True


def test_submit_login_code_refuses_a_second_paste(monkeypatch):
    _running_claude_task(monkeypatch)
    backend_auth.submit_login_code("claude", "abc123#state")
    try:
        backend_auth.submit_login_code("claude", "abc123#state")
    except RuntimeError as exc:
        assert "not asking for a code" in str(exc)
    else:
        raise AssertionError("expected the second paste to be rejected")


def test_submit_login_code_rejects_malformed_and_idle_input(monkeypatch):
    _running_claude_task(monkeypatch)
    for bad in ("", "   ", "abc\nrm -rf /", "x" * 600):
        try:
            backend_auth.submit_login_code("claude", bad)
        except ValueError:
            continue
        raise AssertionError(f"expected {bad[:12]!r} to be rejected")

    monkeypatch.setattr(backend_auth, "_tasks", {})
    monkeypatch.setattr(backend_auth, "_processes", {})
    try:
        backend_auth.submit_login_code("claude", "abc123#state")
    except RuntimeError as exc:
        assert "No sign-in is waiting" in str(exc)
    else:
        raise AssertionError("expected an idle backend to be rejected")


def test_failure_summary_drops_the_unanswered_stdin_prompt():
    summary = backend_auth._failure_summary(
        _CLAUDE_PROMPT + "Login failed: Request failed with status code 400")
    assert summary == "Login failed: Request failed with status code 400"
