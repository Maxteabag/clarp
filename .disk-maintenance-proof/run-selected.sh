#!/usr/bin/bash
set -eu
umask 077
unset PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C
[ "$(id -u)" -ne 0 ] || exit 11
ROOT=$(pwd -P)
[ ! -e results ] || exit 11
sha256sum -c SHA256SUMS
[ "$(cat SOURCE_COMMIT)" = 684555326522175c2bdab42e908cfc5c1ecc8b21 ] || exit 11
mkdir -m700 results results/fixtures
export TMPDIR="$ROOT/results/fixtures"
/usr/bin/python3 --version > results/python.txt 2>&1
uname -srvmo > results/kernel.txt
set +e
/usr/bin/timeout --signal=TERM --kill-after=5s 240s /usr/bin/python3 -B -m unittest -v \
  test_writer_focus.WriterFocus.test_exact_tolerated_set_accepted_without_stopping_units \
  test_writer_focus.WriterFocus.test_unlisted_db_fd_including_listener_is_rejected \
  test_writer_focus.WriterFocus.test_changed_pid_start_or_unit_generation_rejected \
  test_writer_focus.WriterFocus.test_postcheck_heartbeat_finished_and_mismark_are_distinct_no_edit \
  test_balance_timing.TimingFocus.test_actual_controller_shared_phase_cancel_order_and_420_narrow_recovery > results/unittest.raw 2>&1
selected=$?
printf "%s\n" "$selected" > results/selected.rc
receipt_rc=$?
/usr/bin/ps -eo pid,ppid,pgid,sid,uid,stat,comm > results/post-process-census.txt
census_rc=$?
sha256sum SHA256SUMS SOURCE_COMMIT controller.py owned.py child_exec.py writer_policy.py post_check.py test_writer_focus.py test_balance_timing.py > results/executed.sha256
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
