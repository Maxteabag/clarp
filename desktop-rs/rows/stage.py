#!/usr/bin/env python3
"""ROWS scratch proof only (branch proof/desktop-rows-20261101, never merged; copied from the FAFB v9 stage.py).

Stages the upload: python3 rows/stage.py RAW_DIR UPLOAD_DIR
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
STAGE_MAX = 4096       # STAGE.json is a few fixed fields
# Priority order: verdicts and small logs first, frames last.
ALLOW = ["source.txt", "*.admission", "*.rc", "*.verdict", "r-walk.log", "r-handle.log", "r-neg.log",
         "r-walk/app.log", "r-handle/app.log", "r-neg/app.log", "build.log", "r-*/*.png"]

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
# The metadata reserve is an upper bound from the full listing: OVERFLOW.json
# as if every raw file were listed in it with the longer reason, plus STAGE.json.
worst = [{"path": rel, "bytes": size, "sha256": digest, "reason": "over the 80 MB upload cap"} for rel, size, digest in listing]
META = len(json.dumps({"omitted": worst, "not_allowlisted": []}, indent=1).encode()) + 1 + STAGE_MAX
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


def total():
    return sum(p.stat().st_size for p in up.rglob("*") if p.is_file())


stage = {"complete": complete, "cap_bytes": CAP, "meta_reserve_bytes": META, "staged_bytes": staged,
         "staged_files": len(chosen), "raw_files": len(listing), "raw_bytes": sum(s for _, s, _ in listing),
         "omitted": len(omitted), "not_allowlisted": len(others),
         "note": "" if complete else "INCOMPLETE: the proof is STOP; see OVERFLOW.json"}
# Enforced, not assumed: the whole upload dir with STAGE.json must fit the cap.
# If it does not, the staged copies are removed whole (the hash listing and
# OVERFLOW.json stay), and the proof is INCOMPLETE.
if total() + STAGE_MAX > CAP:
    shutil.rmtree(up / "raw", ignore_errors=True)
    complete = False
    stage.update(complete=False, staged_files=0, note="INCOMPLETE: over the 80 MB cap with metadata; staged copies removed, hashes kept")
(up / "STAGE.json").write_text(json.dumps(stage) + "\n")
stage["upload_bytes"] = total()
(up / "STAGE.json").write_text(json.dumps(stage) + "\n")
if stage["upload_bytes"] > CAP:
    # Even the listings do not fit: upload only STAGE.json (the raws stay on
    # the runner, unhashed in the artifact). Never more than the cap.
    for p in list(up.iterdir()):
        if p.name != "STAGE.json":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    complete = False
    stage.update(complete=False, staged_files=0, note="INCOMPLETE: even the hash listings exceed the cap; only STAGE.json uploaded")
    (up / "STAGE.json").write_text(json.dumps(stage) + "\n")
print(f"ROWS-STAGE {json.dumps(stage)}")
sys.exit(0 if complete else 6)
