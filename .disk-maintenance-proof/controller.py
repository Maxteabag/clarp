#!/usr/bin/python3
"""SOURCE CANDIDATE. Requires reviewed, fresh owner binding and two approvals."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
from owned import Owned, CustodyUnknown, DispatchPermit
import sys
import time

BASE = Path('/var/tmp/clarp-refactor-program')
SHARE = Path('/home/peter/.local/share/clarp')
MAIN = ['clarp.service', 'clarp-runtime.service']
ENV = dict(os.environ, LC_ALL='C', LANG='C')

class Refused(Exception):
    pass

class Controller:
    def __init__(self, binding, evidence):
        self.b = binding
        self.e = evidence
        self.seq = 0
        self.stopped = False
        self.balance_terminal = True
        self.balance_child = None
        self.offline_start = None
        self.work_deadline = None
        self.phase = "preflight"
        self.cancel_attempted = False
        self.diagnostic_errors = []
        self.operational_errors = []
        self.children = []
        self.abort_requested = False
        self.abort_signal = None

    def log(self, kind, **values):
        try:
            row = dict(kind=kind, utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       monotonic=time.monotonic(), **values)
            with (self.e / 'events.jsonl').open('a') as f:
                f.write(json.dumps(row, sort_keys=True) + '\n')
                f.flush()
                os.fsync(f.fileno())
        except BaseException as exc:
            self.diagnostic_errors.append(type(exc).__name__ + ':' + str(exc))
            # No retry and no control over recovery starts. Terminal is non-green.

    def remaining(self):
        if self.abort_requested:
            raise Refused('controller_abort_latched_' + str(self.abort_signal))
        if self.work_deadline is None:
            raise Refused('work_clock_unbound')
        left = self.work_deadline - time.monotonic()
        if left <= 0:
            raise Refused('work_budget_exhausted')
        return left

    def command(self, argv, seconds=10):
        if self.phase == 'work':
            seconds = min(seconds, self.remaining())
        self.seq += 1
        output = self.e / ('command-%03d.log' % self.seq)
        with output.open('xb') as f:
            try:
                child = Owned(argv, f, ENV, native_sources=self.b.get("native_sources"),
                              work_permit=DispatchPermit(self.work_deadline, self) if self.phase == "work" else None)
            except CustodyUnknown as exc:
                self.log('launch_custody_UNKNOWN', evidence=exc.child_evidence)
                raise
            self.children.append(child)
            try:
                if self.phase == 'work':
                    seconds = min(seconds, self.remaining())
                rc = child.wait(seconds)
            except BaseException:
                child.cleanup()
                raise
        if output.stat().st_size > 1024 * 1024:
            raise Refused('command_output_over_cap')
        text = output.read_text(errors='replace')
        self.log('command', argv=argv, rc=rc, output=output.name)
        return rc, text

    def require(self, argv, seconds=10):
        rc, text = self.command(argv, seconds)
        if rc:
            raise Refused('command_nonzero_' + str(rc))
        return text

    def unit(self, name):
        text = self.require(['/usr/bin/systemctl', '--user', 'show', name,
                             '--property=ActiveState,SubState,MainPID,InvocationID,ControlGroup'])
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def same_release(self):
        if str((SHARE / 'current').resolve(strict=True)) != self.b['current_real']:
            raise Refused('current_changed')
        root = Path(self.b['current_real'])
        for name, digest in self.b['release_files'].items():
            if name not in ('RUNTIME_RELEASE_ID', 'INSTALL_OK', 'RUNTIME_READY'):
                raise Refused('unexpected_release_marker')
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise Refused('release_marker_changed')

    def no_balance(self):
        rc, text = self.command(['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'status', '/'], 20)
        # btrfs-progs documents terminal no-balance via this C-locale message.
        if rc not in (0, 1) or not re.fullmatch(r'No balance found on [^\n]+\n?', text):
            raise Refused('balance_terminal_UNKNOWN')
        self.balance_terminal = True

    def writer_census(self):
        # Owner's producer inventory is also required; this is not a capability sandbox.
        protected = {str(SHARE / x) for x in ('state.sqlite', 'state.sqlite-wal', 'state.sqlite-shm')}
        holders = []
        for proc in Path('/proc').iterdir():
            if self.phase == 'work':
                self.remaining()
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                for fd in (proc / 'fd').iterdir():
                    if self.phase == 'work':
                        self.remaining()
                    try:
                        target = os.readlink(fd)
                    except FileNotFoundError:
                        continue
                    if target.removesuffix(' (deleted)') in protected:
                        holders.append(int(proc.name))
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                raise Refused('writer_census_permission_UNKNOWN') from exc
        self.log('writer_fd_census', holders=sorted(set(holders)))
        if holders:
            raise Refused('writer_still_open')
        active = self.require(['/usr/bin/systemctl', '--user', 'list-units', '--type=service',
                               '--state=active,activating', '--no-legend', '--plain'])
        if any(line.strip().startswith('clarp-bookkeeping-') for line in active.splitlines()):
            raise Refused('bookkeeping_reactivated')

    def stop(self):
        if self.abort_requested:
            raise Refused('abort_before_any_stop')
        # Mark before first stop: partial stop still needs restoration.
        self.stopped = True
        self.offline_start = time.monotonic()
        self.work_deadline = self.offline_start + 600
        self.phase = 'work'
        for name in self.b['timers'] + self.b['auxiliary_units'] + MAIN:
            self.require(['/usr/bin/systemctl', '--user', 'stop', name], 45)
        for name in self.b['auxiliary_units'] + MAIN:
            state = self.unit(name)
            if state.get('ActiveState') not in ('inactive', 'failed') or state.get('MainPID') != '0':
                raise Refused('unit_not_stopped_' + name)
            group = state.get('ControlGroup')
            if group:
                directory = Path('/sys/fs/cgroup' + group)
                if directory.exists():
                    events = dict(line.split() for line in (directory / 'cgroup.events').read_text().splitlines())
                    if events.get('populated') != '0':
                        raise Refused('owned_cgroup_subtree_not_empty')
        self.writer_census()
        self.log('no_writers_verified_WITH_owner_inventory_not_universal_proof')

    def cancel_balance(self):
        if self.cancel_attempted:
            raise Refused('cancel_already_attempted_no_renewed_budget')
        self.cancel_attempted = True
        self.phase = 'cancel'
        # One cancellation, one separate terminal confirmation; no finally retry.
        cancel_error = None
        try:
            rc, _ = self.command(['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'cancel', '/'], 60)
            self.log('balance_cancel_result', rc=rc)
        except BaseException as exc:
            cancel_error = type(exc).__name__ + ':' + str(exc)
            self.operational_errors.append('cancel:' + cancel_error)
        self.no_balance()
        if self.balance_child is not None and not self.balance_child.reap_started:
            self.balance_child.cleanup()
        if cancel_error:
            self.log('cancel_command_fault_but_terminal_confirmed', reason=cancel_error)

    def pass_balance(self, usage):
        self.phase = 'work'
        self.remaining()
        self.writer_census()
        # Recompute AFTER census/prerequisite commands, at each actual dispatch.
        seconds = min(300, self.remaining())
        self.seq += 1
        output = self.e / ('balance-%d.log' % usage)
        with output.open('xb') as f:
            self.remaining()
            self.balance_terminal = False
            try:
                child = Owned(['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'start',
                               '-dusage=' + str(usage), '/'], f, ENV, native_sources=self.b.get('native_sources'),
                               work_permit=DispatchPermit(self.work_deadline, self))
            except CustodyUnknown as exc:
                self.log('balance_launch_custody_UNKNOWN', evidence=exc.child_evidence)
                raise
            self.children.append(child)
            self.balance_child = child
            # Fork/identity setup consumes the SAME budget too.
            seconds = min(seconds, self.remaining())
            rc = child.wait(seconds)
            self.log('balance_pass', usage=usage, rc=rc)
            if rc:
                raise Refused('balance_nonzero')
            self.no_balance()
            self.balance_child = None

    def usage(self):
        text = self.require(['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'filesystem', 'usage', '-b', '/'])
        m = re.search(r'^\s*Device unallocated:\s*(\d+)\s*$', text, re.M)
        if not m:
            raise Refused('unallocated_parse_UNKNOWN')
        return int(m.group(1))

    def restore(self):
        if not self.stopped:
            return
        self.phase = 'recovery'
        self.no_balance()
        self.same_release()
        failures = []
        for name in ['clarp-runtime.service', 'clarp.service'] + self.b['auxiliary_units'] + self.b['timers']:
            try:
                rc, _ = self.command(['/usr/bin/systemctl', '--user', 'start', name], 20)
                if rc:
                    failures.append(name + ':rc' + str(rc))
            except BaseException as exc:
                # Commit the operational failure BEFORE fallible diagnostics.
                failures.append(name + ':' + type(exc).__name__ + ':' + str(exc))
                self.log('start_fault', unit=name, error=failures[-1])
        self.operational_errors.extend(failures)
        for name in MAIN:
            try:
                state = self.unit(name)
                self.log('restored_unit_state_NOT_HTTP_health', unit=name, state=state)
            except BaseException as exc:
                self.operational_errors.append('post_state:' + name + ':' + str(exc))
        self.log('restore_complete', failures=failures,
                 elapsed_since_first_stop=time.monotonic() - self.offline_start)
        if failures:
            raise Refused('restore_unit_failure')

    def run(self):
        def interrupted(signum, frame):
            self.abort_requested = True
            self.abort_signal = signum
            raise Refused('controller_signal_' + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        primary = None
        try:
            self.same_release()
            self.no_balance()
            self.usage()
            for name, invocation in self.b['expected_active_invocations'].items():
                state = self.unit(name)
                if state.get('ActiveState') != 'active' or state.get('InvocationID') != invocation:
                    raise Refused('unit_preflight_changed_' + name)
            for name in self.b['timers']:
                if self.unit(name).get('ActiveState') != 'active':
                    raise Refused('timer_preflight_changed_' + name)
            self.stop()
            self.pass_balance(10)
            unallocated = self.usage()
            if unallocated < 2 * 1024**3:
                self.pass_balance(25)
            self.usage()
        except BaseException as exc:
            primary = type(exc).__name__ + ':' + str(exc)
            self.operational_errors.append(primary)
            self.log('primary_abort', reason=primary)
        finally:
            try:
                if not self.balance_terminal:
                    self.cancel_balance()
                self.restore()
            except BaseException as exc:
                primary = (primary or '') + '|recovery_UNKNOWN'
                self.operational_errors.append(type(exc).__name__ + ':' + str(exc))
                self.log('RECOVERY_UNKNOWN_NEEDS_EXTERNAL_ATTENTION', reason=self.operational_errors[-1])
            if self.diagnostic_errors or self.operational_errors:
                primary = primary or 'evidence_or_operation_incomplete'
            self.log('terminal', diagnostic_errors=self.diagnostic_errors, operational_errors=self.operational_errors, outcome='FAILED_OR_UNKNOWN' if primary else 'BALANCE_AND_UNIT_STARTS_COMPLETE_OWNER_CHECKS_PENDING',
                     reason=primary, no_http_health_or_replay_claim=True)
        return 11 if primary or self.diagnostic_errors or self.operational_errors else 0


def main():
    # Binding is prepared only after real approvals/owner PRE, not a sample production config.
    if len(sys.argv) != 4:
        raise Refused('usage_binding_sha_evidence')
    raw = Path(sys.argv[1]).read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != sys.argv[2]:
        raise Refused('binding_hash_or_size')
    b = json.loads(raw)
    if b['schema'] != 'clarp.disk-maintenance-binding/1' or b['peter_decision'] != 'decision-0795e2c8e5ebd591f6c242a8' or b['peter_yes'] is not True or not b['lead_go_receipt']:
        raise Refused('approvals_unbound')
    if b['pre_state'] != 'ZERO' or not b['writer_inventory_reconciled'] or not b['quiescence_receipt']:
        raise Refused('owner_PRE_or_quiescence_unbound')
    now = time.time()
    if not (b['window_start_epoch'] <= now <= b['window_end_epoch'] and 0 <= now - b['pre_end_epoch'] <= 120):
        raise Refused('window_or_PRE_stale')
    if len(b['auxiliary_units']) + len(b['timers']) > 8 or len(set(b['auxiliary_units'] + b['timers'])) != len(b['auxiliary_units'] + b['timers']):
        raise Refused('unit_inventory_count_or_duplicates')
    for name in b['auxiliary_units'] + b['timers']:
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.(service|timer)', name) or name in MAIN:
            raise Refused('unit_name')
    if set(b['expected_active_invocations']) != set(MAIN + b['auxiliary_units']):
        raise Refused('unit_inventory_binding')
    cgroup = Path('/proc/self/cgroup').read_text()
    if any('/' + name in cgroup for name in MAIN + b['auxiliary_units'] + b['timers']):
        raise Refused('controller_inside_Clarp_cgroup')
    root = Path(b['current_real'])
    if root.parent != SHARE / 'releases' or root.is_symlink() or not root.is_dir():
        raise Refused('release_path')
    if set(b['release_files']) != {'RUNTIME_RELEASE_ID', 'INSTALL_OK', 'RUNTIME_READY'}:
        raise Refused('release_marker_set')
    e = Path(sys.argv[3])
    e.mkdir(mode=0o700)  # Must be absent: no reused result path.
    os.umask(0o077)
    with (BASE / 'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return Controller(b, e).run()

if __name__ == '__main__':
    raise SystemExit(main())
