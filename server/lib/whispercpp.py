"""Pinned macOS whisper.cpp runtime and model installer."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


WHISPER_CPP_VERSION = "b4938"
WHISPER_CPP_SOURCE_URL = (
    "https://github.com/ggml-org/whisper.cpp/archive/refs/tags/b4938.tar.gz")
WHISPER_CPP_SOURCE_SHA256 = (
    "6d8d70a014ca2b10f8a6d006b8f423e5f5ef2afcfbe92b57ab4e01107238112a")
MODELS = {
    "base.en": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.en.bin",
        "sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
    },
    "small.en": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.en.bin",
        "sha256": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
    },
}


def _download_verified(url: str, destination: Path, expected_sha256: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Clarp installer"})
    digest = hashlib.sha256()
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.download")
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            out.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"checksum mismatch for {destination.name}: expected {expected_sha256}, got {actual}")
    temporary.replace(destination)


def build_runtime(managed_root: Path) -> Path:
    """Build the pinned Metal-enabled whisper-cli runtime."""
    if shutil.which("cmake") is None or shutil.which("xcrun") is None:
        raise RuntimeError(
            "managed whisper.cpp requires cmake and the Xcode Command Line Tools")

    managed_root.mkdir(parents=True, exist_ok=True)
    archive = managed_root / f"whisper.cpp-{WHISPER_CPP_VERSION}.tar.gz"
    _download_verified(
        WHISPER_CPP_SOURCE_URL, archive, WHISPER_CPP_SOURCE_SHA256)
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(managed_root, filter="data")
    source = managed_root / f"whisper.cpp-{WHISPER_CPP_VERSION}"
    build = managed_root / "build"
    subprocess.run([
        "cmake", "-S", str(source), "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF",
        "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF",
    ], check=True)
    subprocess.run([
        "cmake", "--build", str(build), "--config", "Release", "-j",
    ], check=True)
    built_cli = build / "bin/whisper-cli"
    if not built_cli.is_file():
        raise RuntimeError("whisper.cpp build did not produce whisper-cli")
    runtime = managed_root / "whisper-cli"
    shutil.copy2(built_cli, runtime)
    runtime.chmod(0o700)
    return runtime


def install(model_name: str, managed_root: Path) -> tuple[Path, Path]:
    """Build a pinned CLI and download a checksum-pinned GGML model."""
    if model_name not in MODELS:
        raise ValueError(f"unsupported whisper.cpp model: {model_name}")
    runtime = build_runtime(managed_root)

    model = managed_root / f"ggml-{model_name}.bin"
    spec = MODELS[model_name]
    _download_verified(spec["url"], model, spec["sha256"])
    (managed_root / "runtime.json").write_text(json.dumps({
        "provider": "whisper.cpp", "version": WHISPER_CPP_VERSION,
        "source_sha256": WHISPER_CPP_SOURCE_SHA256,
        "model": model_name, "model_sha256": spec["sha256"],
    }, indent=2, sort_keys=True) + "\n")
    return model, runtime
