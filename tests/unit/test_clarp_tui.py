from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from textual.widgets import (
    Button, Input, LoadingIndicator, Select, SelectionList, Static,
    TabbedContent,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clarp_tui", ROOT / "bin/clarp-tui.py")
assert SPEC and SPEC.loader
tui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tui)


def test_tui_exposes_setup_pairing_and_provider_choices():
    async def inspect() -> None:
        app = tui.ClarpAdminApp(first_run=True)
        # Avoid running the real doctor worker during a composition test.
        app.on_mount = lambda: None
        async with app.run_test(size=(120, 50)) as pilot:
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "setup"
            assert set(app.query_one("#backends", SelectionList).selected) == {
                "claude", "codex"}
            assert app.query_one("#tts", Select).value == "cartesia"
            assert app.query_one("#tts-fallback", Select).value == "none"
            assert app.query_one("#network", Select).value == "tailscale"
            assert app.query_one("#cartesia-key", Input).password is True
            assert app.query_one("#pair-url", Input).value == ""
            assert set(app.query_one(
                "#optional-skills", SelectionList).selected) == {
                    "clarp-calendar", "clarp-location"}
            assert len(app.query_one(
                "#included-skills", SelectionList).selected) == 21
            manual_url = app.query_one("#manual-url-field")
            assert manual_url.display is False
            cartesia_key = app.query_one("#cartesia-key-field")
            eleven_key = app.query_one("#eleven-key-field")
            deepgram_key = app.query_one("#deepgram-key-field")
            assert cartesia_key.display is True
            assert eleven_key.display is False
            assert deepgram_key.display is False

            app.query_one("#network", Select).value = "manual"
            await pilot.pause()
            assert manual_url.display is True

            app.query_one("#network", Select).value = "tailscale"
            await pilot.pause()
            assert manual_url.display is False

            app.query_one("#tts", Select).value = "elevenlabs"
            await pilot.pause()
            assert cartesia_key.display is False
            assert eleven_key.display is True

            app.query_one("#tts-fallback", Select).value = "elevenlabs"
            await pilot.pause()
            assert cartesia_key.display is False
            assert eleven_key.display is True

            app.query_one("#tts", Select).value = "cartesia"
            app.query_one("#tts-fallback", Select).value = "deepgram"
            await pilot.pause()
            assert eleven_key.display is False
            assert deepgram_key.display is True

    asyncio.run(inspect())


def test_uninstalled_tui_hides_health_and_pairing_until_setup_succeeds(
        tmp_path, monkeypatch):
    share = tmp_path / "clarp-share"
    monkeypatch.setenv("CLARP_SHARE_DIR", str(share))

    async def inspect() -> None:
        calls = []
        app = tui.ClarpAdminApp(first_run=True)
        app.run_admin = lambda *args: calls.append(args)
        app.load_network_state = lambda: calls.append(("network",))
        async with app.run_test(size=(120, 50)):
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "setup"
            assert tabs.get_tab("overview").display is False
            assert tabs.get_tab("pair").display is False
            assert calls == []

            (share / "current").mkdir(parents=True)
            (share / "current/server.py").touch()
            app._begin_setup()
            button = app.query_one("#apply-setup", Button)
            assert button.disabled is True
            assert app.query_one(
                "#setup-spinner", LoadingIndicator).display is True

            app._finish_setup(True)

            assert tabs.active == "setup"
            assert tabs.get_tab("overview").display is True
            assert tabs.get_tab("pair").display is True
            assert button.disabled is False
            assert str(button.label) == "Apply changes"
            assert app.query_one(
                "#setup-spinner", LoadingIndicator).display is False
            assert "Installation complete" in str(
                app.query_one("#setup-status", Static).render())
            assert calls == [(["doctor"], "overview-log"), ("network",)]

    asyncio.run(inspect())


def test_tui_maps_independent_backend_and_skill_selections_to_setup():
    choices = {
        value: selected for _label, value, selected
        in tui.optional_skill_selections()
    }
    assert choices == {
        "clarp-code-changes": False,
        "clarp-releases": False,
        "clarp-calendar": True,
        "clarp-location": True,
        "clarp-message-watch": False,
        "clarp-whatsapp": False,
        "clarp-email": False,
    }

    async def inspect() -> None:
        submitted = []
        app = tui.ClarpAdminApp(first_run=True)
        app.on_mount = lambda: None
        app.apply_setup = lambda values: submitted.append(values)
        async with app.run_test(size=(120, 50)) as pilot:
            backends = app.query_one("#backends", SelectionList)
            backends.deselect("codex")
            skills = app.query_one("#optional-skills", SelectionList)
            skills.select("clarp-whatsapp")
            app.on_button_pressed(SimpleNamespace(
                button=app.query_one("#apply-setup", Button)))
            await pilot.pause()

        assert len(submitted) == 1
        assert submitted[0]["backend"] == "claude"
        assert set(submitted[0]["optional-skills"]) == {
            "clarp-calendar", "clarp-location", "clarp-whatsapp"}

    asyncio.run(inspect())


def test_tui_defers_network_until_after_setup_and_voice_commands():
    values = {
        "backend": "both", "toolchain": "managed",
        "transcription": "recommended", "tts": "cartesia",
        "tts-fallback": "none", "optional-skills": ["clarp-calendar"],
        "network": "manual", "public-url": "https://clarp.example.test",
    }
    setup = tui.setup_admin_command(values)
    assert "--network" not in setup
    assert setup[-2:] == ["--optional-skill", "clarp-calendar"]
    assert tui.network_admin_command(values) == [
        "network", "use", "manual", "--url", "https://clarp.example.test",
    ]


def test_installed_tui_preserves_saved_setup_choices(tmp_path, monkeypatch):
    share = tmp_path / "share"
    (share / "current").mkdir(parents=True)
    (share / "current/server.py").touch()
    config = tmp_path / "config"
    config.mkdir()
    (config / "install.json").write_text(json.dumps({
        "backend": "codex",
        "toolchain": "existing",
        "transcription": "faster-whisper:medium",
        "skills": ["clarp-email", "clarp-releases"],
    }))
    (config / "config.toml").write_text(
        '[server]\npublic_base_url = "https://clarp.example.test"\n'
        '[network]\nmode = "manual"\n'
        '[tts]\nprovider = "cartesia"\nfallback = "elevenlabs"\n'
        '[whisper]\nenabled = true\nprovider = "whisper.cpp"\n'
        'model = "small.en"\n')
    monkeypatch.setenv("CLARP_SHARE_DIR", str(share))
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(config))

    async def inspect() -> None:
        app = tui.ClarpAdminApp(first_run=False)
        app.on_mount = lambda: None
        async with app.run_test(size=(120, 50)):
            assert set(app.query_one(
                "#backends", SelectionList).selected) == {"codex"}
            assert app.query_one("#toolchain", Select).value == "existing"
            assert app.query_one("#transcription", Select).value == (
                "whisper.cpp:small.en")
            assert app.query_one("#tts", Select).value == "cartesia"
            assert app.query_one("#tts-fallback", Select).value == "elevenlabs"
            assert app.query_one("#network", Select).value == "manual"
            assert app.query_one("#public-url", Input).value == (
                "https://clarp.example.test")
            assert set(app.query_one(
                "#optional-skills", SelectionList).selected) == {
                    "clarp-email", "clarp-releases"}

    asyncio.run(inspect())


def test_tui_voice_choices_keep_an_installed_custom_adapter_selectable():
    assert {value for _label, value in tui.tts_selections("cartesia")} == {
        "cartesia", "elevenlabs", "deepgram", "none"}
    # A custom adapter is not in the built-in list, so it has to survive as the
    # current choice rather than being silently switched away from.
    custom = tui.tts_selections("custom.xtts")
    assert custom[0][1] == "custom.xtts"
    assert "legacy/custom" in custom[0][0]


def test_admin_stream_forwards_live_stdout_stderr_and_input(
        tmp_path, monkeypatch):
    script = tmp_path / "fake-admin.py"
    script.write_text(
        "import os, sys\n"
        "print('phase one', flush=True)\n"
        "print('phase two', file=sys.stderr, flush=True)\n"
        "print('input=' + sys.stdin.read().strip(), flush=True)\n"
        "print('unbuffered=' + os.environ['PYTHONUNBUFFERED'], flush=True)\n")
    monkeypatch.setattr(tui, "admin_script", lambda: script)
    lines = []

    returncode = tui.run_admin_stream(["setup"], "credential\n", lines.append)

    assert returncode == 0
    assert lines == [
        "phase one", "phase two", "input=credential", "unbuffered=1"]


def test_pairing_controls_follow_authoritative_network_state():
    async def inspect() -> None:
        app = tui.ClarpAdminApp(first_run=True)
        app.on_mount = lambda: None
        async with app.run_test(size=(120, 50)):
            url = app.query_one("#pair-url", Input)
            field = app.query_one("#pair-url-field")
            generate = app.query_one("#pair-create", Button)

            app._apply_network_state({
                "mode": "tailscale",
                "pairing_url": "https://host.example.ts.net",
                "auth_configured": True,
            })
            assert url.value == "https://host.example.ts.net"
            assert url.disabled is True
            assert field.display is True
            assert generate.disabled is False

            app._apply_network_state({
                "mode": "manual",
                "pairing_url": "https://clarp.example.com",
                "auth_configured": True,
            })
            assert url.disabled is False
            assert generate.disabled is False

            app._apply_network_state({
                "mode": "lan",
                "pairing_url": "http://host.local:7682",
                "auth_configured": True,
            })
            assert url.disabled is True
            assert generate.disabled is False

            app._apply_network_state({
                "mode": "off", "pairing_url": "",
                "auth_configured": True,
            })
            assert field.display is False
            assert generate.disabled is True

    asyncio.run(inspect())


def test_pairing_qr_opens_complete_code_in_scrollable_modal():
    uri = (
        "clarp://pair?name=Host&url=https%3A%2F%2Fhost.example.ts.net"
        "&code=clp_example&server_id=server-1&scope=full&expires_at=123")
    qr_text = tui.render_pairing_qr(uri)
    assert len(qr_text.splitlines()) > 10

    async def inspect() -> None:
        app = tui.ClarpAdminApp(first_run=True)
        app.on_mount = lambda: None
        async with app.run_test(size=(80, 30)) as pilot:
            app._present_pairing_qr(qr_text, uri, 123)
            await pilot.pause()
            assert isinstance(app.screen, tui.PairingQRScreen)
            assert str(app.screen.query_one(
                "#pair-modal-qr", Static).render()) == qr_text
            assert app.screen_stack[0].query_one(
                "#pair-show", Button).display is True

    asyncio.run(inspect())
