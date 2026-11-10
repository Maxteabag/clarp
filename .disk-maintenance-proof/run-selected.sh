#!/usr/bin/bash
set -eu
umask 077
unset PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C
[ "$(id -u)" -ne 0 ] || exit 11
ROOT=$(pwd -P)
[ ! -e results ] || exit 11
sha256sum -c SHA256SUMS
[ "$(cat SOURCE_COMMIT)" = fdf3c7967e209f90a923dd1a741bec2e509aea61 ] || exit 11
mkdir -m700 results results/fixtures
export TMPDIR="$ROOT/results/fixtures"
/usr/bin/python3 --version > results/python.txt 2>&1
uname -srvmo > results/kernel.txt
set +e
/usr/bin/timeout --signal=TERM --kill-after=5s 240s /usr/bin/python3 -B -m unittest -v \
  test_manager_residual.ManagerResidualFocus.test_only_owner_accepted_manager_EACCES_is_residual_not_inventory_proof > results/unittest.raw 2>&1
selected=$?
printf "%s\n" "$selected" > results/selected.rc
receipt_rc=$?
/usr/bin/ps -eo pid,ppid,pgid,sid,uid,stat,comm > results/post-process-census.txt
census_rc=$?
sha256sum SHA256SUMS SOURCE_COMMIT controller-r24/SHA256SUMS controller-r24/child_exec.py controller-r24/controller.py controller-r24/effective_class.py controller-r24/owned.py controller-r24/post_check.py controller-r24/roster.py controller-r24/writer_policy.py supervisor/protocol.py test_manager_residual.py > results/executed.sha256
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
