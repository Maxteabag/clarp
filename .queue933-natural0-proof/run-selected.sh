#!/usr/bin/bash
set -eu
umask 077
unset PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C
[ "$(id -u)" -ne 0 ] || exit 11
ROOT=$(pwd -P)
export PYTHONPATH="$ROOT/tests"
[ ! -e results ] || exit 11
sha256sum -c SHA256SUMS
[ "$(cat SOURCE_COMMIT)" = 7466afefe410579e855c38f2e3afa3c4a96b7eec ] || exit 11
mkdir -m700 results results/fixtures
export TMPDIR="$ROOT/results/fixtures"
/usr/bin/python3 --version > results/python.txt 2>&1
uname -srvmo > results/kernel.txt
set +e
/usr/bin/timeout --signal=TERM --kill-after=5s 240s /usr/bin/python3 -B -m unittest -v \
  test_natural_zero933.NaturalZero933.test_natural_zero_positive_join_completion_and_ordinary_policies \
  test_natural_zero933.NaturalZero933.test_natural_zero_without_positive_join_is_unknown_not_admission \
  test_natural_zero933.NaturalZero933.test_actual_one_real_registry_validator_and_policy_path_retained > results/unittest.raw 2>&1
selected=$?
printf "%s\n" "$selected" > results/selected.rc
receipt_rc=$?
/usr/bin/ps -eo pid,ppid,pgid,sid,uid,stat,comm > results/post-process-census.txt
census_rc=$?
sha256sum SHA256SUMS SOURCE_COMMIT ops/reship-operator.py ops/natural-runtime933.py ops/transition933.py ops/eligibility-watch.py ops/live-watch.py ops/release-metrics.py ops/baseline-reuse904.py ops/runtime-availability.py ops/rss-continuity911.py ops/error-window927.py support/runtime_bridge931.py tests/test_natural_zero933.py > results/executed.sha256
hash_rc=$?
/usr/bin/python3 -B - <<'INDEX'
import hashlib,json,os,stat
from pathlib import Path
rows=[]
for root,dirs,files in os.walk('results',followlinks=False):
    for name in list(dirs)+files:
        p=Path(root)/name
        if p.name=='index.json' and p.parent==Path('results'):continue
        st=p.lstat()
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise SystemExit(11)
        if stat.S_ISDIR(st.st_mode):continue
        if len(rows)>=1000 or st.st_size>32*1024**2:raise SystemExit(11)
        rows.append({'path':str(p),'bytes':st.st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
with open('results/index.json','x') as f:json.dump(rows,f,sort_keys=True)
INDEX
index_rc=$?
if [ "$selected" -ne 0 ]; then exit "$selected"; fi
if [ "$receipt_rc" -ne 0 ] || [ "$census_rc" -ne 0 ] || [ "$hash_rc" -ne 0 ] || [ "$index_rc" -ne 0 ]; then exit 11; fi
exit 0
