from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import config, custom_stt_adapters
from lib.custom_tts_adapters import AdapterError


def _package(root: Path, adapter_id: str = "custom.test-stt") -> Path:
    root.mkdir(parents=True)
    executable = root / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
request = json.load(sys.stdin)
if request["operation"] == "models":
    print(json.dumps({"ok": True, "models": [
        {"id": "general", "name": "General", "weight": "remote", "languages": ["en"]},
        {"id": "fast", "name": "Fast", "weight": "remote"}
    ]}))
elif request["operation"] == "transcribe":
    assert pathlib.Path(request["audio_path"]).is_file()
    print(json.dumps({"ok": True, "text": "adapter transcript.",
                      "ends_terminal": True, "duration_seconds": 0.25}))
else:
    print(json.dumps({"ok": False, "error": "unsupported"}))
    raise SystemExit(2)
""")
    executable.chmod(0o755)
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "id": adapter_id,
        "name": "Test STT",
        "description": "Test custom transcription",
        "executable": "./adapter",
        "operations": ["models", "transcribe"],
        "default_model": "general",
        "timeout_seconds": 30,
    }))
    return root


def test_install_catalog_test_and_transcribe(tmp_path, monkeypatch):
    source = _package(tmp_path / "source")
    monkeypatch.setattr(custom_stt_adapters, "ROOT", tmp_path / "installed")

    manifest = custom_stt_adapters.install(source)

    assert manifest.id == "custom.test-stt"
    rows = custom_stt_adapters.models(manifest)
    assert rows[0]["id"] == "custom.test-stt:general"
    assert rows[0]["custom"] is True
    assert custom_stt_adapters.test_adapter(manifest)["models"] == 2
    text, terminal, duration = custom_stt_adapters.transcribe(
        manifest, model_id="custom.test-stt:fast",
        audio_bytes=b"RIFFtest", content_type="audio/wav",
        vocab_prompt="Clarp")
    assert (text, terminal, duration) == ("adapter transcript.", True, 0.25)


def test_manifest_requires_both_operations_and_matching_default(tmp_path):
    source = _package(tmp_path / "source")
    raw = json.loads((source / "manifest.json").read_text())
    raw["operations"] = ["models"]
    (source / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(AdapterError, match="transcribe"):
        custom_stt_adapters.load_manifest(source, portable=True)

    raw["operations"] = ["models", "transcribe"]
    raw["default_model"] = "missing"
    (source / "manifest.json").write_text(json.dumps(raw))
    manifest = custom_stt_adapters.load_manifest(source, portable=True)
    with pytest.raises(AdapterError, match="default_model"):
        custom_stt_adapters.models(manifest, force=True)


def test_broken_adapter_is_visible_and_removable(tmp_path, monkeypatch):
    package = _package(tmp_path / "installed/custom.broken")
    (package / "adapter").unlink()
    monkeypatch.setattr(custom_stt_adapters, "ROOT", tmp_path / "installed")

    row = custom_stt_adapters.inventory()[0]
    assert row["available"] is False

    custom_stt_adapters.remove("custom.test-stt")
    assert not package.exists()


def test_transient_model_failure_preserves_default_capability_row(
        tmp_path, monkeypatch):
    package = _package(tmp_path / "installed/custom.offline")
    executable = package / "adapter"
    executable.write_text("""#!/usr/bin/env python3
import json, sys
json.load(sys.stdin)
print(json.dumps({"ok": False, "error": "temporarily offline"}))
""")
    executable.chmod(0o755)
    monkeypatch.setattr(custom_stt_adapters, "ROOT", tmp_path / "installed")

    rows = custom_stt_adapters.catalog_models()

    assert rows == [{
        "id": "custom.test-stt:general",
        "name": "Test STT",
        "provider": "custom.test-stt",
        "model": "general",
        "weight": "adapter",
        "languages": [],
        "description": "Test custom transcription",
        "installed": True,
        "status": "unavailable",
        "error": "temporarily offline",
        "custom": True,
        "adapter_name": "Test STT",
    }]


def test_adapter_transcripts_use_common_hallucination_filter(tmp_path):
    package = _package(tmp_path / "source")
    executable = package / "adapter"
    executable.write_text(executable.read_text().replace(
        "adapter transcript.", "Thank you."))
    executable.chmod(0o755)
    manifest = custom_stt_adapters.load_manifest(package, portable=True)

    text, _terminal, _duration = custom_stt_adapters.transcribe(
        manifest, model_id="custom.test-stt:general",
        audio_bytes=b"audio", content_type="audio/webm", vocab_prompt="")

    assert text == ""


def test_replacing_active_adapter_preserves_configured_model(
        tmp_path, monkeypatch):
    root = tmp_path / "installed"
    monkeypatch.setattr(custom_stt_adapters, "ROOT", root)
    custom_stt_adapters.install(_package(tmp_path / "initial"))
    monkeypatch.setattr(config, "_CACHED", config.Config(
        whisper_provider="custom.test-stt", whisper_model="general"))
    replacement = _package(tmp_path / "replacement")
    executable = replacement / "adapter"
    executable.write_text(executable.read_text().replace(
        '"general", "name": "General"',
        '"replacement", "name": "Replacement"'))
    executable.chmod(0o755)
    manifest_path = replacement / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["default_model"] = "replacement"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(AdapterError, match="active model"):
        custom_stt_adapters.install(replacement, replace=True)

    assert custom_stt_adapters.get("custom.test-stt").default_model == "general"
