"""ONE authored UNEXECUTED stop owner table; off-laptop exact grant required.
Protect exact prior listener retirement BEFORE unchanged census/balance. Credible
regression: old empty post-ControlGroup bypasses the old still-live subtree.
Restore table cannot reach this stop predicate. No production test seam.
Previously qualified admission/census are isolated declared boundaries, not
relabelled as new proof; stop/new guards/original command+permit are real.
"""
import contextlib,hashlib,io,json,os,stat,sys,tempfile,traceback,types,unittest
from pathlib import Path,PurePosixPath
from unittest.mock import patch
ROOT=Path(__file__).parent/'controller-r18'
for name in ('writer_policy','owned','effective_class','roster','controller'):
    m=types.ModuleType(name);m.__file__=str(ROOT/(name+'.py'));m.__package__='';sys.modules[name]=m
    exec(compile((ROOT/(name+'.py')).read_bytes(),m.__file__,'exec',dont_inherit=True),m.__dict__)
C=sys.modules['controller'];R=sys.modules['roster']
UNIT='clarp-bookkeeping-dg3c76d02ac7bed15c.service'
CG='/app/'+UNIT;LEAF='/sys/fs/cgroup'+CG
class CensusReached(Exception):pass

class BookkeepingStopFocus(unittest.TestCase):
    def test_exact_listener_owned_stop_proves_prior_instance_before_census(self):
        positives={'absent','ESRCH_absent','reused_empty','gone_retained_empty'}
        reasons={
            'pre_pid':'listener_prior_unit_or_birth_changed','pre_birth':'listener_prior_unit_or_birth_changed',
            'pre_uid':'listener_prior_stop_binding','pre_boot':'listener_prior_unit_or_birth_changed',
            'pre_invocation':'listener_prior_stop_binding','pre_native_cgroup':'listener_prior_unit_or_birth_changed',
            'pre_bracket':'listener_prior_native_or_group_changed','missing_stop_list':'listener_required_stop_list_missing',
            'noncanonical':'listener_old_cgroup_path_unbound','parent_symlink':'listener_cgroup_parent_type_or_symlink',
            'root_symlink':'listener_cgroup_root_symlink','pre_population_zero':'listener_prior_native_or_group_changed',
            'namespace_permission':'NS_PERMISSION_SENTINEL','stop_nonzero':'command_nonzero_1',
            'post_show_nonzero':'command_nonzero_7','post_missing_pid':'unit_not_stopped_'+UNIT,
            'post_wrong_state':'unit_not_stopped_'+UNIT,'old_birth_live':'listener_prior_pid_birth_still_live',
            'old_stat_permission':'OLD_STAT_PERMISSION_SENTINEL','old_stat_partial':'IndexError',
            'replacement_invocation':'listener_replacement_or_post_state_unknown','post_boot':'listener_stop_boot_changed',
            'post_parent_rebind':'listener_cgroup_parent_namespace_rebound','post_namespace':'listener_cgroup_parent_namespace_rebound',
            'old_leaf_rebound':'listener_prior_cgroup_rebound_or_populated','old_leaf_populated':'listener_prior_cgroup_rebound_or_populated',
            'old_leaf_symlink':'listener_old_cgroup_type_or_symlink','events_permission':'CG_PERMISSION_SENTINEL',
            'events_missing':'CG_EVENTS_MISSING_SENTINEL','events_malformed':'listener_cgroup_events_shape',
            'leaf_reappears':'listener_old_cgroup_reappeared','leaf_disappears':'listener_old_cgroup_disappeared_during_proof',
            'pre_evidence_fault':'listener_prior_stop_evidence_fault','post_evidence_fault':'listener_post_stop_evidence_fault',
            'chained_stat':'STAT_OUTER_SENTINEL','late_work':'work_budget_exhausted',
            'late_first_permit':'ISOLATED_PREVIOUS_ADMISSION_EXPIRED'}
        for case in tuple(sorted(positives))+tuple(reasons):
            with self.subTest(case=case),tempfile.TemporaryDirectory(prefix='listener-stop-owner-') as tmp:
                clock=types.SimpleNamespace(mono=100.0)
                state={'listener_stopped':False,'stopped':[],'attempts':[],'pre_shows':0,'post_shows':0,
                       'post_leaf_lstats':0,'post_pid_reads':0,'event_reads':0,'census':0,'fsync_failed':False,'raw_contexts':[]}
                cg='/app/../'+UNIT if case=='noncanonical' else CG
                def proc_stat(birth):
                    fields=['0']*20;fields[19]=birth;return ('11 (listener) '+' '.join(fields)).encode()
                class NativePath:
                    def __init__(self,value):self.value=str(PurePosixPath(str(value)))
                    def __truediv__(self,name):return NativePath(self.value+'/'+str(name))
                    def __str__(self):return self.value
                    def __fspath__(self):return self.value
                    def __eq__(self,other):return isinstance(other,NativePath) and self.value==other.value
                    @property
                    def name(self):return PurePosixPath(self.value).name
                    @property
                    def parts(self):return PurePosixPath(self.value).parts
                    def resolve(self,strict=False):
                        return NativePath('/wrong-root') if self.value=='/sys/fs/cgroup' and case=='root_symlink' else self
                    def lstat(self):
                        mode=stat.S_IFDIR|0o700;ino=900
                        if self.value=='/sys/fs/cgroup/app':
                            ino=901
                            if case=='parent_symlink':mode=stat.S_IFLNK|0o777
                            if state['listener_stopped'] and case=='post_parent_rebind':ino=999
                        elif self.value==LEAF:
                            ino=905
                            if state['listener_stopped']:
                                state['post_leaf_lstats']+=1;n=state['post_leaf_lstats']
                                if case in ('absent','ESRCH_absent') or (case=='leaf_reappears' and n==1) or (case=='leaf_disappears' and n>=4):
                                    raise FileNotFoundError('EXACT_OLD_LEAF_ABSENT')
                                if case=='old_leaf_rebound':ino=906
                                if case=='old_leaf_symlink':mode=stat.S_IFLNK|0o777
                        elif self.value not in ('/sys/fs/cgroup','/proc/11','/proc/99'):
                            raise AssertionError('unexpected lstat path:'+self.value)
                        return types.SimpleNamespace(st_dev=99,st_ino=ino,st_uid=os.getuid(),st_mode=mode)
                    def stat(self):return self.lstat()
                    def read_bytes(self):
                        if self.value not in ('/proc/11/stat','/proc/99/stat'):raise AssertionError('unexpected native bytes:'+self.value)
                        if not state['listener_stopped']:return proc_stat('100')
                        state['post_pid_reads']+=1
                        if case=='old_birth_live':return proc_stat('100')
                        if case=='old_stat_permission':raise PermissionError('OLD_STAT_PERMISSION_SENTINEL')
                        if case=='old_stat_partial':return b'11 (listener) partial'
                        if case=='chained_stat':
                            try:raise KeyError('STAT_INNER_SENTINEL')
                            except KeyError as exc:raise ValueError('STAT_OUTER_SENTINEL') from exc
                        if case=='reused_empty':return proc_stat('200')
                        if case=='ESRCH_absent':raise ProcessLookupError('EXACT_OLD_PID_ESRCH')
                        raise FileNotFoundError('EXACT_OLD_PID_ABSENT')
                    def read_text(self):
                        if self.value.endswith('/stat'):return self.read_bytes().decode()
                        if self.value in ('/proc/11/cgroup','/proc/99/cgroup'):
                            return '0::'+('/other/'+UNIT if case=='pre_native_cgroup' else cg)
                        if self.value=='/proc/sys/kernel/random/boot_id':return 'changed-boot' if state['listener_stopped'] and case=='post_boot' else 'test-boot'
                        raise AssertionError('unexpected native text:'+self.value)
                    def exists(self):raise AssertionError('unexpected weak exists path:'+self.value)
                original_open=C.os.open;original_close=C.os.close;original_read=C.os.read;original_fstat=C.os.fstat
                virtual={};nextfd=[90000]
                def open_lower(path,flags,*args,**kwargs):
                    if str(path)==LEAF+'/cgroup.events':
                        if state['listener_stopped'] and case=='events_permission':raise PermissionError('CG_PERMISSION_SENTINEL')
                        if state['listener_stopped'] and case=='events_missing':raise FileNotFoundError('CG_EVENTS_MISSING_SENTINEL')
                        nextfd[0]+=1;virtual[nextfd[0]]=True;return nextfd[0]
                    return original_open(path,flags,*args,**kwargs)
                def read_lower(fd,count):
                    if fd not in virtual:return original_read(fd,count)
                    state['event_reads']+=1
                    if state['listener_stopped'] and case=='late_work':clock.mono+=601
                    if state['listener_stopped'] and case=='events_malformed':return b'populated 0\npopulated 1\n'
                    populated='0' if state['listener_stopped'] else '1'
                    if case=='pre_population_zero' and not state['listener_stopped']:populated='0'
                    if case=='old_leaf_populated' and state['listener_stopped']:populated='1'
                    return ('populated '+populated+'\nfrozen 0\n').encode()
                def fstat_lower(fd):return types.SimpleNamespace(st_dev=99,st_ino=912) if fd in virtual else original_fstat(fd)
                def close_lower(fd):
                    if fd in virtual:virtual.pop(fd);return
                    return original_close(fd)
                def readlink_lower(path):
                    if str(path).startswith('/proc/self/ns/'):
                        if case=='namespace_permission':raise PermissionError('NS_PERMISSION_SENTINEL')
                        return 'new-namespace' if state['listener_stopped'] and case=='post_namespace' else 'namespace-'+str(path).rsplit('/',1)[-1]
                    raise AssertionError('unexpected readlink:'+str(path))
                def admission_lower(owner):
                    # Declared previously-qualified admission boundary only.
                    if not 0<=clock.mono-owner.roster_admission['end']<2:raise R.WriterUnknown('ISOLATED_PREVIOUS_ADMISSION_EXPIRED')
                class LowerOwned:
                    def __init__(self,argv,output,env,native_sources=None,work_permit=None):
                        self.argv=argv;self.output=output;self.reap_started=False;self.identity=None
                        if case=='late_first_permit' and argv[:3]==['/usr/bin/systemctl','--user','stop'] and argv[3]=='first.timer':clock.mono+=3
                        if work_permit is not None:work_permit.check() # REAL FirstStopPermit/DispatchPermit
                    def wait(self,seconds):
                        argv=self.argv
                        if argv[:3]==['/usr/bin/systemctl','--user','stop']:
                            name=argv[3];state['attempts'].append(name)
                            if name==UNIT and case=='stop_nonzero':self.reap_started=True;return 1
                            state['stopped'].append(name)
                            if name==UNIT:state['listener_stopped']=True
                            self.reap_started=True;return 0
                        if argv[:3]!=['/usr/bin/systemctl','--user','show']:raise AssertionError('unexpected native command:'+repr(argv))
                        name=argv[3];stopped=name in state['stopped'];rc=0
                        if name==UNIT and not stopped:
                            state['pre_shows']+=1
                            inv='2'*32 if case=='pre_bracket' and state['pre_shows']>=2 else '1'*32
                            fields={'ActiveState':'active','SubState':'running','MainPID':'11','InvocationID':inv,'ControlGroup':cg}
                        else:
                            if name==UNIT:state['post_shows']+=1
                            fields={'ActiveState':'inactive','SubState':'dead','MainPID':'0','InvocationID':'','ControlGroup':''}
                            if name==UNIT:
                                if case=='post_show_nonzero':rc=7
                                if case=='post_missing_pid':fields.pop('MainPID')
                                if case=='post_wrong_state':fields['ActiveState']='activating'
                                if case=='replacement_invocation':fields['InvocationID']='2'*32
                        self.output.write(('\n'.join(k+'='+v for k,v in fields.items())+'\n').encode());self.reap_started=True;return rc
                    def cleanup(self):state['raw_contexts'].append('NO_NATIVE_CHILD_NOT_DRAIN')
                old={'pid':11,'start':'100','uid':os.getuid(),'boot':'test-boot','cgroup':cg,'invocation':'1'*32}
                if case=='pre_pid':old['pid']=99
                if case=='pre_birth':old['start']='999'
                if case=='pre_uid':old['uid']=os.getuid()+1
                if case=='pre_boot':old['boot']='different-boot'
                if case=='pre_invocation':old['invocation']='3'*32
                binding={'auxiliary_units':[UNIT,'later.service'],'timers':['first.timer'],
                         'expected_active_invocations':{UNIT:'1'*32},
                         'bookkeeping_restore':{'unit':UNIT,'delegation_id':'dg3c76d02ac7bed15c','previous_native':old}}
                if case=='missing_stop_list':binding['auxiliary_units']=['later.service']
                def census_boundary(owner):
                    state['census']+=1;raise CensusReached('UNCHANGED_DOWNSTREAM_CENSUS_REACHED_NOT_PROVEN')
                rawstderr=io.StringIO();failure=None;trace=''
                with patch.object(C,'Path',NativePath),patch.object(C.time,'monotonic',lambda:clock.mono),\
                     patch.object(C.os,'open',open_lower),patch.object(C.os,'read',read_lower),patch.object(C.os,'fstat',fstat_lower),\
                     patch.object(C.os,'close',close_lower),patch.object(C.os,'readlink',readlink_lower),\
                     patch.object(C,'check_admission',admission_lower),patch.object(R,'check_admission',admission_lower),\
                     patch.object(C,'Owned',LowerOwned),patch.object(C.Controller,'writer_census',census_boundary),contextlib.redirect_stderr(rawstderr):
                    owner=C.Controller(binding,Path(tmp));owner.roster_admission={'end':100.,'deadline':102.}
                    original_fsync=C.os.fsync
                    def fsync_lower(fd):
                        event=Path(tmp)/'events.jsonl'
                        if event.exists() and not state['fsync_failed']:
                            last=json.loads(event.read_text().splitlines()[-1])
                            target='bookkeeping_before_owned_stop_bound' if case=='pre_evidence_fault' else 'bookkeeping_owned_stop_old_instance_proven_gone' if case=='post_evidence_fault' else None
                            if last['kind']==target:state['fsync_failed']=True;raise OSError('FSYNC_EVIDENCE_SENTINEL')
                        original_fsync(fd)
                    with patch.object(C.os,'fsync',fsync_lower):
                        try:owner.stop()
                        except Exception as exc:failure=exc;trace=traceback.format_exc()
                events=[json.loads(line) for line in (Path(tmp)/'events.jsonl').read_text().splitlines()] if (Path(tmp)/'events.jsonl').exists() else []
                stops=[e for e in events if e['kind']=='command' and e['argv'][:3]==['/usr/bin/systemctl','--user','stop']]
                if case in positives:
                    self.assertIsInstance(failure,CensusReached);self.assertEqual(state['census'],1)
                    self.assertEqual(state['attempts'],['first.timer',UNIT,'later.service','clarp.service','clarp-runtime.service'])
                    self.assertEqual(state['stopped'],state['attempts']);self.assertEqual(state['attempts'].count(UNIT),1)
                    proof=next(e for e in events if e['kind']=='bookkeeping_owned_stop_old_instance_proven_gone')
                    command=next(e for e in stops if e['argv'][3]==UNIT)
                    self.assertEqual(command['rc'],0);self.assertEqual(command['output'],'command-%03d.log'%proof['successful_stop_command_ordinal'])
                    self.assertTrue((Path(tmp)/command['output']).is_file());self.assertGreaterEqual(state['post_pid_reads'],2)
                    self.assertEqual(owner.work_deadline,700.);self.assertEqual(owner.offline_start,100.)
                    self.assertEqual(proof['prior']['native']['pid'],11)
                else:
                    self.assertIsNotNone(failure);self.assertNotIsInstance(failure,CensusReached);self.assertEqual(state['census'],0)
                    self.assertIn(reasons[case],trace,'intended native guard not reached')
                    self.assertLessEqual(state['attempts'].count(UNIT),1)
                    if case.startswith('pre_') or case in ('missing_stop_list','noncanonical','parent_symlink','root_symlink','namespace_permission','late_first_permit'):
                        self.assertNotIn(UNIT,state['attempts'])
                    if case in ('old_birth_live','old_stat_permission','old_stat_partial','chained_stat','ESRCH_absent'):
                        self.assertGreaterEqual(state['post_pid_reads'],1)
                    if case=='chained_stat':self.assertIn('STAT_INNER_SENTINEL',trace);self.assertIn('direct cause',trace)
                    if case in ('pre_evidence_fault','post_evidence_fault'):
                        self.assertTrue(state['fsync_failed']);self.assertTrue(any('FSYNC_EVIDENCE_SENTINEL' in e for e in owner.diagnostic_errors))
                    if case=='late_work':self.assertEqual(owner.work_deadline,700.);self.assertGreater(clock.mono,700.)
                    if case=='late_first_permit':self.assertEqual(state['attempts'],[]);self.assertEqual(owner.roster_admission['deadline'],102.)
                self.assertEqual(virtual,{})

if __name__=='__main__':unittest.main()
