"""ONE synthetic owner-boundary table. Authored UNEXECUTED; off-laptop grant required.
Contract: restored exact listener role+fresh HB within original20, or non-green;
never manually start listener, never leak its deadline to later restorations.
Regression: collected-transient manual start; swallowed chain; samePIDexec;
existing15+5+ONE do not own this new recovery path. No production test seam.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import sys

ROOT=Path(__file__).parent/'controller-r17'
NAMES=('writer_policy','owned','effective_class','roster','controller')
for name in NAMES:
    module=types.ModuleType(name);module.__file__=str(ROOT/(name+'.py'));module.__package__='';sys.modules[name]=module
    exec(compile((ROOT/(name+'.py')).read_bytes(),module.__file__,'exec',dont_inherit=True),module.__dict__)
C=sys.modules['controller']
UNIT='clarp-bookkeeping-dg3c76d02ac7bed15c.service'
DELEGATION='dg3c76d02ac7bed15c'

class BookkeepingRestoreFocus(unittest.TestCase):
    def test_host_recreated_listener_restore_contract(self):
        cases=('pending_positive','unknown_unit','failed_unit','wrong_delegation','wrong_job','wrong_generation',
               'duplicate_row','future_heartbeat','same_pid_argv','same_pid_exe','same_pid_env','argv0_drift',
               'late_command_setup','late_command_wait','late_diagnostic','late_final_join','setup_missing',
               'chained_SQL_failure','chained_proc_failure','overlap420')
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory(prefix='bookkeeping-owner-') as tmp:
                clock=types.SimpleNamespace(mono=500. if case=='overlap420' else 100.)
                def wall():return 1000.+clock.mono-100.
                def sleep(seconds):clock.mono+=seconds
                counts={'show':0,'row':0,'argv':0,'env':0,'exe':0,'argv0':0,'db':0};calls=[]
                class NativePath:
                    def __init__(self,value):self.value=str(value)
                    def __truediv__(self,name):return NativePath(self.value+'/'+str(name))
                    def __str__(self):return self.value
                    @property
                    def name(self):return self.value.rsplit('/',1)[-1]
                    def resolve(self,strict=False):
                        if self.value=='/share/current':return NativePath('/release')
                        if self.value.endswith('/exe'):
                            counts['exe']+=1
                            return NativePath('/wrong' if case=='same_pid_exe' and counts['exe']>1 else '/python')
                        if self.value=='/python-link':
                            counts['argv0']+=1
                            return NativePath('/wrong' if case=='argv0_drift' and counts['argv0']>1 else '/python')
                        return self
                    def stat(self):
                        if self.value=='/share/state.sqlite':counts['db']+=1
                        return types.SimpleNamespace(st_dev=58,st_ino=557601,st_size=6,st_mtime_ns=10,st_ctime_ns=20,st_uid=os.getuid())
                    def read_text(self):
                        if self.value.endswith('/stat'):
                            fields=['0']*20;fields[19]='200';return '22 (python) '+' '.join(fields)
                        if self.value.endswith('/cgroup'):return '0::/app/'+UNIT
                        if self.value.endswith('/boot_id'):return 'test-boot'
                        raise AssertionError('unexpected native text path:'+self.value)
                    def read_bytes(self):
                        if self.value=='/release/lib/bookkeeping_listener.py':return b'source'
                        if self.value.endswith('/cmdline'):
                            counts['argv']+=1
                            if case=='chained_proc_failure':
                                try:raise KeyError('PROC_INNER_SENTINEL')
                                except KeyError as exc:raise ValueError('PROC_OUTER_SENTINEL') from exc
                            role=b'other.role' if case=='same_pid_argv' and counts['argv']>1 else b'lib.bookkeeping_listener'
                            return b'/python-link\0-m\0'+role+b'\0'+DELEGATION.encode()+b'\0'
                        if self.value.endswith('/environ'):
                            counts['env']+=1
                            extra=b'CHANGED=1\0' if case=='same_pid_env' and counts['env']>1 else b''
                            return b'PYTHONPATH=/share/current\0CLAUDE_PWA_DB=/share/state.sqlite\0'+extra
                        raise AssertionError('unexpected native bytes path:'+self.value)
                def unit_state(load='loaded',active='active',sub='running'):
                    return {'LoadState':load,'ActiveState':active,'SubState':sub,'MainPID':'22',
                            'InvocationID':'2'*32,'ControlGroup':'/app/'+UNIT}
                def rows():
                    counts['row']+=1
                    if case=='chained_SQL_failure':
                        try:raise KeyError('SQL_INNER_SENTINEL')
                        except KeyError as exc:raise ValueError('SQL_OUTER_SENTINEL') from exc
                    row={'delegation_id':DELEGATION,'delegation_status':'active','last_heartbeat_at':int(wall()*1000),
                         'job_handle':'bg1:2:listener-job','job_id':'listener-job','generation':2,'kind':'listener','job_status':'running',
                         'worker_pid':22,'worker_start_token':'test-boot:200','job_delegation':DELEGATION}
                    if case=='pending_positive' and counts['row']==1:row['job_id']=None
                    if case=='pending_positive' and counts['row']==2:row['last_heartbeat_at']=999000
                    if case=='wrong_delegation':row['job_delegation']='wrong'
                    if case=='wrong_job':row['kind']='other'
                    if case=='wrong_generation':row['generation']=0
                    if case=='future_heartbeat':row['last_heartbeat_at']=int(wall()*1000)+1000
                    return [row,row] if case=='duplicate_row' else [row]
                original_command=C.Controller.command
                lower={'construct':0,'permit_deadlines':[],'permit_grants':0,'wait_seconds':[],
                       'cleanup_model':[],'real_command_calls':0}
                class LowerOwned:
                    # Lower process boundary: NO native child exists. The real
                    # DispatchPermit.check and original Controller.command run.
                    def __init__(self,argv,output,env,native_sources=None,work_permit=None):
                        lower['construct']+=1
                        if work_permit is None:raise AssertionError('missing actual dispatch permit')
                        lower['permit_deadlines'].append(work_permit.deadline)
                        if case=='late_command_setup':clock.mono+=21
                        work_permit.check()  # actual immutable permit, no fake YES
                        lower['permit_grants']+=1
                    def wait(self,seconds):
                        lower['wait_seconds'].append(seconds);clock.mono+=21
                        raise TimeoutError('LOWER_WAIT_EXPIRED_NO_NATIVE_CHILD')
                    def cleanup(self):lower['cleanup_model'].append('NO_NATIVE_CHILD_CREATED_NOT_DRAIN_PROOF')
                def command(owner,argv,seconds=10):
                    calls.append(list(argv))
                    if argv[:4]==['/usr/bin/systemctl','--user','start','clarp-runtime.service']:return 0,''
                    if argv[:3]==['/usr/bin/systemctl','--user','start']:return 0,''
                    if argv[:3]==['/usr/bin/systemctl','--user','show']:
                        name=argv[3]
                        if name==UNIT:
                            counts['show']+=1
                            if case in ('late_command_setup','late_command_wait'):
                                lower['real_command_calls']+=1
                                return original_command(owner,argv,seconds)
                            if case=='unknown_unit':return 77,'LoadState=error\n'
                            state=unit_state()
                            if case=='failed_unit':state=unit_state(active='failed',sub='failed')
                            if case=='pending_positive' and counts['show']==1:state=unit_state(load='not-found',active='inactive',sub='dead')
                            if case=='pending_positive' and counts['show']==2:state=unit_state(active='activating',sub='start')
                            if case=='late_final_join' and counts['show']>1:clock.mono+=21
                            return (4 if state['LoadState']=='not-found' else 0),'\n'.join(k+'='+v for k,v in state.items())+'\n'
                        return 0,'ActiveState=active\nSubState=running\nMainPID=10\nInvocationID='+'3'*32+'\nControlGroup=/app/'+name+'\n'
                    if argv[0]=='/usr/bin/sqlite3':
                        self.assertIn('-readonly',argv);self.assertIn('PRAGMA query_only=ON;BEGIN;',argv[-1]);return 0,json.dumps(rows())
                    if argv[:5]==['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','status']:return 0,'No balance found on /\n'
                    raise AssertionError('unexpected command:'+repr(argv))
                binding={'current_real':'/release','release_files':{},'auxiliary_units':[UNIT,'later.service'],
                         'timers':['later.timer'],'expected_active_invocations':{UNIT:'1'*32},
                         'protected_db_files':{'state.sqlite':{'dev':58,'inode':557601}},
                         'balance_timing':{'overlap_at420':True},
                         'bookkeeping_restore':{'unit':UNIT,'delegation_id':DELEGATION,'recreation_authority':'HOST_ENSURE_RUNNING_ONLY',
                          'previous_native':{'invocation':'1'*32,'pid':11,'start':'100','boot':'test-boot','cgroup':'/app/'+UNIT},
                          'listener_source_sha256':hashlib.sha256(b'source').hexdigest(),'argv0':'/python-link','native_executable':'/python'}}
                if case=='setup_missing':del binding['bookkeeping_restore']
                rawstderr=io.StringIO()
                with patch.object(C,'Path',NativePath),patch.object(C,'SHARE',NativePath('/share')),\
                     patch.object(C.time,'monotonic',lambda:clock.mono),patch.object(C.time,'time',wall),\
                     patch.object(C.time,'sleep',sleep),patch.object(C.Controller,'command',command),\
                     patch.object(C,'Owned',LowerOwned),contextlib.redirect_stderr(rawstderr):
                    owner=C.Controller(binding,Path(tmp));owner.stopped=True;owner.offline_start=90;owner.command_deadline=777
                    if case=='overlap420':owner.overlap_recovery=True;owner.balance_origin=0;owner.balance_terminal=False
                    # Diagnostic writes stay real. The lower fsync delay models a
                    # late durable positive record; real method must then refuse.
                    original_fsync=C.os.fsync
                    def fsync(fd):
                        original_fsync(fd)
                        if case=='late_diagnostic':clock.mono+=21
                    with patch.object(C.os,'fsync',fsync):
                        failure=None
                        try:owner.restore()
                        except C.Refused as exc:failure=str(exc)
                starts=[x[3] for x in calls if x[:3]==['/usr/bin/systemctl','--user','start']]
                expected=['clarp-runtime.service','clarp.service']+([] if case=='overlap420' else ['later.service','later.timer'])
                self.assertEqual(starts,expected);self.assertNotIn(UNIT,starts)
                self.assertEqual(owner.command_deadline,777,'listener deadline leaked')
                events=[json.loads(line) for line in (Path(tmp)/'events.jsonl').read_text().splitlines()]
                positives=[e for e in events if e['kind']=='bookkeeping_Host_recreation_positive']
                if case=='pending_positive':
                    self.assertIsNone(failure);self.assertEqual(len(positives),1);self.assertGreaterEqual(counts['row'],3)
                    self.assertLess(positives[0]['deadline']-positives[0]['origin'],20.001)
                elif case=='overlap420':
                    self.assertIsNone(failure);self.assertEqual(counts['show'],0);self.assertEqual(counts['row'],0);self.assertEqual(positives,[])
                else:
                    self.assertEqual(failure,'restore_unit_failure');self.assertTrue(owner.operational_errors)
                    if case!='late_diagnostic':self.assertEqual(positives,[])
                    traces=[e['traceback'] for e in events if e['kind']=='bookkeeping_restore_chained_failure']
                    self.assertEqual(len(traces),1);self.assertIn('Traceback',traces[0]);self.assertIn('Traceback',rawstderr.getvalue())
                    if case in ('chained_SQL_failure','chained_proc_failure'):
                        self.assertIn('INNER_SENTINEL',traces[0]);self.assertIn('OUTER_SENTINEL',traces[0]);self.assertIn('direct cause',traces[0])
                    reasons={
                        'unknown_unit':'bookkeeping_unit_observation_UNKNOWN',
                        'failed_unit':'bookkeeping_Host_recreation_failed',
                        'wrong_delegation':'bookkeeping_registry_role_mismatch',
                        'wrong_job':'bookkeeping_registry_role_mismatch',
                        'wrong_generation':'bookkeeping_registry_role_mismatch',
                        'duplicate_row':'bookkeeping_delegation_unknown_or_inactive',
                        'future_heartbeat':'bookkeeping_heartbeat_unknown_or_future',
                        'same_pid_argv':'bookkeeping_recreated_role_or_identity_rebound',
                        'same_pid_exe':'bookkeeping_recreated_role_or_identity_rebound',
                        'same_pid_env':'bookkeeping_recreated_role_or_identity_rebound',
                        'argv0_drift':'bookkeeping_recreated_role_or_identity_rebound',
                        'late_command_setup':'work_permit_expired_before_exec',
                        'late_command_wait':'LOWER_WAIT_EXPIRED_NO_NATIVE_CHILD',
                        'late_diagnostic':'bookkeeping_recreation_deadline',
                        'late_final_join':'bookkeeping_recreation_deadline',
                        'setup_missing':"KeyError: 'bookkeeping_restore'",
                        'chained_SQL_failure':'SQL_OUTER_SENTINEL',
                        'chained_proc_failure':'PROC_OUTER_SENTINEL'}
                    self.assertIn(reasons[case],traces[0])
                    if case in ('same_pid_argv','same_pid_exe','same_pid_env','argv0_drift'):
                        self.assertGreaterEqual(counts['row'],1);self.assertGreaterEqual(counts['argv'],2)
                    if case in ('wrong_delegation','wrong_job','wrong_generation','duplicate_row','future_heartbeat'):
                        self.assertEqual(counts['row'],1)
                    if case in ('late_command_setup','late_command_wait'):
                        self.assertEqual(lower['real_command_calls'],1);self.assertEqual(lower['construct'],1)
                        self.assertEqual(lower['permit_deadlines'],[120.0]);self.assertGreaterEqual(clock.mono,121.)
                        if case=='late_command_setup':
                            self.assertEqual(lower['permit_grants'],0);self.assertEqual(lower['wait_seconds'],[])
                            self.assertEqual(lower['cleanup_model'],[])
                        else:
                            self.assertEqual(lower['permit_grants'],1);self.assertEqual(lower['wait_seconds'],[20.0])
                            self.assertEqual(lower['cleanup_model'],['NO_NATIVE_CHILD_CREATED_NOT_DRAIN_PROOF'])

if __name__=='__main__':unittest.main()
