#!/usr/bin/env python3
"""Decode a captured API response with the repository's built Swift core DTO."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--type", required=True, dest="type_name")
    parser.add_argument("--module", default="ClarpCore")
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--swiftc", default=shutil.which("swiftc") or "swiftc")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", args.type_name):
        parser.error("type must be a Swift type name")
    if not re.fullmatch(r"[A-Za-z_]\w*", args.module):
        parser.error("module must be a Swift module name")
    payload = args.payload.resolve(strict=True)
    if payload.stat().st_size > 16 * 1024 * 1024:
        parser.error("capture exceeds 16 MiB; use a bounded response")
    candidates = [args.build_dir] if args.build_dir else list((args.repo / ".build/core").glob("*/debug"))
    candidates = [p for p in candidates if (p / "Modules" / (args.module + ".swiftmodule")).exists()]
    if not candidates:
        parser.error("build the repository core first with scripts/test_core.sh, or pass --build-dir")
    build = max(candidates, key=lambda p: (p / "Modules" / (args.module + ".swiftmodule")).stat().st_mtime)
    objects = sorted((build / (args.module + ".build")).glob("*.o"))
    if not objects:
        parser.error("compiled module objects are missing")
    source = f'''import Foundation
@testable import {args.module}
do {{
    let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
    _ = try JSONDecoder().decode({args.type_name}.self, from: data)
    print("Decoded {args.type_name} successfully")
}} catch {{
    print(String(reflecting: error))
    exit(1)
}}
'''
    with tempfile.TemporaryDirectory(prefix="clarp-decode-") as directory:
        root = Path(directory)
        program = root / "decode.swift"
        program.write_text(source)
        binary = root / "decode"
        compiled = subprocess.run([args.swiftc, "-I", str(build / "Modules"), str(program),
                                   *map(str, objects), "-o", str(binary)], timeout=120)
        if compiled.returncode:
            return compiled.returncode
        return subprocess.run([str(binary), str(payload)], timeout=30).returncode


if __name__ == "__main__":
    raise SystemExit(main())
