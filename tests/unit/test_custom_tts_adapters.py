from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from lib import config, custom_tts_adapters, tts_providers, voice_catalog


@pytest.fixture(autouse=True)
def _isolate_preview_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        custom_tts_adapters, "PREVIEW_CACHE", tmp_path / "voice-previews")


def _package(root: Path, *, adapter_id: str = "custom.test") -> Path:
    root.mkdir(parents=True)
    executable = root / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, sys, wave
request = json.load(sys.stdin)
operation = request.get("operation")
if operation == "voices":
    print(json.dumps({"ok": True, "voices": [
        {"id": "warm", "name": "Warm", "description": "Warm test voice"},
        {"id": "clear", "name": "Clear"}
    ]}))
elif operation in {"preview", "synthesize"}:
    with wave.open(request["output_path"], "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(bytes(8000))
    print(json.dumps({"ok": True}))
else:
    print(json.dumps({"ok": False, "error": "unsupported"}))
    raise SystemExit(2)
""")
    executable.chmod(0o755)
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "id": adapter_id,
        "name": "Test Adapter",
        "description": "A dynamic test provider",
        "executable": "./adapter",
        "operations": ["voices", "preview", "synthesize"],
        "audio_format": "audio/wav",
        "default_voice": "warm",
        "can_fallback": True,
    }))
    return root


def test_install_discover_and_test_complete_adapter(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    monkeypatch.setattr(custom_tts_adapters, "ROOT", tmp_path / "installed")
    custom_tts_adapters.PREVIEW_CACHE.mkdir()
    (custom_tts_adapters.PREVIEW_CACHE / "old.mp3").write_bytes(b"old")

    installed = custom_tts_adapters.install(
        source, reserved_ids=tts_providers.VALID_IDS)

    assert installed.id == "custom.test"
    assert not custom_tts_adapters.PREVIEW_CACHE.exists()
    assert custom_tts_adapters.voices(installed)[0] == {
        "id": "warm", "name": "Warm", "description": "Warm test voice"}
    result = custom_tts_adapters.test_adapter(installed)
    assert result["ok"] is True
    assert result["voices"] == 2
    assert result["preview_bytes"] > 3
    assert result["synthesis_bytes"] > 3


def test_manifest_requires_preview_and_rejects_reserved_id(tmp_path):
    source = _package(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["operations"] = ["voices", "synthesize"]
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(custom_tts_adapters.AdapterError, match="preview"):
        custom_tts_adapters.load_manifest(source, portable=True)

    manifest["operations"] = ["voices", "preview", "synthesize"]
    manifest["id"] = "cartesia"
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(custom_tts_adapters.AdapterError, match="reserved"):
        custom_tts_adapters.load_manifest(
            source, reserved_ids=tts_providers.VALID_IDS, portable=True)


def test_install_rejects_symlinked_package_content(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    (source / "escape").symlink_to(tmp_path / "outside")
    monkeypatch.setattr(custom_tts_adapters, "ROOT", tmp_path / "installed")
    with pytest.raises(custom_tts_adapters.AdapterError, match="symlink"):
        custom_tts_adapters.install(
            source, reserved_ids=tts_providers.VALID_IDS)


def test_install_is_transactional_when_preview_contract_fails(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    executable = source / "adapter"
    executable.write_text(executable.read_text().replace(
        'elif operation in {"preview", "synthesize"}:',
        'elif operation == "synthesize":'))
    executable.chmod(0o755)
    root = tmp_path / "installed"
    monkeypatch.setattr(custom_tts_adapters, "ROOT", root)

    with pytest.raises(custom_tts_adapters.AdapterError, match="adapter failed"):
        custom_tts_adapters.install(
            source, reserved_ids=tts_providers.VALID_IDS)

    assert not (root / "custom.test").exists()


def test_dynamic_adapter_flows_into_status_catalog_and_synthesis(
        tmp_path, monkeypatch):
    installed = tmp_path / "installed/custom.test"
    _package(installed)
    monkeypatch.setattr(custom_tts_adapters, "ROOT", tmp_path / "installed")
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="custom.test", tts_fallback="none"))
    monkeypatch.setattr("lib.cartesia_voices.english_voices", lambda: [])

    status = tts_providers.status()
    row = next(item for item in status["providers"] if item["id"] == "custom.test")
    assert row["custom"] is True
    assert row["available"] is True
    assert row["supports_preview"] is True

    catalog = voice_catalog.catalog({
        "agent": {
            "name": "Agent",
            "voice_id": '{"custom.test":"warm"}',
        }
    }, "agent")
    group = next(item for item in catalog["providers"] if item["id"] == "custom.test")
    assert group["custom"] is True
    assert group["voices"][0]["current"] is True
    assert group["voices"][0]["preview_url"].startswith(
        "/voice-preview?provider=custom.test")

    output = tmp_path / "speech.mp3"
    written = tts_providers.synthesize(
        "custom.test", text="hello", voice="warm", out_path=output)
    assert written == output.stat().st_size
    assert output.read_bytes().startswith(b"ID3")


def test_invalid_installed_adapter_remains_visible_and_removable(
        tmp_path, monkeypatch):
    package = _package(tmp_path / "installed/custom.broken")
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["operations"] = ["voices"]
    (package / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(custom_tts_adapters, "ROOT", tmp_path / "installed")

    row = custom_tts_adapters.inventory(
        reserved_ids=tts_providers.VALID_IDS)[0]
    assert row["id"] == "custom.test"
    assert row["available"] is False
    assert "preview" in row["error"]
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="none", tts_fallback="none"))
    status_row = next(
        item for item in tts_providers.status()["providers"]
        if item["id"] == "custom.test")
    assert status_row["installed"] is True
    assert status_row["available"] is False

    custom_tts_adapters.remove(
        "CUSTOM.TEST", reserved_ids=tts_providers.VALID_IDS)
    assert not package.exists()


def test_adapter_responses_require_explicit_success(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.no-ok")
    executable = package / "adapter"
    executable.write_text(executable.read_text().replace(
        'print(json.dumps({"ok": True, "voices": [',
        'print(json.dumps({"voices": ['))
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    with pytest.raises(custom_tts_adapters.AdapterError, match="operation failed"):
        custom_tts_adapters.voices(manifest, force=True)


def test_failed_voice_catalog_is_temporarily_cached(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.failing")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
json.load(sys.stdin)
counter = pathlib.Path(__file__).with_name("calls")
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
print(json.dumps({"ok": False, "error": "catalog offline"}))
""")
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    for _ in range(2):
        with pytest.raises(custom_tts_adapters.AdapterError, match="catalog offline"):
            custom_tts_adapters.voices(manifest)

    assert (package / "calls").read_text() == "1"


def test_adapter_response_limit_does_not_buffer_unbounded_output(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.noisy")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import sys
sys.stdout.write("x" * (2 * 1024 * 1024))
""")
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    with pytest.raises(custom_tts_adapters.AdapterError, match="too large"):
        custom_tts_adapters.voices(manifest, force=True)


def test_adapter_timeout_kills_spawned_process_group(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.children")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import pathlib, subprocess, time
child = subprocess.Popen(["sleep", "60"])
pathlib.Path(__file__).with_name("child-pid").write_text(str(child.pid))
time.sleep(60)
""")
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    with pytest.raises(custom_tts_adapters.AdapterError, match="timed out"):
        custom_tts_adapters._request(
            manifest, {"schema_version": 1, "operation": "voices"}, timeout=0.1)

    child_pid = int((package / "child-pid").read_text())
    for _ in range(20):
        stat_path = Path(f"/proc/{child_pid}/stat")
        if stat_path.is_file() and stat_path.read_text().split()[2] == "Z":
            break
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("adapter child process survived timeout")


def test_successful_adapter_launcher_cannot_leave_child_running(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.forking")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, subprocess, sys
request = json.load(sys.stdin)
child = subprocess.Popen(["sleep", "60"])
pathlib.Path(__file__).with_name("child-pid").write_text(str(child.pid))
print(json.dumps({"ok": True, "voices": [
    {"id": "warm", "name": "Warm"}
]}))
""")
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    assert custom_tts_adapters.voices(manifest, force=True)[0]["id"] == "warm"

    child_pid = int((package / "child-pid").read_text())
    for _ in range(20):
        stat_path = Path(f"/proc/{child_pid}/stat")
        if stat_path.is_file() and stat_path.read_text().split()[2] == "Z":
            break
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("successful adapter left its child process running")


def test_adapter_audio_growth_is_stopped_before_timeout(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.growing")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, sys, time
request = json.load(sys.stdin)
path = pathlib.Path(request["output_path"])
with path.open("wb") as output:
    output.truncate(65 * 1024 * 1024)
time.sleep(60)
""")
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    started = time.monotonic()
    with pytest.raises(custom_tts_adapters.AdapterError, match="too large"):
        custom_tts_adapters.synthesize(
            manifest, text="hello", voice="warm",
            out_path=tmp_path / "speech.mp3")

    assert time.monotonic() - started < 3


def test_unplayable_mp3_fails_adapter_validation(tmp_path, monkeypatch):
    package = _package(tmp_path / "source", adapter_id="custom.bad-mp3")
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["audio_format"] = "audio/mpeg"
    (package / "manifest.json").write_text(json.dumps(manifest))
    executable = package / "adapter"
    text = executable.read_text()
    start = text.index('elif operation in {"preview", "synthesize"}:')
    end = text.index("else:\n", start)
    executable.write_text(
        text[:start]
        + 'elif operation in {"preview", "synthesize"}:\n'
          '    pathlib.Path(request["output_path"]).write_bytes(b"not audio")\n'
          '    print(json.dumps({"ok": True}))\n'
        + text[end:])
    executable.chmod(0o755)
    monkeypatch.setattr(custom_tts_adapters, "ROOT", tmp_path / "installed")

    with pytest.raises(custom_tts_adapters.AdapterError, match="playable MP3"):
        custom_tts_adapters.install(
            package, reserved_ids=tts_providers.VALID_IDS)


def test_broken_stdin_terminates_adapter_process_group(tmp_path):
    package = _package(tmp_path / "source", adapter_id="custom.no-stdin")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import os, pathlib, time
pathlib.Path(__file__).with_name("adapter-pid").write_text(str(os.getpid()))
os.close(0)
time.sleep(60)
""")
    executable.chmod(0o755)
    manifest = custom_tts_adapters.load_manifest(package, portable=True)

    with pytest.raises(
            custom_tts_adapters.AdapterError,
            match="rejected its request|timed out"):
        custom_tts_adapters._request(
            manifest, {"schema_version": 1, "operation": "voices",
                       "padding": "x" * (512 * 1024)}, timeout=0.5)

    pid = int((package / "adapter-pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        stat_path = Path(f"/proc/{pid}/stat")
        assert stat_path.is_file() and stat_path.read_text().split()[2] == "Z"
