#!/usr/bin/env python3
"""FAFB scratch proof only (branch proof/desktop-fafb-20261101, never merged).

Stages the upload: python3 fafb/stage.py RAW_DIR UPLOAD_DIR
Only allowlisted files are copied whole, in priority order, while the staged
total stays within 80 MB. A file is never cut: one that does not fit is left on
the runner and listed in OVERFLOW.json with its size and sha256. RAW-SHA256SUMS
lists every raw file (size, sha256), staged or not. STAGE.json says whether the
proof's evidence is complete. Exit 0 complete, 6 INCOMPLETE (an allowlisted
file left out), 2 no raw directory.
"""
import hashlib
import json
import pathlib
import shutil
import sys

CAP = 80 * 1024 * 1024
META = 1024 * 1024    # kept free for RAW-SHA256SUMS' neighbours: OVERFLOW.json and STAGE.json
# Priority order: verdicts and small logs first, frames last.
ALLOW = ["source.txt", "*.rc", "*.verdict", "fa.log", "b-obs.log", "b-green.log", "b-neg.log",
         "b-obs/app.log", "b-green/app.log", "b-neg/app.log", "build.log", "b-*/*.png"]

raw, up = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
up.mkdir(parents=True, exist_ok=True)
if not raw.is_dir():
    (up / "STAGE.json").write_text(json.dumps({"complete": False, "problem": f"no {raw}"}) + "\n")
    sys.exit(2)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


files = sorted(p for p in raw.rglob("*") if p.is_file())
listing = [(p.relative_to(raw).as_posix(), p.stat().st_size, sha(p)) for p in files]
manifest = "".join(f"{digest}  {size}  {rel}\n" for rel, size, digest in listing)
(up / "RAW-SHA256SUMS").write_text(manifest)
staged = len(manifest.encode())
chosen, omitted, seen = [], [], set()
for pattern in ALLOW:
    for rel, size, digest in [x for x in listing if pathlib.PurePosixPath(x[0]).match(pattern) and x[0] not in seen]:
        seen.add(rel)
        if staged + size > CAP - META:
            omitted.append({"path": rel, "bytes": size, "sha256": digest, "reason": "over the 80 MB upload cap"})
            continue
        target = up / "raw" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(raw / rel, target)
        staged += size
        chosen.append(rel)
others = [{"path": rel, "bytes": size, "sha256": digest, "reason": "not allowlisted"} for rel, size, digest in listing if rel not in seen]
complete = not omitted
(up / "OVERFLOW.json").write_text(json.dumps({"omitted": omitted, "not_allowlisted": others}, indent=1) + "\n")
stage = {"complete": complete, "cap_bytes": CAP, "staged_bytes": staged, "staged_files": len(chosen),
         "raw_files": len(listing), "raw_bytes": sum(s for _, s, _ in listing), "omitted": len(omitted),
         "not_allowlisted": len(others), "note": "" if complete else "INCOMPLETE: the proof is STOP; see OVERFLOW.json"}
(up / "STAGE.json").write_text(json.dumps(stage) + "\n")
print(f"FAFB-STAGE {json.dumps(stage)}")
sys.exit(0 if complete else 6)
