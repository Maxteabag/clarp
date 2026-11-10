#!/usr/bin/python3
"""SOURCE CANDIDATE. Requires reviewed, fresh owner binding and two approvals."""
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
from owned import Owned, CustodyUnknown, DispatchPermit
from writer_policy import validate_workers, require_same_worker, classify_fd, WriterUnknown, require_terminal, read_exit, classify_post, validate_heartbeat_age
import sys
import time
import traceback
from roster import admit as admit_current_roster, check_admission, FirstStopPermit, read_bound

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
        self.protected_root = SHARE
        self.roster_admission = None
        self.first_stop_dispatch_deadline = None

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
            permit_deadline=deadline
            if self.first_stop_dispatch_deadline is not None:
                permit_deadline=min(permit_deadline,self.first_stop_dispatch_deadline) if permit_deadline is not None else self.first_stop_dispatch_deadline
            permit=DispatchPermit(permit_deadline,self if self.phase=='work' else CleanupPermitOwner()) if permit_deadline is not None else None
            if self.first_stop_dispatch_deadline is not None:permit=FirstStopPermit(permit_deadline,self)
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

    def manager_residual_document(self, ref, schema, seals):
        if (not isinstance(ref,dict) or set(ref)!={'path','sha256'}
            or type(ref['path']) is not str or not Path(ref['path']).is_absolute()
            or type(ref['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',ref['sha256'])):
            raise WriterUnknown('manager_residual_reference_shape')
        raw,seal=read_bound(ref['path'],limit=65536,private=True)
        if seal['sha256']!=ref['sha256']:raise WriterUnknown('manager_residual_document_hash')
        value=json.loads(raw)
        if not isinstance(value,dict) or (schema is not None and value.get('schema')!=schema):
            raise WriterUnknown('manager_residual_document_schema')
        seals.append(seal)
        return value

    def authorize_manager_residual(self):
        # No caller Boolean or context-only locks/FD-store data can activate this.
        ref=self.b.get('manager_fd_residual_policy')
        if ref is None:return None
        seals=[]
        policy=self.manager_residual_document(ref,'clarp.manager-fd-residual-policy/1',seals)
        if policy.get('enabled') is not True or policy.get('owner_acceptance') is None:
            raise WriterUnknown('manager_residual_policy_disabled_or_owner_answer_absent')
        receipt=self.manager_residual_document(policy['owner_acceptance'],'clarp.owner-accepted-manager-fd-residual/1',seals)
        transport=self.manager_residual_document(receipt.get('decision_answer'),None,seals)
        artifact=transport.get('artifact',{})
        answer=artifact.get('decision',{}) if isinstance(artifact,dict) else {}
        card='decision-c147190cebf96302dfe418d4'
        artifact_id='artifact-6403454082e0fddbf03af602'
        actual=answer.get('answer',{}) if isinstance(answer,dict) else {}
        if (not isinstance(answer,dict) or not isinstance(actual,dict)
            or artifact.get('artifact_id')!=artifact_id or artifact.get('session')!='hobbles-18b0'
            or artifact.get('type')!='question' or artifact.get('status')!='completed'
            or answer.get('artifact_id')!=artifact_id or answer.get('decision_id')!=card
            or answer.get('status')!='answered' or answer.get('resolved_choice')!='answered'
            or answer.get('resolved_by')!='user'
            or type(answer.get('resolved_at')) is not int or answer['resolved_at']<=0
            or actual.get('option_id')!='tonight'
            or type(actual.get('label')) is not str or not 0<len(actual['label'])<=4096
            or not isinstance(answer.get('options'),list)
            or sum(1 for option in answer['options'] if isinstance(option,dict)
                   and option.get('id')=='tonight' and option.get('label')==actual['label'])!=1
            or receipt.get('owner')!='Peter' or receipt.get('decision_card_id')!=card
            or receipt.get('artifact_id')!=artifact_id or receipt.get('option_id')!='tonight'
            or type(receipt.get('resolved_at')) is not int or receipt['resolved_at']!=answer['resolved_at']
            or receipt.get('actual_answer')!=actual['label']
            or self.b.get('peter_decision')!=card or self.b.get('peter_yes') is not True
            or receipt.get('state')!='OWNER_ACCEPTED_RESIDUAL'
            or receipt.get('accepted_scope')!='BOUND_USER_MANAGER_READLINK_EACCES_ONLY'
            or receipt.get('proof_of_nonwriter') is not False
            or receipt.get('writer_inventory_reconciled') is not False):
            raise WriterUnknown('manager_residual_actual_owner_acceptance_unbound')
        # Final B/lead capsule authenticates the actual card-answer transport.
        # These source checks bind its exact private bytes; they do not sign it.
        manager=receipt.get('manager')
        keys={'pid','start','uid','boot','comm','cgroup'}
        if (not isinstance(manager,dict) or set(manager)!=keys
            or type(manager['pid']) is not int or manager['pid']!=1331
            or type(manager['uid']) is not int or manager['uid']!=1000 or os.getuid()!=1000
            or manager['start']!='1168'
            or manager['boot']!='ba9283af-aabf-4244-9606-ac4361a968f5'
            or manager['comm']!='systemd'
            or manager['cgroup']!='/user.slice/user-1000.slice/user@1000.service/init.scope'
            or policy.get('manager')!=manager or self.b.get('manager_residual_identity')!=manager):
            raise WriterUnknown('manager_residual_exact_identity_unbound')
        scope_ref=receipt.get('manager_exe_scope')
        scope=self.manager_residual_document(scope_ref,'clarp.manager-exe-residual-scope/1',seals)
        exe_evidence={'state':'UNAVAILABLE_EACCES','errno':13,
                      'proof_of_executable_identity':False,'same_pid_reexec_not_excluded':True}
        if (scope.get('issuer')!='hobbles-18b0' or scope.get('decision_card_id')!=card
            or scope.get('state')!='OWNER_ACCEPTED_RESIDUAL'
            or scope.get('actual_Peter_answer_ref')!=receipt['decision_answer']
            or scope.get('manager')!=manager
            or scope.get('allowed_operations')!=['exe_readlink_EACCES','fd_readlink_EACCES']
            or scope.get('proof_of_executable_identity') is not False
            or scope.get('same_pid_reexec_not_excluded') is not True
            or receipt.get('manager_exe_evidence')!=exe_evidence
            or policy.get('manager_exe_evidence')!=exe_evidence
            or self.b.get('manager_exe_evidence')!=exe_evidence):
            raise WriterUnknown('manager_exe_explicit_owner_scope_unbound')
        for value in (receipt.get('manager_exe_evidence'),policy.get('manager_exe_evidence'),self.b.get('manager_exe_evidence')):
            if (not isinstance(value,dict) or set(value)!=set(exe_evidence)
                or type(value.get('errno')) is not int or value['errno']!=13
                or value.get('state')!='UNAVAILABLE_EACCES'
                or value.get('proof_of_executable_identity') is not False
                or value.get('same_pid_reexec_not_excluded') is not True):
                raise WriterUnknown('manager_exe_evidence_not_strictly_typed')
        lifetime=receipt.get('manager_lifetime_context')
        zero=[['monotonic','0','0'],['boottime','0','0']]
        if (not isinstance(lifetime,dict) or set(lifetime)!={'bus_socket_version','zero_clock_offsets'}
            or lifetime.get('zero_clock_offsets')!={'owner':zero,'manager':zero}
            or not isinstance(lifetime.get('bus_socket_version'),list) or len(lifetime['bus_socket_version'])!=7
            or any(type(x) is not int or x<0 for x in lifetime['bus_socket_version'])
            or lifetime['bus_socket_version'][5]!=1000 or not stat.S_ISSOCK(lifetime['bus_socket_version'][6])
            or policy.get('manager_lifetime_context')!=lifetime or self.b.get('manager_lifetime_context')!=lifetime):
            raise WriterUnknown('manager_lifetime_context_unbound')
        raw,mfseal=read_bound(Path(__file__).with_name('SHA256SUMS'),limit=65536,private=True)
        if receipt.get('controller_MF_sha256')!=mfseal['sha256']:
            raise WriterUnknown('manager_residual_controller_MF_unbound')
        seals.append(mfseal)
        if (self.b.get('writer_inventory_reconciled') is not False
            or self.b.get('writer_inventory_accounting')!='ACCOUNTED_EXCEPT_OWNER_ACCEPTED_MANAGER_RESIDUAL'):
            raise WriterUnknown('manager_residual_must_not_claim_inventory_TRUE')
        return {'manager':manager,'manager_lifetime_context':lifetime,'manager_exe_evidence':exe_evidence,
                'manager_exe_scope_ref':scope_ref,'seals':seals,'policy_ref':ref,
                'owner_acceptance_ref':policy['owner_acceptance'],'decision_answer_ref':receipt['decision_answer'],
                'state':'OWNER_ACCEPTED_RESIDUAL','proof_of_nonwriter':False,'writer_inventory_reconciled':False}

    def manager_residual_observation(self, authorization):
        p=Path('/proc')/str(authorization['manager']['pid'])
        before=self.process_observation(p);comm=(p/'comm').read_text().strip()
        # ONLY exact manager exe-readlink/EACCES is an explicitly accepted
        # missing executable-identity check. No guessed path, no reexec proof.
        try:
            observed_exe=os.readlink(p/'exe')
        except PermissionError as exc:
            if exc.errno!=errno.EACCES:raise
            evidence=dict(authorization['manager_exe_evidence'])
            error={'type':type(exc).__name__,'errno':exc.errno,'message':str(exc),'path':str(p/'exe')}
        else:
            raise WriterUnknown('manager_exe_evidence_state_changed_readable:'+str(observed_exe)[:1024])
        socket=Path('/run/user/1000/bus');z=socket.lstat()
        socket_version=[z.st_dev,z.st_ino,z.st_size,z.st_mtime_ns,z.st_ctime_ns,z.st_uid,z.st_mode]
        offsets={}
        for name,file in [('owner',Path('/proc/self/timens_offsets')),('manager',p/'timens_offsets')]:
            offsets[name]=[line.split() for line in file.read_text().splitlines()]
        lifetime={'bus_socket_version':socket_version,'zero_clock_offsets':offsets}
        after=self.process_observation(p);lastcomm=(p/'comm').read_text().strip();last=socket.lstat()
        final_socket=[last.st_dev,last.st_ino,last.st_size,last.st_mtime_ns,last.st_ctime_ns,last.st_uid,last.st_mode]
        positive=dict(after,comm=lastcomm)
        if (before!=after or comm!=lastcomm or positive!=authorization['manager']
            or socket_version!=final_socket or lifetime!=authorization['manager_lifetime_context']):
            raise WriterUnknown('manager_residual_positive_identity_or_lifetime_changed')
        self.log('manager_executable_OWNER_ACCEPTED_RESIDUAL',state='OWNER_ACCEPTED_RESIDUAL',
                 positive_identity=positive,executable_identity=evidence,actual_error=error,
                 manager_lifetime_context=lifetime,owner_acceptance=authorization['owner_acceptance_ref'],
                 lead_scope=authorization['manager_exe_scope_ref'],proof_of_nonwriter=False,
                 writer_inventory_reconciled=False)
        if self.diagnostic_errors:raise WriterUnknown('manager_residual_evidence_fault')
        return {'positive_identity':positive,'exe_evidence':evidence,'manager_lifetime_context':lifetime}

    def recheck_manager_residual(self, authorization):
        # Required authorization/native reads are NEVER descriptor-disappearance
        # races. Latch even a once-only FNF as chained UNKNOWN before FD catches.
        try:
            for seal in authorization['seals']:
                read_bound(seal['path'],expected=seal,limit=65536,private=True)
            return self.manager_residual_observation(authorization)
        except Exception as exc:
            raise WriterUnknown('manager_residual_recheck_UNKNOWN:'+type(exc).__name__) from exc

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
            residual=self.authorize_manager_residual()
            manager_before=self.recheck_manager_residual(residual) if residual is not None else None
            accepted = [];residual_fds=[]
            for proc in Path('/proc').iterdir():
                if self.phase == 'work': self.remaining()
                if not proc.name.isdigit() or int(proc.name) == os.getpid(): continue
                try:
                    if proc.stat().st_uid != os.getuid(): continue
                    for fd in (proc / 'fd').iterdir():
                        if self.phase == 'work': self.remaining()
                        try:
                            try:
                                target = os.readlink(fd).removesuffix(' (deleted)')
                            except PermissionError as exc:
                                # ONLY this operation/errno on the exact accepted manager.
                                # Directory enumeration, fd.stat and all other PIDs still refuse.
                                if exc.errno!=errno.EACCES or residual is None or int(proc.name)!=1331:
                                    raise
                                if self.recheck_manager_residual(residual)!=manager_before:
                                    raise WriterUnknown('manager_residual_census_rebound') from exc
                                if not fd.name.isdigit() or len(residual_fds)>=4096:
                                    raise WriterUnknown('manager_residual_fd_shape_or_cap') from exc
                                row={'pid':1331,'fd':int(fd.name),'path':str(fd),'errno':exc.errno,
                                     'error_type':type(exc).__name__,'error':str(exc),
                                     'state':'OWNER_ACCEPTED_RESIDUAL','identity':manager_before,
                                     'owner_acceptance':residual['owner_acceptance_ref'],
                                     'proof_of_nonwriter':False,'writer_inventory_reconciled':False}
                                self.log('manager_fd_OWNER_ACCEPTED_RESIDUAL',**row)
                                if self.diagnostic_errors:raise WriterUnknown('manager_residual_evidence_fault') from exc
                                residual_fds.append(row)
                                continue
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
            if residual is not None:
                if self.recheck_manager_residual(residual)!=manager_before:
                    raise WriterUnknown('manager_residual_final_identity_rebound')
                self.log('exact_tolerated_fd_census',accepted=accepted,
                         state='ACCOUNTED_EXCEPT_OWNER_ACCEPTED_MANAGER_RESIDUAL',
                         manager_fd_residuals=residual_fds,writer_inventory_reconciled=False,
                         no_unlisted_writer_observed=False,universal_writer_absence=False)
                if self.diagnostic_errors:raise WriterUnknown('manager_residual_evidence_fault')
            else:
                self.log('exact_tolerated_fd_census', accepted=accepted,
                         no_unlisted_writer_observed=True, universal_writer_absence=False)
            active = self.require(['/usr/bin/systemctl', '--user', 'list-units', '--type=service',
                                   '--state=active,activating', '--no-legend', '--plain'])
            if any(line.strip().startswith('clarp-bookkeeping-') for line in active.splitlines()):
                raise WriterUnknown('bookkeeping_reactivated')
        except (WriterUnknown, KeyError, ValueError, IndexError) as exc:
            raise Refused('writer_classification:' + str(exc)) from exc

    def listener_cgroup_path(self, cgroup):
        exact='clarp-bookkeeping-dg3c76d02ac7bed15c.service'
        root=Path('/sys/fs/cgroup')
        if (not isinstance(cgroup,str) or not cgroup.startswith('/') or not cgroup.endswith('/'+exact)
            or str(Path(cgroup))!=cgroup or '..' in Path(cgroup).parts):
            raise Refused('listener_old_cgroup_path_unbound')
        if root.resolve(strict=True)!=root:raise Refused('listener_cgroup_root_symlink')
        leaf=Path(str(root)+cgroup);parents=[];current=root
        for part in Path(cgroup).parts[1:-1]:
            current=current/part;st=current.lstat()
            if not stat.S_ISDIR(st.st_mode):raise Refused('listener_cgroup_parent_type_or_symlink')
            parents.append((str(current),[st.st_dev,st.st_ino,st.st_uid,st.st_mode]))
        st=root.lstat()
        if not stat.S_ISDIR(st.st_mode):raise Refused('listener_cgroup_root_type')
        parents.insert(0,(str(root),[st.st_dev,st.st_ino,st.st_uid,st.st_mode]))
        namespace={name:os.readlink('/proc/self/ns/'+name) for name in ('mnt','pid','cgroup')}
        return leaf,parents,namespace

    def listener_cgroup_events(self, leaf):
        before=leaf.lstat()
        if not stat.S_ISDIR(before.st_mode):raise Refused('listener_old_cgroup_type_or_symlink')
        fd=os.open(leaf/'cgroup.events',os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
        try:
            a=os.fstat(fd);raw=os.read(fd,4097);z=os.fstat(fd)
            if len(raw)>4096 or (a.st_dev,a.st_ino)!=(z.st_dev,z.st_ino):raise Refused('listener_cgroup_events_rebound_or_size')
        finally:os.close(fd)
        after=leaf.lstat();ident=lambda q:[q.st_dev,q.st_ino,q.st_uid,q.st_mode]
        if ident(before)!=ident(after):raise Refused('listener_old_cgroup_identity_rebound')
        events={}
        for line in raw.decode('ascii','strict').splitlines():
            pair=line.split()
            if len(pair)!=2 or pair[0] in events or pair[1] not in ('0','1'):raise Refused('listener_cgroup_events_shape')
            events[pair[0]]=pair[1]
        if events.get('populated') not in ('0','1'):raise Refused('listener_cgroup_populated_missing')
        return ident(after),events,raw.decode('ascii')

    def bind_listener_before_stop(self, name):
        exact='clarp-bookkeeping-dg3c76d02ac7bed15c.service'
        spec=self.b['bookkeeping_restore'];old=spec['previous_native']
        if (name!=exact or spec['unit']!=exact or spec['delegation_id']!='dg3c76d02ac7bed15c'
            or type(old['pid']) is not int or old['pid']<=0 or not isinstance(old['start'],str)
            or not re.fullmatch('[0-9]+',old['start']) or old['uid']!=os.getuid()
            or not re.fullmatch('[0-9a-f]{32}',old['invocation'])
            or old['invocation']!=self.b['expected_active_invocations'][name]):
            raise Refused('listener_prior_stop_binding')
        first=self.unit(name);proc=Path('/proc')/str(old['pid']);native=self.process_observation(proc)
        if (first.get('ActiveState')!='active' or first.get('MainPID')!=str(old['pid'])
            or first.get('InvocationID')!=old['invocation'] or first.get('ControlGroup')!=old['cgroup']
            or any(native[k]!=old[k] for k in ('pid','start','uid','cgroup','boot'))):
            raise Refused('listener_prior_unit_or_birth_changed')
        leaf,parents,namespace=self.listener_cgroup_path(old['cgroup']);identity,events,raw=self.listener_cgroup_events(leaf)
        last=self.unit(name);after=self.process_observation(proc)
        if (after!=native or first!=last or events['populated']!='1'):
            raise Refused('listener_prior_native_or_group_changed')
        self.listener_stop_bound={'unit':name,'native':dict(old),'cgroup_path':str(leaf),'cgroup_identity':identity,
                                  'parents':parents,'namespace':namespace,'pre_unit':first,'pre_events':raw,'pre_mono':time.monotonic()}
        self.listener_stop_rc0_ordinal=None
        self.log('bookkeeping_before_owned_stop_bound',binding=self.listener_stop_bound)
        if self.diagnostic_errors:raise Refused('listener_prior_stop_evidence_fault')

    def verify_bound_listener_stopped(self, name, state):
        bound=self.listener_stop_bound;old=bound['native']
        if name!=bound['unit'] or self.listener_stop_rc0_ordinal is None:raise Refused('listener_successful_owned_stop_unproven')
        if (state.get('ActiveState') not in ('inactive','failed') or state.get('MainPID')!='0'
            or state.get('InvocationID') not in ('',old['invocation'])):
            raise Refused('listener_replacement_or_post_state_unknown')
        def old_birth_gone():
            if Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=old['boot']:raise Refused('listener_stop_boot_changed')
            path=Path('/proc')/str(old['pid'])/'stat'
            try:raw=path.read_bytes()
            except (FileNotFoundError,ProcessLookupError):
                return {'state':'EXACT_PRIOR_PID_ABSENT','path':str(path)}
            if len(raw)>8192:raise Refused('listener_prior_pid_stat_size')
            fields=raw.decode('utf8','strict').rsplit(')',1)[1].split();birth=fields[19]
            if not re.fullmatch('[0-9]+',birth):raise Refused('listener_prior_pid_birth_unknown')
            if birth==old['start']:raise Refused('listener_prior_pid_birth_still_live')
            return {'state':'PID_REUSED_DIFFERENT_BIRTH_NO_ACTION','path':str(path),'observed_start':birth,
                    'raw_sha256':hashlib.sha256(raw).hexdigest()}
        first_birth=old_birth_gone()
        leaf,parents,namespace=self.listener_cgroup_path(old['cgroup'])
        if parents!=bound['parents'] or namespace!=bound['namespace'] or str(leaf)!=bound['cgroup_path']:
            raise Refused('listener_cgroup_parent_namespace_rebound')
        try:leaf_stat=leaf.lstat()
        except FileNotFoundError:
            group={'state':'EXACT_PRIOR_CGROUP_ABSENT','path':str(leaf)}
        else:
            identity,events,raw=self.listener_cgroup_events(leaf)
            if identity!=bound['cgroup_identity'] or events['populated']!='0':raise Refused('listener_prior_cgroup_rebound_or_populated')
            group={'state':'SAME_PRIOR_CGROUP_POPULATED_ZERO','path':str(leaf),'identity':identity,'raw_events':raw}
        final_state=self.unit(name);last_birth=old_birth_gone()
        _,final_parents,final_namespace=self.listener_cgroup_path(old['cgroup'])
        if (state!=final_state or final_parents!=parents or final_namespace!=namespace):
            raise Refused('listener_post_stop_unit_or_namespace_rebound')
        # Repeat leaf-state proof after final native/unit joins, not just parents.
        try:final_leaf=leaf.lstat()
        except FileNotFoundError:
            if group['state']!='EXACT_PRIOR_CGROUP_ABSENT':raise Refused('listener_old_cgroup_disappeared_during_proof')
        else:
            if group['state']!='SAME_PRIOR_CGROUP_POPULATED_ZERO':raise Refused('listener_old_cgroup_reappeared')
            identity,events,raw=self.listener_cgroup_events(leaf)
            if identity!=bound['cgroup_identity'] or events['populated']!='0':raise Refused('listener_old_cgroup_final_rebound_or_populated')
        self.log('bookkeeping_owned_stop_old_instance_proven_gone',unit=name,prior=bound,
                 successful_stop_command_ordinal=self.listener_stop_rc0_ordinal,post_state=final_state,
                 first_birth=first_birth,last_birth=last_birth,old_subtree=group,
                 unchanged_writer_census_still_required=True)
        if self.diagnostic_errors:raise Refused('listener_post_stop_evidence_fault')

    def stop(self):
        if 'clarp-bookkeeping-dg3c76d02ac7bed15c.service' not in self.b['auxiliary_units']:
            raise Refused('listener_required_stop_list_missing')
        check_admission(self)
        if self.abort_requested:
            raise Refused('abort_before_any_stop')
        # Mark before first stop: partial stop still needs restoration.
        self.stopped = True
        self.offline_start = time.monotonic()
        self.work_deadline = self.offline_start + 600
        self.phase = 'work'
        for index,name in enumerate(self.b['timers'] + self.b['auxiliary_units'] + MAIN):
            if index==0:self.first_stop_dispatch_deadline=self.roster_admission['deadline']
            try:
                if name=='clarp-bookkeeping-dg3c76d02ac7bed15c.service':self.bind_listener_before_stop(name)
                self.require(['/usr/bin/systemctl', '--user', 'stop', name], 45)
                if name=='clarp-bookkeeping-dg3c76d02ac7bed15c.service':self.listener_stop_rc0_ordinal=self.seq
            finally:self.first_stop_dispatch_deadline=None
        for name in self.b['auxiliary_units'] + MAIN:
            state = self.unit(name)
            if state.get('ActiveState') not in ('inactive', 'failed') or state.get('MainPID') != '0':
                raise Refused('unit_not_stopped_' + name)
            if name=='clarp-bookkeeping-dg3c76d02ac7bed15c.service':self.verify_bound_listener_stopped(name,state)
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

    def verify_host_recreated_bookkeeping(self, name):
        # This replaces ONLY the exact listener's existing 20s start slot.
        # Host ensure_running is sole recreation authority. No helper/start retry.
        origin=time.monotonic();deadline=origin+20
        old_deadline=self.command_deadline
        deadline=min(old_deadline,deadline) if old_deadline is not None else deadline
        spec=self.b['bookkeeping_restore']
        previous=spec['previous_native']
        exact='clarp-bookkeeping-dg3c76d02ac7bed15c.service'
        delegation='dg3c76d02ac7bed15c'
        def tick():
            if time.monotonic()>=deadline:raise Refused('bookkeeping_recreation_deadline')
        def source_role():
            self.same_release()
            role=Path(self.b['current_real'])/'lib/bookkeeping_listener.py'
            before=role.stat();raw=role.read_bytes();after=role.stat()
            version=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
            if version(before)!=version(after) or version(after)!=version(role.stat()) or hashlib.sha256(raw).hexdigest()!=spec['listener_source_sha256']:
                raise Refused('bookkeeping_role_source_changed')
        try:
            self.command_deadline=deadline
            tick()
            if (name!=exact or spec['unit']!=exact or spec['delegation_id']!=delegation
                or spec['recreation_authority']!='HOST_ENSURE_RUNNING_ONLY'
                or previous['invocation']!=self.b['expected_active_invocations'][name]
                or type(previous['pid']) is not int or previous['pid']<=0
                or not re.fullmatch(r'[0-9]+',previous['start'])):
                raise Refused('bookkeeping_recreation_binding')
            start_wall=time.time();source_role();tick()
            while True:
                tick()
                rc,text=self.command(['/usr/bin/systemctl','--user','show',name,
                    '--property=LoadState,ActiveState,SubState,MainPID,InvocationID,ControlGroup'],20)
                tick();state=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
                if rc not in (0,4) or state.get('LoadState') not in ('loaded','not-found'):
                    raise Refused('bookkeeping_unit_observation_UNKNOWN')
                if state.get('ActiveState')=='failed':raise Refused('bookkeeping_Host_recreation_failed')
                pending='unit_not_active'
                if state.get('ActiveState')=='active' and state.get('SubState')=='running':
                    pid=int(state.get('MainPID') or 0);inv=state.get('InvocationID','')
                    if pid<=0 or pid==previous['pid'] or not re.fullmatch('[0-9a-f]{32}',inv) or inv==previous['invocation']:
                        raise Refused('bookkeeping_not_new_invocation')
                    proc=Path('/proc')/str(pid);native=self.process_observation(proc);tick()
                    if (native['uid']!=os.getuid() or native['start']==previous['start']
                        or native['boot']!=previous['boot'] or native['cgroup']!=state.get('ControlGroup')
                        or native['cgroup']!=previous['cgroup'] or not native['cgroup'].endswith('/'+exact)):
                        raise Refused('bookkeeping_recreated_native_identity')
                    argv=(proc/'cmdline').read_bytes()
                    if len(argv)>8192 or argv.split(b'\0')[:-1]!=[spec['argv0'].encode(),b'-m',b'lib.bookkeeping_listener',delegation.encode()]:
                        raise Refused('bookkeeping_recreated_code_role')
                    if str((proc/'exe').resolve(strict=True))!=spec['native_executable'] or str(Path(spec['argv0']).resolve(strict=True))!=spec['native_executable']:
                        raise Refused('bookkeeping_recreated_interpreter')
                    rawenv=(proc/'environ').read_bytes()
                    if len(rawenv)>65536:raise Refused('bookkeeping_environment_size')
                    env=dict(item.split(b'=',1) for item in rawenv.split(b'\0') if b'=' in item)
                    if env.get(b'PYTHONPATH')!=str(SHARE/'current').encode() or str(Path(env.get(b'CLAUDE_PWA_DB',b'').decode()).resolve(strict=True))!=str(self.protected_root/'state.sqlite'):
                        raise Refused('bookkeeping_recreated_environment_role')
                    tick();db=self.protected_root/'state.sqlite';a=db.stat();expected=self.b['protected_db_files']['state.sqlite']
                    if (a.st_dev,a.st_ino)!=(expected['dev'],expected['inode']):raise Refused('bookkeeping_DB_identity')
                    token=native['boot']+':'+native['start']
                    sql=("PRAGMA query_only=ON;BEGIN;SELECT d.delegation_id,d.status AS delegation_status,"
                         "d.last_heartbeat_at,d.job_handle,j.job_id,j.generation,j.kind,j.status AS job_status,"
                         "j.worker_pid,j.worker_start_token,json_extract(j.metadata_json,'$.delegation_id') AS job_delegation "
                         "FROM goal_delegations d LEFT JOIN background_jobs j ON j.worker_pid="+str(pid)+
                         " AND j.worker_start_token='"+token+"' WHERE d.delegation_id='"+delegation+"';COMMIT;")
                    raw=self.require(['/usr/bin/sqlite3','-batch','-bail','-readonly','-json','-cmd','.timeout 2000',str(db),sql],10)
                    tick();z=db.stat()
                    if (z.st_dev,z.st_ino)!=(a.st_dev,a.st_ino):raise Refused('bookkeeping_DB_rebound')
                    rows=json.loads(raw)
                    if not isinstance(rows,list) or len(rows)!=1 or rows[0]['delegation_status']!='active':raise Refused('bookkeeping_delegation_unknown_or_inactive')
                    row=rows[0];pending='registration_or_role_heartbeat_not_yet_fresh'
                    if row['job_id'] is not None:
                        if (row['kind']!='listener' or row['job_delegation']!=delegation or row['job_status']!='running'
                            or row['worker_pid']!=pid or row['worker_start_token']!=token
                            or type(row['generation']) is not int or row['generation']<=0
                            or row['job_handle']!='bg1:'+str(row['generation'])+':'+row['job_id']):
                            raise Refused('bookkeeping_registry_role_mismatch')
                        beat=row['last_heartbeat_at'];now=time.time()
                        if type(beat) is not int or beat>int(now*1000):raise Refused('bookkeeping_heartbeat_unknown_or_future')
                        if beat>=int(start_wall*1000) and now-beat/1000<=20:
                            source_role();tick()
                            final_before=self.process_observation(proc)
                            final_argv=(proc/'cmdline').read_bytes()
                            final_exe=str((proc/'exe').resolve(strict=True))
                            final_argv0=str(Path(spec['argv0']).resolve(strict=True))
                            final_env=(proc/'environ').read_bytes()
                            final_db=db.stat()
                            after=self.process_observation(proc);last=self.unit(name);tick()
                            if (final_before!=native or after!=native
                                or final_argv!=argv or final_env!=rawenv
                                or final_exe!=spec['native_executable'] or final_argv0!=spec['native_executable']
                                or (final_db.st_dev,final_db.st_ino)!=(a.st_dev,a.st_ino)
                                or any(last.get(k)!=state.get(k) for k in ('ActiveState','SubState','MainPID','InvocationID','ControlGroup'))):
                                raise Refused('bookkeeping_recreated_role_or_identity_rebound')
                            self.log('bookkeeping_Host_recreation_positive',unit=name,delegation=delegation,native=native,
                                     invocation=inv,job_id=row['job_id'],generation=row['generation'],role_heartbeat_at=beat,
                                     origin=origin,deadline=deadline,no_manual_start=True)
                            tick();return
                elif state.get('ActiveState') not in ('inactive','activating','deactivating'):
                    raise Refused('bookkeeping_unknown_unit_state')
                self.log('bookkeeping_Host_recreation_pending',unit=name,reason=pending,deadline=deadline)
                tick();time.sleep(min(.5,max(0,deadline-time.monotonic())))
        finally:
            self.command_deadline=old_deadline

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
                if name=='clarp-bookkeeping-dg3c76d02ac7bed15c.service':
                    self.verify_host_recreated_bookkeeping(name)
                else:
                    rc, _ = self.command(['/usr/bin/systemctl', '--user', 'start', name], 20)
                    if rc:
                        failures.append(name + ':rc' + str(rc))
            except BaseException as exc:
                # Commit the operational failure BEFORE fallible diagnostics.
                failures.append(name + ':' + type(exc).__name__ + ':' + str(exc))
                if name=='clarp-bookkeeping-dg3c76d02ac7bed15c.service':
                    # Preserve the actual chain before this exact catch loses it.
                    actual_trace=traceback.format_exc()
                    try:
                        sys.stderr.write(actual_trace);sys.stderr.flush()
                    except BaseException as diagnostic:
                        self.diagnostic_errors.append('listener_raw_trace:'+type(diagnostic).__name__+':'+str(diagnostic))
                    self.log('bookkeeping_restore_chained_failure',unit=name,error=failures[-1],traceback=actual_trace)
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
            residual=self.authorize_manager_residual()
            if residual is not None:self.recheck_manager_residual(residual) # Actual native/exe before ANY command/stop.
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
            # Actual native/registry/image/FD capture after all prerequisites.
            # Cached caller registry rows cannot admit the first stop.
            admit_current_roster(self)
            self.stop()
            self.pass_balance(10)
            if not self.cancel_attempted:
                unallocated=self.usage()
                if unallocated<2*1024**3:self.pass_balance(25)
                if not self.cancel_attempted:self.usage()
        except BaseException as exc:
            primary = type(exc).__name__ + ':' + str(exc)
            self.operational_errors.append(primary)
            self.log('primary_abort',reason=primary,traceback=traceback.format_exc(),
                     private_roster_context=getattr(self,'roster_refusal_context',{'stage':'UNOBSERVED'}))
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


def validate_owner_binding(b):
    peter_card='decision-c147190cebf96302dfe418d4' if b.get('manager_fd_residual_policy') is not None else 'decision-0795e2c8e5ebd591f6c242a8'
    if b['schema'] != 'clarp.disk-maintenance-binding/1' or b['peter_decision'] != peter_card or b['peter_yes'] is not True or not b['lead_go_receipt']:
        raise Refused('approvals_unbound')
    if b.get('balance_timing')!={'cancel_by':240,'skip25_after':150,'terminal_target':300,'overlap_at':420,'overlap_at420':True} or not b.get('lead_overlap_disposition_receipt'):
        raise Refused('exact_timing_disposition_unbound')
    inventory_ok=(b['writer_inventory_reconciled'] is True) if b.get('manager_fd_residual_policy') is None else (b['writer_inventory_reconciled'] is False and b.get('writer_inventory_accounting')=='ACCOUNTED_EXCEPT_OWNER_ACCEPTED_MANAGER_RESIDUAL')
    if b['pre_state'] != 'ZERO' or not inventory_ok or not b['quiescence_receipt']:
        raise Refused('owner_PRE_or_quiescence_unbound')


def main():
    # Binding is prepared only after real approvals/owner PRE, not a sample production config.
    if len(sys.argv) != 4:
        raise Refused('usage_binding_sha_evidence')
    raw = Path(sys.argv[1]).read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != sys.argv[2]:
        raise Refused('binding_hash_or_size')
    b = json.loads(raw)
    validate_owner_binding(b)
    now = time.time()
    if not (b['window_start_epoch'] <= now <= b['window_end_epoch'] and 0 <= now - b['pre_end_epoch'] <= 120):
        raise Refused('window_or_PRE_stale')
    if len(b['auxiliary_units']) + len(b['timers']) > 8 or len(set(b['auxiliary_units'] + b['timers'])) != len(b['auxiliary_units'] + b['timers']):
        raise Refused('unit_inventory_count_or_duplicates')
    for name in b['auxiliary_units'] + b['timers']:
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.(service|timer)', name) or name in MAIN:
            raise Refused('unit_name')
    if not isinstance(b.get('retired_worker_history'),list) or not b.get('roster_policy_review'):
        raise Refused('current_roster_policy_or_history_unbound')
    if 'tolerated_workers' in b:raise Refused('caller_cached_tolerated_set_not_admission')
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
