#!/usr/bin/bash
# SOURCE-ONLY proposal until exact route review and trigger authority.
set -euo pipefail
umask 077
OUT=auth934-abc-evidence
mkdir -m700 "$OUT"
finish() {
  primary=$?
  trap - EXIT
  set +e
  printf '%s\n' "$primary" > "$OUT/runner.rc"
  receipt=$?
  if [ "$primary" -ne 0 ]; then exit "$primary"; fi
  if [ "$receipt" -ne 0 ]; then exit 11; fi
  exit 0
}
trap finish EXIT
test "$GITHUB_REPOSITORY" = Maxteabag/clarp
test "$GITHUB_EVENT_NAME" = push
test "$GITHUB_REF" = refs/heads/refactor/ci/auth934-abc-33fa-golden-focus-r2
test "$GITHUB_RUN_ATTEMPT" = 1
test "$AUTH934_RUNNER_ENVIRONMENT" = github-hosted
test -z "${CLARP_UPDATE_ROUTE_GOLDENS+x}"
test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
test "$(git rev-parse HEAD^)" = 33fae91a23311ae7c898e2f943e557451c59f64f
.venv/bin/python -B - <<'PYSOURCE' > "$OUT/source.raw" 2>&1
from pathlib import Path
import hashlib,json,os,platform,subprocess
product='33fae91a23311ae7c898e2f943e557451c59f64f'
allowed={'.github/workflows/auth934-abc-golden-focus.yml','.github/scripts/auth934-abc-golden-focus.sh'}
changed=set(subprocess.check_output(['git','diff','--name-only',product,'HEAD'],text=True).splitlines())
assert changed==allowed, changed
assert not subprocess.check_output(['git','diff','--name-only','HEAD'],text=True).strip()
bindings={'server/lib/janitor_autonomy.py': '2c9cd1bfe452dbbb2ad87a6a18dac3e310dc25a24834ba53ccc3560ba0d62f6c', 'server/lib/janitor_hotseat.py': '4c2bc0a2ff8eff8532e889ce2cdc6c97b93c686b27fe8358608466f697debdfe', 'server/lib/janitors.py': 'c45afead7d98569e41b445acbe8466fa94ca986b2f7cd9ed606b09fc75a31ca5', 'tests/unit/test_janitor_hotseat.py': 'fd937a5ebd8ca020c74b5d0b77b893c1403903e5384b4f312dfd4cb36e2ee91b', 'tests/unit/test_hotseat_status.py': '376543a0e53e6fb01cd864865d98e105d7643a1a30b44e1cc5358896a08a4bf9', 'server/lib/server_identity.py': 'be8452e2daf7c03aff9272284a278c4062487cf6506f07090a0f111a740a33e5', 'docs/compatibility.md': 'c6e7b7d85fee53899ebcc43753c5603ae2c2e9eb2b5906cde54462d6caa9cd98', 'tests/fixtures/route_goldens/responses.json': '80e0b1c0f3a369cb5962b526b5bc41ca3eab0ac5c4df6ca0d2fa0c9da4dcca14', 'scripts/ci_contained.sh': '07f228044e915297a84a52201e68e40a34809928fcf0ecb8d300b692ee5d067f', 'pyproject.toml': '67161d5a23d3a6db7a5c2580f4338059ae983f5f8b5082acedf2f8e23e740ad5', 'uv.lock': '74df07b1c87379354c79639c8309f834080b0ce2028927f1c07ad2b0ae5922da', '.github/workflows/tests.yml': 'b43d469937cbb58bd8064acf040b73f896ef4486a0756f1243c0eb13be3612cb'}
for path,digest in bindings.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest, path
fixture=Path('tests/fixtures/route_goldens/responses.json')
assert hashlib.sha256(fixture.read_bytes()).hexdigest()=='80e0b1c0f3a369cb5962b526b5bc41ca3eab0ac5c4df6ca0d2fa0c9da4dcca14'
Path('auth934-abc-evidence/source.json').write_text(json.dumps(dict(product=product,carrier=os.environ['GITHUB_SHA'],run=os.environ['GITHUB_RUN_ID'],attempt=os.environ['GITHUB_RUN_ATTEMPT'],python=platform.python_version(),kernel=platform.platform(),fixture_before_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),scope='generated-fixture-and-associated-focus-only-NOT-G1',controller_changes=sorted(changed)),indent=2)+'\n')
PYSOURCE
cat "$OUT/source.raw"
# Existing namespace helper alone gets022. All raw files open OUTSIDE under077.
phase() {
  name=$1
  seconds=$2
  shift 2
  set +e
  (
    umask 022
    bash scripts/ci_contained.sh /usr/bin/timeout --signal=TERM --kill-after=15s "${seconds}s" "$@"
  ) > "$OUT/$name.raw" 2>&1
  primary=$?
  printf '%s\n' "$primary" > "$OUT/$name.rc"
  receipt=$?
  cat "$OUT/$name.raw"
  display=$?
  set -e
  if [ "$primary" -ne 0 ]; then exit "$primary"; fi
  if [ "$receipt" -ne 0 ] || [ "$display" -ne 0 ]; then exit 11; fi
}
phase golden-update 180 /usr/bin/env CLARP_UPDATE_ROUTE_GOLDENS=1 \
  .venv/bin/python -B -m pytest -n 0 \
  tests/integration/test_route_response_goldens.py::test_response_golden \
  --junitxml=auth934-abc-evidence/golden-update.xml --clarp-timing-dir=auth934-abc-evidence/timing/update
.venv/bin/python -B - <<'PYDELTA' > "$OUT/generator-delta.raw" 2>&1
from pathlib import Path
import copy,hashlib,json,subprocess
fixture='tests/fixtures/route_goldens/responses.json'
before=subprocess.check_output(['git','show','33fae91a23311ae7c898e2f943e557451c59f64f:'+fixture])
after=Path(fixture).read_bytes()
# F1: retain bounded observed bytes and the exact patch BEFORE shape guards.
# These are UNVALIDATED evidence until every independent assertion succeeds.
out=Path('auth934-abc-evidence')
assert len(after)<=1024*1024, 'generated fixture exceeds 1MiB evidence bound'
patch=subprocess.check_output(['git','diff','--binary','--',fixture])
assert len(patch)<=2*1024*1024, 'generated patch exceeds 2MiB evidence bound'
with (out/'responses.generated.json').open('xb') as f:f.write(after)
with (out/'generated.patch').open('xb') as f:f.write(patch)
with (out/'generator-output-UNVALIDATED.json').open('x') as f:
    json.dump(dict(state='UNVALIDATED_OBSERVED_AFTER_GENERATOR_RC0',before_sha256=hashlib.sha256(before).hexdigest(),after_sha256=hashlib.sha256(after).hexdigest(),patch_sha256=hashlib.sha256(patch).hexdigest(),validation='Consult generator-delta.json and actual primary status; this receipt alone never means PASS'),f,indent=2)
assert set(subprocess.check_output(['git','diff','--name-only','HEAD'],text=True).splitlines())=={fixture}
old=json.loads(before);new=json.loads(after)
assert set(old)==set(new) and old['_about']==new['_about']
assert set(old['routes'])==set(new['routes'])
changed=[k for k in old['routes'] if old['routes'][k]!=new['routes'][k]]
assert changed==['GET /janitors'], changed
# responses.json is a TYPE-SHAPE owner: contract integers are 'num', not78/80.
# Do not invent number-value changes or allow any other route/status byte drift.
a=before.splitlines(keepends=True);b=after.splitlines(keepends=True)
assert len(a)==len(b)
lines=[i for i,(x,y) in enumerate(zip(a,b)) if x!=y]
assert len(lines)==1 and a[lines[0]].startswith(b'  "GET /janitors": ')
# Exact in-memory inverse of ONLY the granted one descriptor shape. The actual
# generated fixture is NEVER rewritten/normalized by this validation.
shape={'default':'bool','description':'str','key':'str','label':'str','type':'str'}
matches=[]
body=new['routes']['GET /janitors']
for i,t in enumerate(body['admin']['body']['templates']):
    for j,o in enumerate(t.get('options',[])):
        if o!=shape:continue
        candidate=copy.deepcopy(body)
        del candidate['admin']['body']['templates'][i]['options'][j]
        templates=candidate['admin']['body']['templates']
        unique={json.dumps(x,sort_keys=True):x for x in templates}
        candidate['admin']['body']['templates']=[unique[k] for k in sorted(unique)]
        if candidate==old['routes']['GET /janitors']:matches.append([i,j])
assert len(matches)==1, matches
(out/'generator-delta.json').write_text(json.dumps(dict(before_sha256=hashlib.sha256(before).hexdigest(),after_sha256=hashlib.sha256(after).hexdigest(),changed_routes=changed,only_changed_line=lines[0]+1,one_bool_inverse_path=matches[0],all_other_bytes_equal=True,contract_literal_values='not emitted by this shape generator',independent_B_and_contract_review='REQUIRED_BEFORE_FINAL_PIN_G1'),indent=2)+'\n')
print('exact one catalog boolean shape; all other fixture bytes equal')
PYDELTA
cat "$OUT/generator-delta.raw"
# New pytest processes with UPDATE absent. Whole owner modules already contain
# all seven repaired reach cases and Codex OFF/ON; do not replay them separately.
phase associated-focus 420 /usr/bin/env -u CLARP_UPDATE_ROUTE_GOLDENS \
  .venv/bin/python -B -m pytest -n 0 \
  tests/unit/test_backend_capabilities.py tests/unit/test_backend_identity_guard.py \
  tests/unit/test_janitor_hotseat.py tests/unit/test_hotseat_status.py \
  tests/unit/test_client_contract.py \
  --junitxml=auth934-abc-evidence/associated-focus.xml --clarp-timing-dir=auth934-abc-evidence/timing/focus
phase route-validation 180 /usr/bin/env -u CLARP_UPDATE_ROUTE_GOLDENS \
  .venv/bin/python -B -m pytest -n 0 \
  tests/integration/test_route_goldens.py tests/integration/test_route_response_goldens.py \
  --junitxml=auth934-abc-evidence/route-validation.xml --clarp-timing-dir=auth934-abc-evidence/timing/route
.venv/bin/python -B - <<'PYFINAL' > "$OUT/final-source.raw" 2>&1
from pathlib import Path
import hashlib,json,subprocess
fixture='tests/fixtures/route_goldens/responses.json'
assert set(subprocess.check_output(['git','diff','--name-only','HEAD'],text=True).splitlines())=={fixture}
record=json.loads(Path('auth934-abc-evidence/generator-delta.json').read_text())
assert hashlib.sha256(Path(fixture).read_bytes()).hexdigest()==record['after_sha256']
Path('auth934-abc-evidence/RESULT.json').write_text(json.dumps(dict(scope='GENERATOR_AND_ASSOCIATED_FOCUS_ONLY_NOT_FULL_G1_CI',raw_phase_rcs={n:Path('auth934-abc-evidence/'+n+'.rc').read_text().strip() for n in ['golden-update','associated-focus','route-validation']},fixture_review='PENDING_B_AND_CONTRACT_EXACT_DELTA',final_reviewed_pin='NOT_CREATED',contract80='PROVISIONAL_NOT_BOOKED'),indent=2)+'\n')
PYFINAL
cat "$OUT/final-source.raw"
