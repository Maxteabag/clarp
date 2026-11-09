#!/usr/bin/bash
set -euo pipefail
umask 077
# This shell itself is under the outer570TERM+30KILL envelope.
test "$GITHUB_REPOSITORY" = Maxteabag/clarp
test "$GITHUB_EVENT_NAME" = push
test "$GITHUB_REF" = refs/heads/refactor/ci/e26-bookkeeping-restore-e623-proof-r1
test "$GITHUB_RUN_ATTEMPT" = 1
test "$PROOF_RUNNER_ENVIRONMENT" = github-hosted
test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
test ! -e proof-results
mkdir -m700 proof-results
/usr/bin/python3 -B - <<'PY'
from pathlib import Path
import hashlib,json,os,platform
c=json.loads(Path('SOURCE-CLOSURE.json').read_bytes())
for name,r in c['bindings'].items():
 p=Path(name);assert p.is_file() and not p.is_symlink()
 raw=p.read_bytes();assert len(raw)==r['bytes'] and hashlib.sha256(raw).hexdigest()==r['sha256']
Path('proof-results/source.json').write_text(json.dumps(dict(carrier=os.environ['GITHUB_SHA'],run=os.environ['GITHUB_RUN_ID'],attempt=os.environ['GITHUB_RUN_ATTEMPT'],controller=c['controller_source'],python=platform.python_version(),kernel=platform.platform(),image=os.environ.get('ImageOS'),image_version=os.environ.get('ImageVersion'),bindings=c['bindings']),indent=2)+'\n')
PY
/usr/bin/python3 -m venv --without-pip .venv
# Reuse the exact public writer-proof containment and its unmodified self-check.
set +e
(
  umask 022
  bash scripts/ci_contained.sh /usr/bin/bash -c 'cd .disk-maintenance-proof && exec /usr/bin/bash run-selected.sh'
) > proof-results/contained.raw 2>&1
primary=$?
printf '%s\n' "$primary" > proof-results/contained.rc
receipt=$?
cat proof-results/contained.raw
display=$?
if [ "$primary" -ne 0 ]; then exit "$primary"; fi
if [ "$receipt" -ne 0 ] || [ "$display" -ne 0 ]; then exit 11; fi
exit 0
