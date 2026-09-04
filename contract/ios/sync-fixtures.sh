#!/bin/sh
# Sync/check the shared payload fixtures in an explicit iOS worktree.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
check=0
if [ "${1:-}" = --check ]; then check=1; shift; fi
dest=${1:-"$here/../../../clarp-ios/CoreBehaviorTests/Fixtures"}
python3 - "$here/../fixtures" "$dest" "$check" <<'PY'
import hashlib, json, pathlib, shutil, subprocess, sys
source, target = map(pathlib.Path, sys.argv[1:3])
check = sys.argv[3] == '1'
def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.glob('*/*.json'))}
expected = hashes(source)
manifest = target / 'contract-source.json'
if check:
    if hashes(target) != expected:
        raise SystemExit('iOS fixture content differs from the Host checkout')
    if not manifest.is_file() or json.loads(manifest.read_text()).get('files') != expected:
        raise SystemExit('iOS fixture provenance is missing or stale')
    print('shared contract fixtures match')
else:
    target.mkdir(parents=True, exist_ok=True)
    previous = json.loads(manifest.read_text()).get('files', {}) if manifest.is_file() else {}
    for name in previous.keys() - expected.keys():
        path = target / name
        if path.resolve().is_relative_to(target.resolve()) and path.is_file():
            path.unlink()
    for name in expected:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    manifest.write_text(json.dumps({'host_sha': sha, 'files': expected}, indent=2) + '\n')
    print(f'synced {len(expected)} fixtures from Host {sha}')
PY
