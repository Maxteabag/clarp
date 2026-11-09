#!/usr/bin/bash
set -eu
umask 077
unset PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C
[ "$(id -u)" -ne 0 ] || exit 11
ROOT=$(pwd -P)
[ ! -e results ] || exit 11
sha256sum -c SHA256SUMS
[ "$(cat SOURCE_COMMIT)" = f226950b3b1748e4b6d9a208b9ac2f8bbbcfc852 ] || exit 11
mkdir -m700 results results/fixtures
export TMPDIR="$ROOT/results/fixtures"
/usr/bin/python3 --version > results/python.txt 2>&1
uname -srvmo > results/kernel.txt
set +e
/usr/bin/timeout --signal=TERM --kill-after=5s 240s /usr/bin/python3 -B -m unittest -v \
  test_controls.Controls.test_partial_stop_faults_do_not_balance_and_attempt_both_starts \
  test_controls.Controls.test_runtime_start_and_log_fault_still_attempts_host_and_is_not_green \
  test_controls.Controls.test_single_clock_expiry_after_census_prevents_every_balance_spawn \
  test_controls.Controls.test_pass_threshold_no_third_pass_and_expiry_after_stop \
  test_controls.Controls.test_unknown_cancel_terminal_blocks_starts_and_no_cancel_retry \
  test_controls.Controls.test_confirmed_cancel_with_logging_fault_still_restores_and_retains_failure \
  test_controls.Controls.test_owned_real_exit_timeout_and_no_signals_after_reap_or_ECHILD \
  test_controls.BindingAndWriterControls.test_actual_main_refuses_approval_stale_inventory_and_self_cgroup_before_controller \
  test_controls.BindingAndWriterControls.test_actual_writer_census_refuses_named_holder_and_permission_unknown \
  test_controls.FollowupControls.test_real_gate_restores_child_mask_and_normal_TERM_is_not_forced_KILL \
  test_controls.FollowupControls.test_actual_observe_credential_transition_requires_exact_gated_lineage \
  test_controls.FollowupControls.test_registered_abort_during_actual_work_log_prevents_25_and_restores \
  test_controls.GateAndCleanupControls.test_setup_ages_original_permit_and_private_effect_never_executes \
  test_controls.GateAndCleanupControls.test_gate_own_expiry_check_refuses_lower_exec_effect \
  test_controls.GateAndCleanupControls.test_each_close_failure_preserves_primary_both_attempts_and_parent_mask > results/unittest.raw 2>&1
selected=$?
printf "%s\n" "$selected" > results/selected.rc
receipt_rc=$?
/usr/bin/ps -eo pid,ppid,pgid,sid,uid,stat,comm > results/post-process-census.txt
census_rc=$?
sha256sum SHA256SUMS SOURCE_COMMIT controller.py owned.py child_exec.py test_controls.py > results/executed.sha256
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
