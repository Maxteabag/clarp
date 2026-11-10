"""ONE SOURCE-ONLY/UNEXECUTED manager-residual owner-boundary table.
Contract: a real census labels ONLY exact manager readlink-EACCES as explicitly
accepted residual, keeps inventory FALSE and continues scanning; all other
unknowns abort. Credible regression: broad PermissionError waiver or fabricated
answer/inventoryTRUE. Old STOP/restore/supervision controls do not own this
branch. No new production seam; only lower private files/proc/Owned responders.
"""
import contextlib,errno,hashlib,json,os,shutil,sys,tempfile,traceback,types,unittest
from pathlib import Path,PurePosixPath
from unittest.mock import patch
ROOT=Path(__file__).parent/'controller-r24'
CARD='decision-c147190cebf96302dfe418d4'
ARTIFACT='artifact-6403454082e0fddbf03af602'
BOOT='ba9283af-aabf-4244-9606-ac4361a968f5'
WORKER='clarp-job-fixture.service'

class ManagerResidualFocus(unittest.TestCase):
    def test_only_owner_accepted_manager_EACCES_is_residual_not_inventory_proof(self):
        positives=('manager_residual','worker_protected_with_manager_residual','ordinary_no_residual','precommand_positive')
        reasons={
            'policy_disabled':'manager_residual_policy_disabled_or_owner_answer_absent',
            'acceptance_empty':'manager_residual_policy_disabled_or_owner_answer_absent',
            'wrong_answer_card':'manager_residual_actual_owner_acceptance_unbound',
            'admin_option':'manager_residual_actual_owner_acceptance_unbound',
            'agent_answer':'manager_residual_actual_owner_acceptance_unbound',
            'bool_resolved_at':'manager_residual_actual_owner_acceptance_unbound',
            'wrong_receipt_label':'manager_residual_actual_owner_acceptance_unbound',
            'receipt_proof_true':'manager_residual_actual_owner_acceptance_unbound',
            'receipt_inventory_true':'manager_residual_actual_owner_acceptance_unbound',
            'binding_inventory_true':'owner_PRE_or_quiescence_unbound',
            'wrong_receipt_hash':'manager_residual_document_hash',
            'wrong_MF_pin':'manager_residual_controller_MF_unbound',
            'policy_manager_drift':'manager_residual_exact_identity_unbound',
            'manager_birth':'manager_residual_positive_identity_or_lifetime_changed',
            'manager_exe':'manager_exe_evidence_state_changed_readable',
            'manager_exe_without_scope':'manager_residual_reference_shape',
            'manager_EPERM':'writer_census_permission_UNKNOWN',
            'other_PID_EACCES':'writer_census_permission_UNKNOWN',
            'manager_fd_stat_EACCES':'writer_census_permission_UNKNOWN',
            'manager_directory_EACCES':'writer_census_permission_UNKNOWN',
            'known_manager_protected_FD':'unlisted_db_fd',
            'later_other_protected_FD':'unlisted_db_fd',
            'late_manager_rebind':'manager_residual_positive_identity_or_lifetime_changed',
            'receipt_rebound_after_EACCES':'bound_file_version_hash',
            'protected_identity_drift':'canonical_db_identity_changed',
            'residual_evidence_failure':'manager_residual_evidence_fault',
            'transient_seal_FNF':'manager_residual_recheck_UNKNOWN:FileNotFoundError',
            'transient_manager_FNF':'manager_residual_recheck_UNKNOWN:FileNotFoundError',
            'precommand_disabled':'manager_residual_policy_disabled_or_owner_answer_absent',
            'precommand_empty':'manager_residual_policy_disabled_or_owner_answer_absent',
            'precommand_exe_EPERM':'manager_residual_recheck_UNKNOWN:PermissionError',
            'precommand_identity':'manager_residual_positive_identity_or_lifetime_changed',
            'manager_uid':'manager_residual_positive_identity_or_lifetime_changed',
            'manager_boot':'manager_residual_positive_identity_or_lifetime_changed',
            'manager_cgroup':'manager_residual_positive_identity_or_lifetime_changed',
            'controller_card_mismatch':'approvals_unbound',
            'protocol_bad_ref':'exact_manager_residual_binding_required',
            'protocol_missing_GO':'actual_current_approved_binding_required',
            'protocol_PRE_UNKNOWN':'actual_current_approved_binding_required',
            'protocol_stale_PRE':'actual_current_approved_binding_required',
            'work_abort':'controller_abort_latched_',
            'work_expired':'work_budget_exhausted',
            'scope_wrong_card':'manager_exe_explicit_owner_scope_unbound',
            'scope_wrong_answer_ref':'manager_exe_explicit_owner_scope_unbound',
            'scope_proof_true':'manager_exe_explicit_owner_scope_unbound',
            'scope_not_residual':'manager_exe_explicit_owner_scope_unbound',
            'scope_other_operations':'manager_exe_explicit_owner_scope_unbound',
            'exe_proof_true':'manager_exe_explicit_owner_scope_unbound',
            'exe_readable_expected_name':'manager_exe_evidence_state_changed_readable',
            'exe_evidence_transition':'manager_exe_evidence_state_changed_readable',
            'exe_FNF':'manager_residual_recheck_UNKNOWN:FileNotFoundError',
            'manager_comm':'manager_residual_positive_identity_or_lifetime_changed',
            'manager_comm_permission':'manager_residual_recheck_UNKNOWN:PermissionError',
            'manager_bus_rebound':'manager_residual_positive_identity_or_lifetime_changed',
            'manager_clock_nonzero':'manager_residual_positive_identity_or_lifetime_changed',
            'lifetime_missing':'manager_lifetime_context_unbound',
            'exe_bool_as_int_proof':'manager_exe_evidence_not_strictly_typed',
            'exe_int_as_bool_reexec':'manager_exe_evidence_not_strictly_typed'}
        for case in positives+tuple(reasons):
            with self.subTest(case=case),tempfile.TemporaryDirectory(prefix='manager-residual-owner-') as tmp:
                base=Path(tmp);private=base/'source';private.mkdir(mode=0o700)
                for file in ROOT.iterdir():
                    if file.is_file():shutil.copyfile(file,private/file.name);os.chmod(private/file.name,0o600)
                modules={}
                for name in ('writer_policy','owned','effective_class','roster','controller'):
                    m=types.ModuleType(name);m.__file__=str(private/(name+'.py'));m.__package__='';sys.modules[name]=m
                    exec(compile((private/(name+'.py')).read_bytes(),m.__file__,'exec',dont_inherit=True),m.__dict__);modules[name]=m
                C=modules['controller'];WP=modules['writer_policy'];R=modules['roster']
                P=types.ModuleType('manager_residual_protocol');P.__file__=str(ROOT.parent/'supervisor/protocol.py')
                exec(compile(Path(P.__file__).read_bytes(),P.__file__,'exec',dont_inherit=True),P.__dict__)
                clock=types.SimpleNamespace(mono=100.0)
                times=types.SimpleNamespace(time=lambda:1000.,monotonic=lambda:clock.mono)
                protected=base/'protected';protected.mkdir(); evidence=base/'evidence';evidence.mkdir(mode=0o700)
                roles={}
                for role in ('state.sqlite','state.sqlite-wal','state.sqlite-shm'):
                    f=protected/role;f.write_bytes(b'PRIVATE_SYNTHETIC_NOT_A_DATABASE');v=f.stat();roles[role]={'dev':v.st_dev,'inode':v.st_ino}
                manager={'pid':1331,'start':'1168','uid':1000,'boot':BOOT,'comm':'systemd','cgroup':'/user.slice/user-1000.slice/user@1000.service/init.scope'}
                answer={'artifact':{'artifact_id':ARTIFACT,'session':'hobbles-18b0','type':'question','status':'completed','decision':{'artifact_id':ARTIFACT,'decision_id':CARD,'status':'answered','resolved_choice':'answered','resolved_by':'user','resolved_at':1234,'answer':{'option_id':'tonight','label':'SYNTHETIC OWNER ACCEPTS MANAGER ONLY'},'options':[{'id':'tonight','label':'SYNTHETIC OWNER ACCEPTS MANAGER ONLY'}]}}}
                if case=='wrong_answer_card':answer['artifact']['decision']['decision_id']='other-card'
                if case=='admin_option':answer['artifact']['decision']['answer']['option_id']='tuesday-admin-rule'
                if case=='agent_answer':answer['artifact']['decision']['resolved_by']='agent:fixture'
                if case=='bool_resolved_at':answer['artifact']['decision']['resolved_at']=True
                def save(name,value):
                    f=base/name;f.write_text(json.dumps(value));os.chmod(f,0o600);return {'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
                answerref=save('answer.json',answer)
                zero=[['monotonic','0','0'],['boottime','0','0']]
                lifetime={'bus_socket_version':[99,801,0,100,100,1000,0o140600],'zero_clock_offsets':{'owner':zero,'manager':zero}}
                exe_evidence={'state':'UNAVAILABLE_EACCES','errno':13,'proof_of_executable_identity':False,'same_pid_reexec_not_excluded':True}
                scope={'schema':'clarp.manager-exe-residual-scope/1','issuer':'hobbles-18b0','decision_card_id':CARD,'state':'OWNER_ACCEPTED_RESIDUAL','actual_Peter_answer_ref':answerref,'manager':manager.copy(),'allowed_operations':['exe_readlink_EACCES','fd_readlink_EACCES'],'proof_of_executable_identity':False,'same_pid_reexec_not_excluded':True}
                if case=='scope_wrong_card':scope['decision_card_id']='OTHER'
                if case=='scope_wrong_answer_ref':scope['actual_Peter_answer_ref']={'path':'/other','sha256':'0'*64}
                if case=='scope_proof_true':scope['proof_of_executable_identity']=True
                if case=='scope_not_residual':scope['state']='PROVEN'
                if case=='scope_other_operations':scope['allowed_operations'].append('ANY_PERMISSION_ERROR')
                scope_ref=save('scope.json',scope)
                if case=='manager_exe_without_scope':scope_ref=None
                receipt={'schema':'clarp.owner-accepted-manager-fd-residual/1','owner':'Peter','decision_card_id':CARD,'artifact_id':ARTIFACT,'option_id':'tonight','resolved_at':1234,'actual_answer':'SYNTHETIC OWNER ACCEPTS MANAGER ONLY','state':'OWNER_ACCEPTED_RESIDUAL','accepted_scope':'BOUND_USER_MANAGER_READLINK_EACCES_ONLY','proof_of_nonwriter':False,'writer_inventory_reconciled':False,'manager':manager.copy(),'controller_MF_sha256':hashlib.sha256((private/'SHA256SUMS').read_bytes()).hexdigest(),'decision_answer':answerref}
                receipt.update(manager_exe_scope=scope_ref,manager_exe_evidence=exe_evidence.copy(),manager_lifetime_context=lifetime.copy())
                if case=='exe_bool_as_int_proof':receipt['manager_exe_evidence']['proof_of_executable_identity']=0
                if case=='exe_int_as_bool_reexec':receipt['manager_exe_evidence']['same_pid_reexec_not_excluded']=1
                if case=='exe_proof_true':receipt['manager_exe_evidence']['proof_of_executable_identity']=True
                if case=='lifetime_missing':receipt['manager_lifetime_context']=None
                if case=='wrong_receipt_label':receipt['actual_answer']='OTHER_LABEL'
                if case=='receipt_proof_true':receipt['proof_of_nonwriter']=True
                if case=='receipt_inventory_true':receipt['writer_inventory_reconciled']=True
                if case=='wrong_MF_pin':receipt['controller_MF_sha256']='0'*64
                receiptref=save('receipt.json',receipt)
                if case=='wrong_receipt_hash':receiptref['sha256']='0'*64
                policy={'schema':'clarp.manager-fd-residual-policy/1','enabled':case not in ('policy_disabled','precommand_disabled'),'owner_acceptance':None if case in ('acceptance_empty','precommand_empty') else receiptref,'manager':manager.copy()}
                policy.update(manager_exe_evidence=exe_evidence.copy(),manager_lifetime_context=lifetime.copy())
                if case=='policy_manager_drift':policy['manager']['start']='other'
                policyref=save('policy.json',policy)
                worker={'unit':WORKER,'pid':21,'start':'200','invocation':'1'*32,'cgroup':'/app/clarp-job-fixture.service','uid':1000,'boot':BOOT,'job_id':'fixture','owner':'fixture-owner','phase':'ACTIVE'}
                binding={'tolerated_workers':[worker],'auxiliary_units':[],'timers':[],'protected_db_files':roles,'peter_decision':CARD,'peter_yes':True,'writer_inventory_reconciled':case=='binding_inventory_true','writer_inventory_accounting':'ACCOUNTED_EXCEPT_OWNER_ACCEPTED_MANAGER_RESIDUAL','manager_fd_residual_policy':policyref,'manager_residual_identity':manager.copy()}
                binding.update(manager_exe_evidence=exe_evidence.copy(),manager_lifetime_context=lifetime.copy())
                binding.update(schema='clarp.disk-maintenance-binding/1',lead_go_receipt='SYNTHETIC_FIXTURE_NOT_GO',pre_state='ZERO',
                    window_start_epoch=900.,window_end_epoch=2000.,pre_end_epoch=990.,quiescence_receipt='SYNTHETIC_NOTICE_NOT_ACTUAL',
                    balance_timing={'cancel_by':240,'skip25_after':150,'terminal_target':300,'overlap_at':420,'overlap_at420':True},lead_overlap_disposition_receipt='SYNTHETIC_ONLY')
                if case=='ordinary_no_residual':
                    binding.pop('manager_fd_residual_policy');binding['writer_inventory_reconciled']=True
                    binding['peter_decision']='decision-0795e2c8e5ebd591f6c242a8'
                if case=='controller_card_mismatch':binding['peter_decision']='decision-0795e2c8e5ebd591f6c242a8'
                if case=='protocol_bad_ref':binding['manager_fd_residual_policy']={}
                if case=='protocol_missing_GO':binding['lead_go_receipt']=''
                if case=='protocol_PRE_UNKNOWN':binding['pre_state']='UNKNOWN'
                if case=='protocol_stale_PRE':binding['pre_end_epoch']=700.
                release=base/'release';release.mkdir();(protected/'current').symlink_to(release,target_is_directory=True)
                marker=release/'RUNTIME_RELEASE_ID';marker.write_text('SYNTHETIC_RUNTIME')
                binding['current_real']=str(release);binding['release_files']={'RUNTIME_RELEASE_ID':hashlib.sha256(marker.read_bytes()).hexdigest()}
                if case=='protected_identity_drift':binding['protected_db_files']['state.sqlite']['inode']+=1
                state={'finished_scan':False,'links':[],'fdstats':[],'commands':[],'manager_errors':0,'evidence_fault':False,'fnf_count':0}
                def proc_stat(pid):
                    fields=['0']*20;fields[19]='1168' if pid==1331 else '200'
                    if pid==1331 and (case in ('manager_birth','precommand_identity') or (case=='late_manager_rebind' and state['finished_scan'])):fields[19]='999'
                    return str(pid)+' (fixture) '+' '.join(fields)
                class NativePath:
                    def __init__(self,value):self.value=str(PurePosixPath(str(value)))
                    def __truediv__(self,name):return NativePath(self.value+'/'+str(name))
                    def __str__(self):return self.value
                    def __fspath__(self):return self.value
                    @property
                    def name(self):return PurePosixPath(self.value).name
                    def is_absolute(self):return self.value.startswith('/')
                    def iterdir(self):
                        if self.value=='/proc':
                            def processes():
                                for pid in (1331,21,2222):yield NativePath('/proc/'+str(pid))
                                state['finished_scan']=True
                            return processes()
                        if self.value.endswith('/fd'):
                            if case=='manager_directory_EACCES' and self.value=='/proc/1331/fd':raise PermissionError(errno.EACCES,'MANAGER_DIRECTORY_PERMISSION_SENTINEL',self.value)
                            return iter([NativePath(self.value+'/'+n) for n in ('0','1','2')])
                        raise AssertionError('UNEXPECTED_NATIVE_ITER:'+self.value)
                    def lstat(self):
                        if self.value=='/run/user/1000/bus':
                            return types.SimpleNamespace(st_dev=99,st_ino=999 if case=='manager_bus_rebound' else 801,st_size=0,st_mtime_ns=100,st_ctime_ns=100,st_uid=1000,st_mode=0o140600)
                        raise AssertionError('UNEXPECTED_NATIVE_LSTAT:'+self.value)
                    def stat(self):
                        if '/fd/' in self.value:
                            state['fdstats'].append(self.value)
                            if case=='manager_fd_stat_EACCES' and self.value=='/proc/1331/fd/1':raise PermissionError(errno.EACCES,'MANAGER_FDSTAT_PERMISSION_SENTINEL',self.value)
                            real=(case=='known_manager_protected_FD' and self.value=='/proc/1331/fd/2') or (case=='later_other_protected_FD' and self.value=='/proc/2222/fd/2') or (case=='worker_protected_with_manager_residual' and self.value=='/proc/21/fd/2')
                            a=(protected/'state.sqlite').stat()
                            return types.SimpleNamespace(st_dev=a.st_dev,st_ino=a.st_ino if real else -1,st_uid=1000)
                        return types.SimpleNamespace(st_uid=999 if case=='manager_uid' and self.value=='/proc/1331' else 1000)
                    def read_text(self):
                        if self.value.endswith('/timens_offsets'):
                            return 'monotonic 1 0\nboottime 0 0\n' if case=='manager_clock_nonzero' else 'monotonic 0 0\nboottime 0 0\n'
                        if self.value=='/proc/1331/comm':
                            if case=='manager_comm_permission':raise PermissionError(errno.EACCES,'MANAGER_COMM_PERMISSION_SENTINEL',self.value)
                            return 'OTHER_MANAGER_NAME' if case=='manager_comm' else 'systemd'
                        if self.value=='/proc/sys/kernel/random/boot_id':return 'OTHER_BOOT' if case=='manager_boot' else BOOT
                        pid=int(self.value.split('/')[2])
                        if self.name=='stat':
                            if pid==1331 and case=='transient_manager_FNF' and state['manager_errors'] and state['fnf_count']==0:
                                state['fnf_count']+=1;raise FileNotFoundError('TRANSIENT_REQUIRED_MANAGER_READ_FNF')
                            return proc_stat(pid)
                        if self.name=='cgroup':
                            return '0::'+(('/changed/manager' if case=='manager_cgroup' else manager['cgroup']) if pid==1331 else worker['cgroup'] if pid==21 else '/foreign/unlisted.service')
                        raise AssertionError('UNEXPECTED_NATIVE_READ:'+self.value)
                def path(value):return NativePath(value) if str(value).startswith('/proc') or str(value)=='/run/user/1000/bus' else Path(value)
                original_readlink=os.readlink
                def readlink(value,*args,**kwargs):
                    value=str(value)
                    if not value.startswith('/proc'):return original_readlink(value,*args,**kwargs)
                    state['links'].append(value)
                    if value=='/proc/1331/exe':
                        if case in ('manager_exe','exe_readable_expected_name') or (case=='exe_evidence_transition' and state['manager_errors']):
                            return '/different/exe' if case=='manager_exe' else '/usr/lib/systemd/systemd'
                        if case=='exe_FNF':raise FileNotFoundError('EXACT_MANAGER_EXE_FNF_NOT_ACCEPTED')
                        raise PermissionError(errno.EPERM if case=='precommand_exe_EPERM' else errno.EACCES,'MANAGER_EXE_PERMISSION_SENTINEL',value)
                    if value=='/proc/1331/fd/0' and case!='ordinary_no_residual':
                        state['manager_errors']+=1
                        if case=='receipt_rebound_after_EACCES':(base/'receipt.json').write_text(json.dumps(receipt)+' ')
                        raise PermissionError(errno.EPERM if case=='manager_EPERM' else errno.EACCES,'MANAGER_LINK_PERMISSION_SENTINEL',value)
                    if value=='/proc/2222/fd/0' and case=='other_PID_EACCES':raise PermissionError(errno.EACCES,'OTHER_PID_PERMISSION_SENTINEL',value)
                    return '/dev/null'
                class FileOSProxy:
                    def __getattr__(self,key):return getattr(os,key)
                    def open(self,path,flags,*a,**kw):
                        if str(path)==str(base/'receipt.json') and case=='transient_seal_FNF' and state['manager_errors'] and state['fnf_count']==0:
                            state['fnf_count']+=1;raise FileNotFoundError('TRANSIENT_REQUIRED_RECEIPT_READ_FNF')
                        return os.open(path,flags,*a,**kw)
                original_fsync=os.fsync
                def fsync(fd):
                    e=evidence/'events.jsonl'
                    if case=='residual_evidence_failure' and e.exists() and 'manager_fd_OWNER_ACCEPTED_RESIDUAL' in e.read_text():state['evidence_fault']=True;raise OSError('RESIDUAL_FSYNC_SENTINEL')
                    return original_fsync(fd)
                # Native owner/proc UID is synthetic1000; actual private fixture reader
                # still checks the real hosted user's file ownership/mode/hash.
                class OSProxy:
                    def __getattr__(self,key):return getattr(os,key)
                    def getuid(self):return 1000
                    def getpid(self):return 9999
                    def readlink(self,*a,**kw):return readlink(*a,**kw)
                    def fsync(self,fd):return fsync(fd)
                class LowerOwned:
                    def __init__(self,argv,output,env,native_sources=None,work_permit=None):self.argv=argv;self.output=output;state['commands'].append(argv)
                    def wait(self,seconds):
                        if case=='precommand_positive' and self.argv==['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','status','/']:raise RuntimeError('POSITIVE_PREFLIGHT_REACHED_FIRST_READONLY_COMMAND')
                        if self.argv[:3]==['/usr/bin/systemctl','--user','show'] and self.argv[3]==WORKER:
                            self.output.write(('ActiveState=active\nMainPID=21\nInvocationID='+worker['invocation']+'\nControlGroup='+worker['cgroup']+'\n').encode());return 0
                        if self.argv[:3]==['/usr/bin/systemctl','--user','list-units']:return 0
                        raise AssertionError('UNEXPECTED_NATIVE_COMMAND:'+repr(self.argv))
                    def cleanup(self):raise AssertionError('NO_NATIVE_CHILD_OR_CLEANUP_NEEDED')
                failure=None;trace='';run_rc=None
                with patch.object(C,'Path',path),patch.object(C,'SHARE',protected),patch.object(C,'os',OSProxy()),patch.object(WP,'os',OSProxy()),patch.object(R,'os',FileOSProxy()),patch.object(C,'Owned',LowerOwned),patch.object(C,'time',times),patch.object(P,'time',times),patch.object(C.signal,'signal',lambda *args:None):
                    owner=C.Controller(binding,evidence)
                    if case in ('work_abort','work_expired'):
                        owner.phase='work';owner.work_deadline=99. if case=='work_expired' else 700.;owner.abort_requested=case=='work_abort'
                    try:
                        if case.startswith('protocol_'):
                            P.binding_check(json.dumps(binding).encode())
                        elif case.startswith('precommand_'):
                            run_rc=owner.run()
                            lines=(evidence/'events.jsonl').read_text().splitlines()
                            abort=next(json.loads(line) for line in lines if json.loads(line)['kind']=='primary_abort')
                            trace=abort['traceback'];failure=RuntimeError(abort['reason'])
                        else:
                            C.validate_owner_binding(binding)
                            P.binding_check(json.dumps(binding).encode())
                            owner.writer_census()
                    except Exception as exc:failure=exc;trace=traceback.format_exc()
                events=[json.loads(x) for x in (evidence/'events.jsonl').read_text().splitlines()] if (evidence/'events.jsonl').exists() else []
                residual_events=[e for e in events if e['kind']=='manager_fd_OWNER_ACCEPTED_RESIDUAL']
                final=[e for e in events if e['kind']=='exact_tolerated_fd_census']
                exe_events=[e for e in events if e['kind']=='manager_executable_OWNER_ACCEPTED_RESIDUAL']
                if case=='precommand_positive':
                    self.assertEqual(run_rc,11);self.assertIn('POSITIVE_PREFLIGHT_REACHED_FIRST_READONLY_COMMAND',trace)
                    self.assertEqual(state['commands'],[['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','status','/']])
                    self.assertFalse(owner.stopped);self.assertEqual(final,[])
                    continue
                if case in positives:
                    self.assertIsNone(failure,trace);self.assertEqual(len(final),1)
                    self.assertIn('/proc/2222/fd/2',state['links']);self.assertIn('/proc/1331/fd/1',state['fdstats'])
                    if case!='ordinary_no_residual':
                        self.assertGreaterEqual(len(exe_events),2)
                        self.assertTrue(all(e['executable_identity']['state']=='UNAVAILABLE_EACCES' and e['executable_identity']['proof_of_executable_identity'] is False and e['executable_identity']['same_pid_reexec_not_excluded'] is True for e in exe_events))
                        self.assertTrue(all('exe' not in e['positive_identity'] and e['positive_identity']['comm']=='systemd' for e in exe_events))
                        self.assertTrue(all(e['lead_scope']==scope_ref and e['actual_error']['errno']==errno.EACCES for e in exe_events))
                        self.assertEqual(len(residual_events),1);self.assertEqual(residual_events[0]['fd'],0)
                        self.assertEqual(residual_events[0]['errno'],errno.EACCES);self.assertEqual(residual_events[0]['owner_acceptance'],receiptref)
                        self.assertEqual(final[0]['state'],'ACCOUNTED_EXCEPT_OWNER_ACCEPTED_MANAGER_RESIDUAL')
                        self.assertIs(final[0]['writer_inventory_reconciled'],False);self.assertIs(final[0]['no_unlisted_writer_observed'],False)
                        self.assertIs(binding['writer_inventory_reconciled'],False);self.assertEqual(state['manager_errors'],1)
                        if case=='worker_protected_with_manager_residual':
                            self.assertEqual(len(final[0]['accepted']),1)
                            self.assertEqual(final[0]['accepted'][0]['unit'],WORKER)
                            self.assertEqual(final[0]['accepted'][0]['pid'],21)
                            self.assertEqual(final[0]['accepted'][0]['fd'],2)
                    else:self.assertEqual(residual_events,[]);self.assertIs(final[0]['no_unlisted_writer_observed'],True)
                else:
                    self.assertIsNotNone(failure);self.assertIn(reasons[case],trace,'intended guard not reached');self.assertEqual(final,[])
                    if case in ('known_manager_protected_FD','later_other_protected_FD'):self.assertTrue(any(x.endswith('/fd/2') for x in state['fdstats']))
                    if case=='other_PID_EACCES':self.assertIn('/proc/2222/fd/0',state['links']);self.assertEqual(len(residual_events),1)
                    if case=='manager_fd_stat_EACCES':self.assertIn('/proc/1331/fd/1',state['fdstats'])
                    if case=='late_manager_rebind':self.assertTrue(state['finished_scan'])
                    if case in ('transient_seal_FNF','transient_manager_FNF'):
                        self.assertEqual(state['fnf_count'],1);self.assertEqual(state['manager_errors'],1)
                        self.assertIn('TRANSIENT_REQUIRED_',trace);self.assertIn('direct cause',trace)
                    if case.startswith('precommand_'):
                        self.assertEqual(state['commands'],[]);self.assertEqual(run_rc,11)
                    if case=='residual_evidence_failure':self.assertTrue(state['evidence_fault']);self.assertTrue(any('RESIDUAL_FSYNC_SENTINEL' in e for e in owner.diagnostic_errors))
                self.assertFalse(any('stop' in x or 'start' in x or 'balance' in x for argv in state['commands'] for x in argv))
                self.assertFalse(owner.stopped)
                if not case.startswith('precommand_'):self.assertEqual(owner.operational_errors,[])

if __name__=='__main__':unittest.main()
