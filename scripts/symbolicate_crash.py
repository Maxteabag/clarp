#!/usr/bin/env python3
"""Symbolicate a MetricKit crash payload on Linux, no Mac required.

The iOS app POSTs MetricKit diagnostics to /crash, and the server stores each
payload under ~/.local/share/clarp/crashes/ as ios-diagnostic-<millis>-<hex>.json
(crash-<millis>.json before commit 422b32f renamed it). Those payloads
carry addresses, not names: every frame has a binaryUUID and an
offsetIntoBinaryTextSegment. To turn that into function names and line numbers
we need the dSYM whose UUID matches the frame, which the TestFlight workflow
uploads as the clarp-dsyms artifact.

    # index whatever dSYMs are on hand, then symbolicate the newest crash
    scripts/symbolicate_crash.py --latest

    # a specific payload against a specific dSYM
    scripts/symbolicate_crash.py path/to/crash-1788202829366.json \
        --dsym-dir ios-native/.build/arm64-apple-ios/release

Frames from binaries we have no dSYM for (SwiftUI, SwiftUICore, libswiftCore)
are still printed, with binary name and offset, so the shape of the stack
survives even when only the app is symbolicated.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# MetricHit offsets are relative to the __TEXT segment's vmaddr, which is
# 0x100000000 for every arm64 iOS executable Xcode links. The `address` field in
# the payload includes the ASLR slide and is therefore useless across launches;
# the offset is the stable coordinate.
TEXT_VMADDR = 0x100000000

DEFAULT_STORE = Path.home() / ".local/share/clarp-dsyms"
DEFAULT_CRASH_DIR = Path.home() / ".local/share/clarp/crashes"
# 422b32f renamed the payloads to ios-diagnostic-* when the endpoint grew to
# carry hangs and CPU exceptions as well as crashes. Both names are on disk.
PAYLOAD_GLOBS = ("ios-diagnostic-*.json", "crash-*.json")


def payloads_by_recency(directory: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in PAYLOAD_GLOBS:
        found.extend(directory.glob(pattern))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def find_dwarf_binaries(root: Path) -> list[Path]:
    """Every DWARF payload under a directory, whether or not it is a bundle."""
    if root.is_file():
        return [root]
    found = []
    for dsym in sorted(root.rglob("*.dSYM")):
        found.extend(sorted(dsym.glob("Contents/Resources/DWARF/*")))
    if not found:
        # A bare DWARF file laid out without the bundle wrapper.
        found = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix == ""]
    return found


def uuid_of(binary: Path) -> str | None:
    try:
        out = subprocess.run(
            ["llvm-dwarfdump", "--uuid", str(binary)],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"UUID: ([0-9A-Fa-f-]{36})", out)
    return match.group(1).upper() if match else None


def build_index(dirs: list[Path]) -> dict[str, Path]:
    """Map binary UUID to the DWARF file that can symbolicate it."""
    index: dict[str, Path] = {}
    for root in dirs:
        if not root.exists():
            continue
        for binary in find_dwarf_binaries(root):
            uuid = uuid_of(binary)
            if uuid and uuid not in index:
                index[uuid] = binary
    return index


def symbolicate(binary: Path, offsets: list[int]) -> dict[int, str]:
    """Resolve offsets in one binary with a single llvm-symbolizer invocation."""
    if not offsets:
        return {}
    stdin = "".join(f"0x{TEXT_VMADDR + off:x}\n" for off in offsets)
    try:
        proc = subprocess.run(
            ["llvm-symbolizer", f"--obj={binary}", "--demangle",
             "--functions=short", "--output-style=LLVM"],
            input=stdin, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: llvm-symbolizer failed for {binary}: {exc}", file=sys.stderr)
        return {}

    # llvm-symbolizer separates each input address with a blank line, and emits
    # function/location pairs (repeated when a frame was inlined).
    resolved: dict[int, str] = {}
    blocks = proc.stdout.split("\n\n")
    for offset, block in zip(offsets, blocks):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        parts = []
        for i in range(0, len(lines) - 1, 2):
            func, loc = lines[i], lines[i + 1]
            loc = re.sub(r":0:0$", "", loc)
            if func in ("??", ""):
                continue
            parts.append(f"{func}  ({loc})" if loc not in ("??", "") else func)
        if parts:
            resolved[offset] = "  <- inlined in ".join(parts)
    return resolved


def flatten(frame: dict, depth: int = 0) -> list[tuple[int, dict]]:
    """MetricKit nests frames as subFrames; walk them into an ordered list."""
    rows = [(depth, frame)]
    for sub in frame.get("subFrames") or []:
        rows.extend(flatten(sub, depth + 1))
    return rows


def collect_offsets(rows: list[tuple[int, dict]]) -> dict[str, set[int]]:
    per_uuid: dict[str, set[int]] = {}
    for _, frame in rows:
        uuid = (frame.get("binaryUUID") or "").upper()
        offset = frame.get("offsetIntoBinaryTextSegment")
        if uuid and isinstance(offset, int):
            per_uuid.setdefault(uuid, set()).add(offset)
    return per_uuid


def describe_metadata(meta: dict) -> str:
    signal = meta.get("signal")
    exc_type = meta.get("exceptionType")
    key = (exc_type, signal) if isinstance(exc_type, int) and isinstance(signal, int) else None
    # The pair that matters most: type 6 / signal 5 is EXC_BREAKPOINT, a Swift
    # runtime trap (integer overflow, force unwrap, precondition failure).
    known = {
        (6, 5): "EXC_BREAKPOINT / SIGTRAP - Swift runtime trap",
        (1, 11): "EXC_BAD_ACCESS / SIGSEGV - bad memory access",
        (10, 10): "EXC_BAD_ACCESS / SIGBUS",
        (4, 4): "EXC_BAD_INSTRUCTION / SIGILL",
    }.get(key, "") if key else ""
    bits = [
        f"app {meta.get('appVersion')} build {meta.get('appBuildVersion')}",
        f"{meta.get('osVersion')}",
        f"{meta.get('deviceType')}",
        f"exceptionType={exc_type} exceptionCode={meta.get('exceptionCode')} signal={signal}",
    ]
    if meta.get("terminationReason"):
        bits.append(f"terminationReason={meta['terminationReason']}")
    if known:
        bits.append(known)
    return "\n  ".join(b for b in bits if b and b != "None")


def render_crash(crash: dict, index: dict[str, Path], max_frames: int) -> None:
    meta = crash.get("diagnosticMetaData") or {}
    print(f"  {describe_metadata(meta)}")

    stacks = (crash.get("callStackTree") or {}).get("callStacks") or []
    per_thread = (crash.get("callStackTree") or {}).get("callStackPerThread")
    print(f"  {len(stacks)} thread(s), callStackPerThread={per_thread}")

    for n, stack in enumerate(stacks):
        attributed = stack.get("threadAttributed")
        rows: list[tuple[int, dict]] = []
        for root in stack.get("callStackRootFrames") or []:
            rows.extend(flatten(root))

        # Only the crashing thread is worth printing in full by default.
        if not attributed and n > 0:
            continue

        label = "CRASHING THREAD" if attributed else f"thread {n}"
        print(f"\n  --- {label} ({len(rows)} frames) ---")

        resolved: dict[str, dict[int, str]] = {}
        for uuid, offsets in collect_offsets(rows).items():
            if uuid in index:
                resolved[uuid] = symbolicate(index[uuid], sorted(offsets))

        for depth, frame in rows[:max_frames]:
            uuid = (frame.get("binaryUUID") or "").upper()
            offset = frame.get("offsetIntoBinaryTextSegment")
            name = frame.get("binaryName") or "?"
            symbol = resolved.get(uuid, {}).get(offset) if isinstance(offset, int) else None
            marker = "*" if symbol else " "
            if symbol:
                print(f"  {marker} {depth:2d} {name}  {symbol}")
            else:
                print(f"  {marker} {depth:2d} {name} + {offset}")
        if len(rows) > max_frames:
            print(f"     ... {len(rows) - max_frames} more frames "
                  f"(raise --max-frames to see them)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("crash", nargs="*", type=Path,
                       help="payload(s) to symbolicate (ios-diagnostic-*.json)")
    parser.add_argument("--latest", action="store_true",
                       help=f"symbolicate the newest payload in {DEFAULT_CRASH_DIR}")
    parser.add_argument("--dsym-dir", type=Path, action="append", default=[],
                       help="directory to search for dSYMs (repeatable)")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE,
                       help=f"dSYM store, always searched (default {DEFAULT_STORE})")
    parser.add_argument("--max-frames", type=int, default=40)
    parser.add_argument("--list-uuids", action="store_true",
                       help="print the UUIDs each payload needs, then exit")
    args = parser.parse_args()

    for tool in ("llvm-symbolizer", "llvm-dwarfdump"):
        if not shutil.which(tool):
            print(f"error: {tool} not found; install the llvm package", file=sys.stderr)
            return 2

    payloads = list(args.crash)
    if args.latest or not payloads:
        candidates = payloads_by_recency(DEFAULT_CRASH_DIR)
        if not candidates:
            print(f"error: no crash payloads in {DEFAULT_CRASH_DIR}", file=sys.stderr)
            return 2
        payloads = [candidates[0]]

    if args.list_uuids:
        for path in payloads:
            data = json.loads(path.read_text())
            needed: dict[str, str] = {}
            for crash in data.get("crashDiagnostics") or []:
                for stack in (crash.get("callStackTree") or {}).get("callStacks") or []:
                    for root in stack.get("callStackRootFrames") or []:
                        for _, frame in flatten(root):
                            uuid = (frame.get("binaryUUID") or "").upper()
                            if uuid:
                                needed[uuid] = frame.get("binaryName") or "?"
            print(f"{path.name}:")
            for uuid, name in sorted(needed.items(), key=lambda kv: kv[1]):
                print(f"  {uuid}  {name}")
        return 0

    search = [args.store, *args.dsym_dir]
    index = build_index(search)
    print(f"dSYM index: {len(index)} binary UUID(s) from "
          f"{', '.join(str(p) for p in search)}")
    for uuid, binary in sorted(index.items()):
        print(f"  {uuid}  {binary.name}")

    for path in payloads:
        data = json.loads(path.read_text())
        crashes = data.get("crashDiagnostics") or []
        print(f"\n=== {path.name} : {len(crashes)} crash(es), "
              f"{data.get('timeStampBegin')} .. {data.get('timeStampEnd')} ===")
        for crash in crashes:
            render_crash(crash, index, args.max_frames)

    if not index:
        print("\nNo dSYMs matched. Frames show binary + offset only.\n"
              "Fetch the dSYMs for the crashing build with:\n"
              "  scripts/fetch_dsyms.sh <workflow-run-id>", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
