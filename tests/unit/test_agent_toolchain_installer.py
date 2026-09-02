from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "install_agent_toolchain", ROOT / "scripts/install_agent_toolchain.py")
toolchain = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(toolchain)


def test_manifest_pins_supported_platforms_and_exact_cli_versions():
    manifest = json.loads((ROOT / "toolchain/toolchain.json").read_text())
    assert set(manifest["archives"]) == {
        "darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"}
    assert all(len(item["sha256"]) == 64 for item in manifest["archives"].values())
    package = json.loads((ROOT / "toolchain/package.json").read_text())
    assert package["dependencies"] == {
        "@anthropic-ai/claude-code": "2.1.258",
        "@openai/codex": "0.150.1",
    }
    lock = json.loads((ROOT / "toolchain/package-lock.json").read_text())
    assert lock["packages"][""]["dependencies"] == package["dependencies"]


def test_platform_key_normalizes_supported_architectures(monkeypatch):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(toolchain.platform, "machine", lambda: "arm64")
    assert toolchain.platform_key() == "darwin-arm64"
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    monkeypatch.setattr(toolchain.platform, "machine", lambda: "x86_64")
    assert toolchain.platform_key() == "linux-x64"


def test_wrapper_uses_private_node_and_vendor_binary(tmp_path):
    wrapper = tmp_path / "toolchain/bin/claude"
    vendor = tmp_path / "toolchain/npm/node_modules/.bin/claude"
    node = tmp_path / "toolchain/node/version"
    toolchain.write_wrapper(wrapper, vendor, node)
    text = wrapper.read_text()
    assert str(node / "bin") in text
    assert str(vendor) in text
    assert "DISABLE_UPDATES=1" in text
    assert '"$@"' in text
    assert wrapper.stat().st_mode & 0o777 == 0o700
