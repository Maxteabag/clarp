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
from writer_policy import validate_workers, require_same_worker, classify_fd, WriterUnknown, require_terminal, read_exit, classify_post, validate_heartbeat_age
import sys
import time
import traceback

BASE = Path('/var/tmp/clarp-refactor-program')
SHARE = Path('/home/peter/.local/share/clarp')
MAIN = ['clarp.service', 'clarp-runtime.service']
ENV = dict(os.environ, LC_ALL='C', LANG='C')

class Refused(Exception):
    pass

class CleanupPermitOwner:
    abort_requested=False

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
        self.balance_origin = None
        self.overlap_recovery = False
        self.command_deadline = None
        self.last_gate_bracket = None
        self.target300_missed = False

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

    def work_dispatch_deadline(self):
        self.remaining()
        deadline=self.work_deadline
        if self.balance_origin is not None:
            # Reserve eight seconds for ordinary helper retirement before240.
            deadline=min(deadline,self.balance_origin+232)
        if time.monotonic()>=deadline:raise Refused('balance_cancel_reserve_reached')
        return deadline

    def command(self,argv,seconds=10):
        deadline=self.command_deadline
        if self.phase=='work':
            value=self.work_dispatch_deadline()
            deadline=min(deadline,value) if deadline is not None else value
        if deadline is not None:
            seconds=min(seconds,deadline-time.monotonic())
            if seconds<=0:raise Refused('command_absolute_deadline')
        self.seq+=1;output=self.e/('command-%03d.log'%self.seq)
        with output.open('xb') as f:
            before=time.monotonic()
            permit=DispatchPermit(deadline,self if self.phase=='work' else CleanupPermitOwner()) if deadline is not None else None
            try:
                child=Owned(argv,f,ENV,native_sources=self.b.get('native_sources'),work_permit=permit)
            except CustodyUnknown as exc:
                self.log('launch_custody_UNKNOWN',evidence=exc.child_evidence);raise
            after=time.monotonic()
            self.last_gate_bracket=(before,after)
            self.children.append(child)
            try:
                if deadline is not None:
                    seconds=min(seconds,deadline-time.monotonic())
                    if seconds<=0:raise Refused('command_setup_deadline')
                rc=child.wait(seconds)
            except BaseException:
                child.cleanup();raise
        if output.stat().st_size>1024*1024:raise Refused('command_output_over_cap')
        text=output.read_text(errors='replace')
        self.log('command',argv=argv,rc=rc,output=output.name,exec_grant_bracket=[before,after])
        return rc,text

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

    def no_balance(self,deadline=None):
        previous=self.command_deadline
        self.command_deadline=deadline
        try:
            rc,text=self.command(['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','status','/'],20)
        finally:
            self.command_deadline=previous
        # btrfs-progs documents terminal no-balance via this C-locale message.
        if rc not in (0, 1) or not re.fullmatch(r'No balance found on [^\n]+\n?', text):
            raise Refused('balance_terminal_UNKNOWN')
        self.balance_terminal = True

    def process_observation(self, proc):
        return {'pid':int(proc.name),
                'start':(proc/'stat').read_text().rsplit(')',1)[1].split()[19],
                'cgroup':(proc/'cgroup').read_text().strip().split('0::',1)[1],
                'uid':proc.stat().st_uid,
                'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}

    def capture_fd(self, proc, fd, role):
        before=self.process_observation(proc);first=fd.stat()
        last=fd.stat();after=self.process_observation(proc)
        if before!=after or (first.st_dev,first.st_ino)!=(last.st_dev,last.st_ino):
            raise Refused('fd_full_identity_rebound')
        return dict(before,role=role,dev=last.st_dev,inode=last.st_ino,fd=int(fd.name))

    def worker_snapshot(self, binding):
        first=self.unit(binding['unit']);pid=int(first.get('MainPID') or 0)
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if pid>0:
            proc=Path('/proc')/str(pid);before=self.process_observation(proc)
            after=self.process_observation(proc);last=self.unit(binding['unit'])
            if before!=after or before['cgroup']!=first.get('ControlGroup') or any(first.get(k)!=last.get(k) for k in ('MainPID','InvocationID','ControlGroup','ActiveState')):
                raise Refused('worker_snapshot_rebound')
            return dict(before,unit=binding['unit'],invocation=first['InvocationID'],state=first['ActiveState'])
        # A terminal unit requires same generation, original PID gone and subtree empty.
        original=Path('/proc')/str(binding['pid'])
        try:gone=(original/'stat').read_text().rsplit(')',1)[1].split()[19]!=binding['start']
        except FileNotFoundError:gone=True
        group=first.get('ControlGroup')
        directory=Path('/sys/fs/cgroup'+str(group))
        empty=bool(group) and not directory.exists()
        if group and directory.exists():
            events=dict(line.split() for line in (directory/'cgroup.events').read_text().splitlines());empty=events.get('populated')=='0'
        last=self.unit(binding['unit'])
        if any(first.get(k)!=last.get(k) for k in ('MainPID','InvocationID','ControlGroup','ActiveState')):
            raise Refused('terminal_unit_changed')
        return {'unit':binding['unit'],'pid':0,'boot':boot,'uid':os.getuid(),
                'invocation':first.get('InvocationID'),'cgroup':group,'state':first.get('ActiveState'),
                'original_gone':gone,'cgroup_empty':empty}

    def writer_census(self):
        try:
            workers = validate_workers(self.b['tolerated_workers'], self.b['auxiliary_units'] + MAIN + self.b['timers'])
            protected = self.b['protected_db_files']
            expected_roles = {'state.sqlite', 'state.sqlite-wal', 'state.sqlite-shm'}
            if set(protected) != expected_roles:
                raise WriterUnknown('protected_fd_set_unbound')
            for role, record in protected.items():
                value = (SHARE / role).stat()
                if (value.st_dev, value.st_ino) != (record['dev'], record['inode']):
                    raise WriterUnknown('canonical_db_identity_changed')
            observations = {name: self.worker_snapshot(w) for name, w in workers.items()}
            for name,w in workers.items():
                (require_terminal if w.get('phase','ACTIVE')=='PREFINISHED' else require_same_worker)(w,observations[name])
                if w.get('phase','ACTIVE')=='PREFINISHED':
                    receipt=read_exit(w['log_exit']['sealed'],w,0)
                    if classify_post(w,observations[name],w['pre_registry'],receipt,0)['state']!='PREFINISHED_RECONCILED':
                        raise WriterUnknown('prefinished_not_independently_reconciled')
            accepted = []
            for proc in Path('/proc').iterdir():
                if self.phase == 'work': self.remaining()
                if not proc.name.isdigit() or int(proc.name) == os.getpid(): continue
                try:
                    if proc.stat().st_uid != os.getuid(): continue
                    for fd in (proc / 'fd').iterdir():
                        if self.phase == 'work': self.remaining()
                        try:
                            target = os.readlink(fd).removesuffix(' (deleted)')
                            value = fd.stat()
                        except FileNotFoundError: continue
                        roles = [role for role, r in protected.items()
                                 if (value.st_dev, value.st_ino) == (r['dev'], r['inode']) or target == str(SHARE / role)]
                        if not roles: continue
                        if len(roles) != 1: raise WriterUnknown('ambiguous_protected_fd')
                        record = self.capture_fd(proc, fd, roles[0])
                        unit = classify_fd(record, workers, observations, protected)
                        accepted.append(dict(record, unit=unit))
                except FileNotFoundError:
                    continue
                except PermissionError as exc:
                    raise WriterUnknown('writer_census_permission_UNKNOWN') from exc
            # Rejoin the same actual leader/unit generation after observing all FDs.
            for name,w in workers.items():
                (require_terminal if w.get('phase','ACTIVE')=='PREFINISHED' else require_same_worker)(w,self.worker_snapshot(w))
            for role, record in protected.items():
                value = (SHARE / role).stat()
                if (value.st_dev, value.st_ino) != (record['dev'], record['inode']):
                    raise WriterUnknown('canonical_db_identity_changed_after_census')
            self.log('exact_tolerated_fd_census', accepted=accepted,
                     no_unlisted_writer_observed=True, universal_writer_absence=False)
            active = self.require(['/usr/bin/systemctl', '--user', 'list-units', '--type=service',
                                   '--state=active,activating', '--no-legend', '--plain'])
            if any(line.strip().startswith('clarp-bookkeeping-') for line in active.splitlines()):
                raise WriterUnknown('bookkeeping_reactivated')
        except (WriterUnknown, KeyError, ValueError, IndexError) as exc:
            raise Refused('writer_classification:' + str(exc)) from exc

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
        self.log('only_exact_tolerated_writers_plus_owner_inventory_not_universal_proof')

    def observe_balance_terminal(self):
        if self.balance_origin is None:
            self.no_balance();return
        target=self.balance_origin+300;end=self.balance_origin+420
        for attempt in range(96):
            now=time.monotonic()
            if now>target and not self.target300_missed:
                self.target300_missed=True
                self.operational_errors.append('terminal_target300_missed')
                self.log('terminal_target300_missed',at=now,origin=self.balance_origin)
            if now>=end:break
            # Owned.wait may need six seconds to retire its private helper.
            # Do not spend that known allowance beyond the420 observation boundary.
            latest=end-6
            if now>=latest:
                time.sleep(end-now);break
            deadline=target-6 if now<target-6 else latest
            try:
                self.no_balance(deadline)
                observed=time.monotonic()
                self.log('balance_terminal_confirmed',observed=observed,
                         phase_elapsed=observed-self.balance_origin,target300_met=observed<=target)
                if observed>target and not self.target300_missed:
                    self.target300_missed=True
                    self.operational_errors.append('terminal_target300_missed')
                return
            except BaseException as exc:
                detail=type(exc).__name__+':'+str(exc)
                if self.abort_requested or isinstance(exc,(KeyboardInterrupt,SystemExit)):
                    self.operational_errors.append('status_control_abort:'+detail)
                self.log('balance_status_UNKNOWN',attempt=attempt,error=detail,
                         abort_latched=self.abort_requested,traceback=traceback.format_exc())
            left=end-time.monotonic()
            if left>0:time.sleep(min(5,left))
        if time.monotonic()<end:raise Refused('status_observation_budget_UNKNOWN')
        if not self.b['balance_timing']['overlap_at420']:
            raise Refused('overlap_disposition_unbound')
        # Latch before diagnostics or starts. NEVER fabricate balance_terminal.
        self.overlap_recovery=True
        self.operational_errors.append('UNKNOWN_BALANCE_OVERLAP_RECOVERY')
        self.log('UNKNOWN_BALANCE_OVERLAP_RECOVERY',phase_elapsed=time.monotonic()-self.balance_origin,
                 terminal_confirmed=False,auxiliary_restore_not_authorized=True)

    def cancel_balance(self):
        if self.cancel_attempted:raise Refused('cancel_already_attempted_no_renewed_budget')
        self.cancel_attempted=True;self.phase='cancel'
        requested=time.monotonic()
        if self.balance_origin is not None and requested>self.balance_origin+240:
            self.operational_errors.append('cancel240_request_missed')
        self.last_gate_bracket=None
        previous_deadline=self.command_deadline
        # SAME first-balance clock: setup, parent grant, child exec and wait all
        # bounded by240. A missed admission is UNKNOWN, never a late retry.
        self.command_deadline=self.balance_origin+240 if self.balance_origin is not None else None
        try:
            if self.command_deadline is None:raise Refused('cancel_clock_unbound')
            rc,_=self.command(['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','cancel','/'],60)
            upper=self.last_gate_bracket[1] if self.last_gate_bracket else None
            if self.balance_origin is not None and (upper is None or upper>self.balance_origin+240):
                self.operational_errors.append('cancel240_exec_grant_unproven_or_late')
            self.log('balance_cancel_return',requested=requested,origin=self.balance_origin,rc=rc,exec_grant_bracket=self.last_gate_bracket,
                     returned=time.monotonic())
        except BaseException as exc:
            self.operational_errors.append('cancel:'+type(exc).__name__+':'+str(exc))
            self.log('cancel_command_UNKNOWN',requested=requested,origin=self.balance_origin,
                     admission_deadline=self.command_deadline,exec_grant_bracket=self.last_gate_bracket,
                     observed=time.monotonic(),traceback=traceback.format_exc())
        finally:
            self.command_deadline=previous_deadline
        # Cancellation/status precede any balance-client TERM/KILL cleanup.
        self.observe_balance_terminal()
        if self.balance_terminal and self.balance_child is not None and not self.balance_child.reap_started:
            try:self.balance_child.cleanup()
            except BaseException as exc:
                self.operational_errors.append('balance_client_cleanup:'+str(exc))
                self.log('balance_client_cleanup_UNKNOWN',traceback=traceback.format_exc())

    def pass_balance(self,usage):
        self.phase='work';self.remaining()
        if usage==25 and (self.balance_origin is None or time.monotonic()-self.balance_origin>150):
            self.log('pass25_NOT_STARTED_elapsed150');return False
        self.writer_census()
        if usage==25 and time.monotonic()-self.balance_origin>150:
            self.log('pass25_NOT_STARTED_after_census150');return False
        self.seq+=1;output=self.e/('balance-%d.log'%usage)
        with output.open('xb') as f:
            self.remaining()
            if self.balance_origin is None:
                self.balance_origin=time.monotonic() # BEFORE all command setup/exec; never reset.
                self.log('balance_phase_origin',at=self.balance_origin,
                         boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            deadline=self.work_dispatch_deadline()
            if usage==25:deadline=min(deadline,self.balance_origin+150)
            previous_terminal=self.balance_terminal
            self.balance_terminal=False
            begin=time.monotonic()
            try:
                child=Owned(['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','start','-dusage='+str(usage),'/'],
                            f,ENV,native_sources=self.b.get('native_sources'),work_permit=DispatchPermit(deadline,self))
            except CustodyUnknown as exc:
                ev=exc.child_evidence
                if (usage==25 and ev.get('gate_released') is False and ev.get('birth') is not None
                        and not ev.get('cleanup_errors') and not ev.get('child_cleanup_error')
                        and 'work_permit_expired_before_exec' in str(ev.get('primary'))
                        and time.monotonic()>=self.balance_origin+150 and not self.abort_requested):
                    self.balance_terminal=previous_terminal
                    self.log('pass25_NOT_STARTED_setup150',evidence=ev);return False
                self.log('balance_launch_custody_UNKNOWN',evidence=ev);raise
            granted=time.monotonic()
            self.children.append(child);self.balance_child=child
            self.log('balance_exec_grant_bracket',usage=usage,bracket=[begin,granted],origin=self.balance_origin)
            wait_end=min(self.work_deadline,self.balance_origin+232)
            # Unlike Owned.wait, this retains the reservation WITHOUT timeout cleanup.
            if not child.terminal_before(wait_end):
                self.cancel_balance()
                self.log('balance_work_ended_by_approved_phase_cap',terminal_confirmed=self.balance_terminal,
                         overlap=self.overlap_recovery)
                return False
            rc=child.wait(0)
            self.log('balance_pass',usage=usage,rc=rc,phase_elapsed=time.monotonic()-self.balance_origin)
            if rc:raise Refused('balance_nonzero')
            self.no_balance(deadline=min(self.work_deadline,self.balance_origin+232))
            self.balance_child=None
            return True

    def usage(self):
        text = self.require(['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'filesystem', 'usage', '-b', '/'])
        m = re.search(r'^\s*Device unallocated:\s*(\d+)\s*$', text, re.M)
        if not m:
            raise Refused('unallocated_parse_UNKNOWN')
        return int(m.group(1))

    def restore(self):
        if not self.stopped:
            return
        self.phase='recovery'
        if self.overlap_recovery:
            if self.balance_origin is None or time.monotonic()-self.balance_origin<420 or not self.b['balance_timing']['overlap_at420']:
                raise Refused('invalid_overlap_recovery')
            # Explicit narrowpair only; balance stays UNKNOWN/non-green.
            units=['clarp-runtime.service','clarp.service']
            self.log('overlap_auxiliary_restore_NOT_ATTEMPTED',units=self.b['auxiliary_units']+self.b['timers'],
                     host_autostarted_auxiliary_state='UNKNOWN_OWNER_MUST_RECHECK')
        else:
            if not self.balance_terminal:self.no_balance()
            units=['clarp-runtime.service','clarp.service']+self.b['auxiliary_units']+self.b['timers']
        self.same_release()
        failures = []
        for name in units:
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
            validate_heartbeat_age(self.b['tolerated_workers'],int(time.time()*1000))
            self.stop()
            self.pass_balance(10)
            if not self.cancel_attempted:
                unallocated=self.usage()
                if unallocated<2*1024**3:self.pass_balance(25)
                if not self.cancel_attempted:self.usage()
        except BaseException as exc:
            primary = type(exc).__name__ + ':' + str(exc)
            self.operational_errors.append(primary)
            self.log('primary_abort', reason=primary)
        finally:
            try:
                if not self.balance_terminal and not self.cancel_attempted:
                    self.cancel_balance()
                self.restore()
            except BaseException as exc:
                primary = (primary or '') + '|recovery_UNKNOWN'
                self.operational_errors.append(type(exc).__name__ + ':' + str(exc))
                self.log('RECOVERY_UNKNOWN_NEEDS_EXTERNAL_ATTENTION', reason=self.operational_errors[-1])
            if self.abort_requested:
                self.operational_errors.append('controller_abort_latched_'+str(self.abort_signal))
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
    if b.get('balance_timing')!={'cancel_by':240,'skip25_after':150,'terminal_target':300,'overlap_at':420,'overlap_at420':True} or not b.get('lead_overlap_disposition_receipt'):
        raise Refused('exact_timing_disposition_unbound')
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
    validate_workers(b['tolerated_workers'], MAIN + b['auxiliary_units'] + b['timers'])
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
