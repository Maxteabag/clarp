#!/usr/bin/env python3
"""Install exact vendor CLI versions into a Clarp-owned private prefix."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def platform_key() -> str:
    system = platform.system().lower()
    if system == "darwin":
        system = "darwin"
    elif system == "linux":
        system = "linux"
    else:
        raise SystemExit(f"unsupported toolchain operating system: {system}")
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if machine not in {"arm64", "aarch64", "x86_64", "amd64"}:
        raise SystemExit(f"unsupported toolchain architecture: {machine}")
    return f"{system}-{architecture}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_node(root: Path, source: Path) -> Path:
    manifest = json.loads((source / "toolchain.json").read_text())
    version = str(manifest["node_version"])
    key = platform_key()
    record = manifest["archives"].get(key)
    if not isinstance(record, dict):
        raise SystemExit(f"no pinned Node archive for {key}")
    destination = root / "node" / f"{version}-{key}"
    node = destination / "bin/node"
    if node.is_file():
        return destination
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / str(record["file"])
    expected = str(record["sha256"])
    if not archive.is_file() or sha256(archive) != expected:
        archive.unlink(missing_ok=True)
        url = f"https://nodejs.org/dist/v{version}/{record['file']}"
        temporary = archive.with_suffix(archive.suffix + ".part")
        temporary.unlink(missing_ok=True)
        print(f">> downloading pinned Node {version} for {key}")
        urllib.request.urlretrieve(url, temporary)
        if sha256(temporary) != expected:
            temporary.unlink(missing_ok=True)
            raise SystemExit("downloaded Node archive failed SHA-256 verification")
        temporary.replace(archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary_dir:
        temporary_root = Path(temporary_dir)
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(temporary_root, filter="data")
        extracted = temporary_root / archive.name.removesuffix(".tar.gz")
        if not (extracted / "bin/node").is_file():
            raise SystemExit("Node archive did not contain the expected executable")
        extracted.rename(destination)
    return destination


def install_packages(root: Path, source: Path, node_root: Path) -> Path:
    npm_root = root / "npm"
    npm_root.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(source / name, npm_root / name)
    lock_digest = sha256(source / "package-lock.json")
    marker = npm_root / ".clarp-lock-sha256"
    if marker.is_file() and marker.read_text().strip() == lock_digest:
        if all((npm_root / f"node_modules/.bin/{name}").exists()
               for name in ("claude", "codex")):
            return npm_root
    environment = dict(os.environ)
    environment["PATH"] = f"{node_root / 'bin'}:{environment.get('PATH', '')}"
    print(">> installing pinned Claude and Codex CLIs")
    subprocess.run(
        [str(node_root / "bin/npm"), "ci", "--prefix", str(npm_root),
         "--no-audit", "--no-fund"],
        env=environment, check=True,
    )
    marker.write_text(lock_digest + "\n")
    return npm_root


def write_wrapper(path: Path, executable: Path, node_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".next")
    lines = [
        "#!/bin/sh",
        f"PATH={json.dumps(str(node_root / 'bin'))}:\"$PATH\"",
        "export PATH",
    ]
    if path.name == "claude":
        lines.extend([
            "DISABLE_AUTOUPDATER=1",
            "DISABLE_UPDATES=1",
            "export DISABLE_AUTOUPDATER DISABLE_UPDATES",
        ])
    lines.append(f"exec {json.dumps(str(executable))} \"$@\"")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o700)
    temporary.replace(path)


def install(root: Path, source: Path) -> dict[str, str]:
    root = root.expanduser().resolve()
    source = source.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    node_root = ensure_node(root, source)
    npm_root = install_packages(root, source, node_root)
    binaries = {}
    for name in ("claude", "codex"):
        vendor = npm_root / f"node_modules/.bin/{name}"
        wrapper = root / f"bin/{name}"
        write_wrapper(wrapper, vendor, node_root)
        binaries[name] = str(wrapper)
    versions = {
        "node": subprocess.check_output(
            [str(node_root / "bin/node"), "--version"], text=True).strip(),
        **{
            name: subprocess.check_output(
                [path, "--version"], text=True, env={
                    **os.environ,
                    "PATH": f"{root / 'bin'}:{os.environ.get('PATH', '')}",
                }).strip()
            for name, path in binaries.items()
        },
    }
    (root / "versions.json").write_text(
        json.dumps(versions, indent=2, sort_keys=True) + "\n")
    return binaries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.root, args.source), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
