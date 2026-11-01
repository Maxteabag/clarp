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


def test_codex_logout_recycles_app_servers(monkeypatch):
    recycled = []
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    from lib import codex_app_server
    monkeypatch.setattr(codex_app_server, "recycle_clients", lambda: recycled.append("codex") or 0)
    monkeypatch.setattr(
        backend_auth, "_credential_metadata", lambda _backend: (False, 0))

    def fake_run(argv, timeout=8):
        if argv[-2:] == ["login", "status"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")
        if argv[-3:] == ["auth", "status", "--json"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"loggedIn": False}), stderr="")
        return SimpleNamespace(returncode=0, stdout="Logged out", stderr="")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    backend_auth.logout("codex")
    assert recycled == ["codex"]


def test_claude_logout_does_not_recycle_codex_writers(monkeypatch):
    recycled = []
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    from lib import codex_app_server
    monkeypatch.setattr(codex_app_server, "recycle_clients", lambda: recycled.append("codex") or 0)
    monkeypatch.setattr(
        backend_auth, "_credential_metadata", lambda _backend: (False, 0))

    def fake_run(argv, timeout=8):
        if argv[-3:] == ["auth", "status", "--json"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"loggedIn": False}), stderr="")
        if argv[-2:] == ["login", "status"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")
        return SimpleNamespace(returncode=0, stdout="Logged out", stderr="")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    backend_auth.logout("claude")
    assert recycled == []


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


def test_status_rows_come_from_the_registry_with_login_kind(monkeypatch):
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: None)
    rows = backend_auth.status()
    assert [row["id"] for row in rows] == ["claude", "codex"]
    assert rows[0]["login_kind"] == "cli"
    assert rows[1]["login_kind"] == "device_code"
    assert all(row["installed"] is False for row in rows)


def test_login_refuses_backends_without_a_sign_in(monkeypatch):
    import pytest
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    with pytest.raises(ValueError):
        backend_auth.start_login("opencode")
    with pytest.raises(ValueError):
        backend_auth.logout("deepseek")
    with pytest.raises(ValueError):
        backend_auth.submit_login_code("agy", "ABCD-12345")


def test_login_accepts_registry_aliases(monkeypatch):
    monkeypatch.setattr(backend_auth.shutil, "which", lambda name: f"/bin/{name}")
    calls = []

    def fake_run(argv, timeout=8):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backend_auth, "_run", fake_run)
    monkeypatch.setattr(backend_auth, "status",
                        lambda validate=True, **_kw: [{"id": "claude"}, {"id": "codex"}])
    row = backend_auth.logout("codex")
    assert row["id"] == "codex"
    assert calls[-1] == ["/bin/codex", "logout"]


def _every_harness_env(monkeypatch, tmp_path, *, missing=(), grok_out="Default model: grok-4.6\n",
                       agy_rc=0):
    from lib import harness_providers
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "cache" / "opencode").mkdir(parents=True)
    (tmp_path / "cache" / "opencode" / "models.json").write_text(json.dumps({
        "deepseek": {"name": "DeepSeek", "env": ["DEEPSEEK_API_KEY"],
                     "models": {"deepseek-v4-pro": {}}},
        "groq": {"name": "Groq", "env": ["GROQ_API_KEY"], "models": {"llama": {}}}}))
    (tmp_path / "data" / "opencode").mkdir(parents=True)
    (tmp_path / "data" / "opencode" / "auth.json").write_text(json.dumps({
        "groq": {"type": "api", "key": "sk-fake-backend-auth-row"}}))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "google_accounts.json").write_text(
        json.dumps({"active": "person@example.com", "old": []}))
    for var in ("DEEPSEEK_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(harness_providers, "refresh_methods_in_background",
                        lambda **_kw: (_ for _ in ()).throw(AssertionError("no probe")))
    monkeypatch.setattr(backend_auth.shutil, "which",
                        lambda name: None if name in missing else f"/bin/{name}")
    monkeypatch.setattr(backend_auth, "_credential_metadata", lambda _backend: (False, 0))
    monkeypatch.setattr(backend_auth, "_validation", lambda _backend, force=False: ("valid", ""))

    def fake_run(argv, timeout=8):
        name = argv[0].rsplit("/", 1)[-1]
        if name == "grok":
            return SimpleNamespace(returncode=0, stdout=grok_out, stderr="")
        if name == "agy":
            return SimpleNamespace(returncode=agy_rc, stdout="Fetching available models...\n"
                                   + ("gemini-4\tGemini 4\n" if agy_rc == 0 else ""), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Not logged in")

    monkeypatch.setattr(backend_auth, "_run", fake_run)


def test_plain_backend_auth_keeps_its_rows(monkeypatch, tmp_path):
    _every_harness_env(monkeypatch, tmp_path)
    assert [row["id"] for row in backend_auth.status()] == ["claude", "codex"]


def test_every_harness_lists_each_with_what_clarp_can_say(monkeypatch, tmp_path):
    _every_harness_env(monkeypatch, tmp_path, missing=("muse",))

    rows = {row["id"]: row for row in backend_auth.status(every_harness=True)}

    assert list(rows) == ["claude", "codex", "agy", "grok", "opencode", "deepseek", "muse"]
    assert rows["grok"]["state"] == "valid" and rows["grok"]["can_login"] is True
    assert rows["grok"]["can_logout"] is True
    assert rows["agy"]["state"] == "valid" and rows["agy"]["can_login"] is False
    assert rows["agy"]["account"] == "person@example.com"
    assert rows["opencode"]["model_providers"] is True
    assert rows["opencode"]["state"] == "valid" and rows["opencode"]["account"] == "Groq"
    assert rows["opencode"]["model_providers_signed_in"] == [
        {"id": "groq", "name": "Groq", "credential": "api"}]
    assert rows["deepseek"]["state"] == "signed_out"   # Groq hosts no DeepSeek model
    assert rows["muse"]["installed"] is False
    assert "sk-fake-backend-auth-row" not in json.dumps(rows)


def test_signed_out_grok_and_agy_read_as_signed_out(monkeypatch, tmp_path):
    _every_harness_env(monkeypatch, tmp_path, grok_out="You are not authenticated.\n", agy_rc=1)
    rows = {row["id"]: row for row in backend_auth.status(every_harness=True)}
    assert rows["grok"]["state"] == "signed_out" and rows["grok"]["can_logout"] is False
    assert rows["agy"]["state"] == "signed_out" and rows["agy"]["account"] == ""


def test_grok_signs_in_with_a_device_code_and_out_with_its_cli(monkeypatch, tmp_path):
    _every_harness_env(monkeypatch, tmp_path)
    driver = backend_auth._driver("grok")
    assert driver.login_argv == ("login", "--device-auth")
    calls = []
    monkeypatch.setattr(backend_auth, "_run", lambda argv, timeout=8: calls.append(argv) or
                        SimpleNamespace(returncode=0, stdout="You are not authenticated.\n", stderr=""))
    row = backend_auth.logout("grok")
    assert calls[0] == ["/bin/grok", "logout"]
    assert row["id"] == "grok" and row["state"] == "signed_out"
