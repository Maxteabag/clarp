"""ONE off-host synthetic table. No native child, app, database or service.
Contract owner: release wrapper main/runtime_wait; credible regressions are the
retained nine-character rejection, transient-observation abort and late PASS.
Existing service/gate tests cannot exercise this private orchestration boundary.
No production seams: only filesystem roots, native interfaces and clocks respond.
All rows remain UNEXECUTED until exact B route YES and captain's ONE granted run.
"""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).with_name('install_exact_and_measure-r10-SOURCE-ONLY.py')
SHA='0123456789abcdef0123456789abcdef01234567'
RUNTIME='0123456789abcdef01234567'

class Clock:
    def __init__(self):self.now=1000.0
    def monotonic(self):return self.now
    def time(self):return 2000000000+self.now
    def sleep(self,seconds):self.now+=seconds

class Fixture:
    def __init__(self,root,prefix=9,mode='happy'):
        spec=importlib.util.spec_from_file_location('synthetic_release_owner',SOURCE)
        self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
        self.root=root;self.share=root/'share';self.releases=self.share/'releases';self.releases.mkdir(parents=True)
        self.clock=Clock();self.mode=mode;self.calls=[];self.native_queries=[];self.sample_queue=[];self.restart=False;self.git_result=SHA;self.git_rc=0
        self.version=SHA[:prefix];self.release=self.version+'-synthetic-release';self.target=self.releases/self.release
        self.previous=self.releases/'fedcba987-previous';self.make_release(self.previous,'fedcba987','f'*24)
        self.current=self.share/'current';self.current.symlink_to(self.previous)
        self.make_release(self.target,self.version,RUNTIME)
        self.config=root/'config.toml';self.config.write_text('\n'*55+'claude_effort = ""\n')
        self.check=root/'release-check';self.check.write_text('SYNTHETIC_NOT_EXECUTABLE')
        self.data=root/'evidence';self.data.mkdir();self.out=self.data/'out'
        self.authority=root/'synthetic-authority.json';self.authority.write_text('{"synthetic":true}')
        self.plan={'sha':SHA,'output':str(self.out),'install_gate':'LEAD_0859_EXACT_MAIN_CRITICAL_DOCKER_GREEN','integration_job_success':True,'install_authority':self.ref(self.authority),'exact_critical_gate_GREEN':True,'exact_Docker_integration_GREEN':True,'foreign_focused_required':False,'D_review':self.ref(self.authority),'actual_lead_park_notice':dict(self.ref(self.authority),actual_notice_confirmed=True,issued_epoch=0),'not_before_epoch':120,'worker_sha256':self.o.digest(SOURCE),'checker_sha256':self.o.digest(self.check),'operator_B_source_review':self.ref(self.authority),'previous_release':self.previous.name,'config_sha256':self.o.digest(self.config),'authority':'SYNTHETIC_ONLY','installer_env':{},'official_update_source_repo':str(root/'synthetic-git')}
        self.planpath=root/'plan.json';self.planpath.write_text(json.dumps(self.plan))
        self.unit={'ActiveState':'active','SubState':'running','MainPID':'42','InvocationID':'synthetic','ExecMainStartTimestamp':'SYNTHETIC','ExecMainStartTimestampMonotonic':'1000000'}
    def ref(self,p):return {'path':str(p),'sha256':self.o.digest(p)}
    def make_release(self,p,v,r):
        p.mkdir()
        for k,value in {'DEPLOYED_VERSION':v,'DEPLOYED_RELEASE_ID':p.name,'RUNTIME_RELEASE_ID':r,'SERVICE_PYTHON':'/synthetic/python','SERVICE_PATH':'/synthetic/server','TOOLCHAIN_DIR':'/synthetic/toolchain'}.items():(p/k).write_text(value+'\n')
    def switch(self,path):self.current.unlink();self.current.symlink_to(path)
    def bound_install(self):
        return {'canonical_directory':str(self.target),'release_id':self.target.name,
                'version':(self.target/'DEPLOYED_VERSION').read_text().strip(),
                'runtime_id':(self.target/'RUNTIME_RELEASE_ID').read_text().strip(),'resolved_commit':SHA}
    def sample(self):
        p=self.current.resolve();v=(p/'DEPLOYED_VERSION').read_text().strip();r=(p/'RUNTIME_RELEASE_ID').read_text().strip()
        on=self.restart or p==self.previous
        return {'installed':{'version':v,'release_id':p.name},'probe':{'status_code':200,'release_id':p.name,'health_ready':True,'runtime_available':on,'runtime_release_id':r if on else '0'*24},'host_on_installed':True,'runtime_on_installed':on}
    def native_run(self,argv,**kw):
        self.native_queries.append(list(argv))
        if argv==[str(self.check),'--observe-runtime','status']:
            value=self.sample_queue.pop(0) if self.sample_queue else self.sample()
            if isinstance(value,BaseException):raise value
            return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
        if argv[:3]==['/usr/bin/systemctl','--user','show']:
            return subprocess.CompletedProcess(argv,0,''.join(k+'='+v+'\n' for k,v in self.unit.items()),'')
        raise AssertionError('UNEXPECTED_NATIVE_RUN '+repr(argv))
    def child(self,argv,stdout,stderr,**kw):
        # Actual command() owns its request/raw/return files and wait; this
        # fixture implements ONLY the lower child interface, never command().
        self.calls.append(list(argv));rc=0;out=b''
        if argv[1:3]==['update','--ref']:
            self.switch(self.target)
        elif argv[0]=='git':rc=self.git_rc;out=(self.git_result+'\n').encode()
        elif argv[0]==str(self.check) and 'watch' in argv:
            if self.mode.startswith('host_fail'):rc=11
            if self.mode=='host_fail_foreign':self.switch(self.previous)
            if self.mode=='host_fail_marker':(self.target/'DEPLOYED_VERSION').write_text('abcdef0\n')
        elif argv[:3]==['/usr/bin/systemctl','--user','restart']:self.restart=True
        elif len(argv)>2 and argv[2]=='rollback':
            self.switch(self.previous)
        elif argv[0]=='/usr/bin/journalctl':
            out=(json.dumps({'_SYSTEMD_INVOCATION_ID':'synthetic','_PID':'42','MESSAGE':" runtimeReady recovery={'interrupted': 0}"})+'\n').encode()
        elif argv[1:2]==['prompt']:pass
        else:raise AssertionError('UNEXPECTED_NATIVE_CHILD '+repr(argv))
        stdout.write(out);stdout.flush()
        return types.SimpleNamespace(pid=900,wait=lambda timeout=None:rc)
    @contextlib.contextmanager
    def installed(self):
        class ReadonlySyntheticRows:
            row_factory=None
            def execute(self,*args):return []
            def close(self):pass
        # No real database is opened. The zero-interruption path consumes only
        # synthetic empty query rows through the lower connection interface.
        original_path=Path
        pathproxy=types.SimpleNamespace(Path=lambda x: self.config if str(x)=='/home/peter/.config/clarp/config.toml' else original_path(x))
        with contextlib.ExitStack() as stack:
            for attr,value in {'P':self.data,'SHARE':self.share,'CHECK':self.check,'time':self.clock,'pathlib':pathproxy,'sqlite3':types.SimpleNamespace(connect=lambda *a,**k:ReadonlySyntheticRows(),Row=object)}.items():stack.enter_context(patch.object(self.o,attr,value))
            stack.enter_context(patch.object(self.o.subprocess,'run',self.native_run));stack.enter_context(patch.object(self.o.subprocess,'Popen',self.child))
            yield
    def main(self):
        with self.installed(),patch.object(sys,'argv',[str(SOURCE),str(self.planpath),self.o.digest(self.planpath)]):rc=self.o.main()
        result=json.loads((self.out/'RESULT.json').read_text())
        return rc,result

class ReleaseOwnerBoundary(unittest.TestCase):
    def test_single_owner_boundary_table(self):
        # ONE method; each row asserts independently expected outcome/reach.
        for prefix in (7,9,40):
            with self.subTest(row='full_main_prefix_'+str(prefix)),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td),prefix);rc,r=f.main()
                self.assertEqual(rc,0,r);self.assertEqual(r['state'],'RELEASE_AND_RUNTIME_HEALTH_PASS')
                self.assertEqual(r['release_id'],f.release)
                self.assertEqual(r['snapshot_timings'],{'state':'NOT_REQUESTED_OR_EXPLICITLY_SKIPPED','owner_GET_attempts':0})
                self.assertEqual(sum(a[1:3]==['update','--ref'] for a in f.calls),1)
                self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1)
                self.assertFalse(any(a[0]=='/usr/bin/curl' for a in f.calls+f.native_queries))
                self.assertTrue((f.out/'BOUND-INSTALL-OWNERSHIP.json').is_file())
        # Actual lead105714 stability rule at the REAL post-health main path.
        for fault_counts,expected_pass in [({3},True),({8},True),({3,4},False),({3,5},False)]:
            with self.subTest(row='stability_main_timeouts_'+str(sorted(fault_counts))),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));original_run=f.native_run;status_count=[0]
                def run(argv,**kw):
                    if f.restart and argv==[str(f.check),'--observe-runtime','status']:
                        status_count[0]+=1
                        if status_count[0] in fault_counts:raise subprocess.TimeoutExpired(['synthetic-status'],3)
                    return original_run(argv,**kw)
                f.native_run=run;rc,r=f.main()
                self.assertEqual(rc,0 if expected_pass else 11,r)
                series=r['stability'];self.assertEqual(series['original_deadline'],1300)
                self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1)
                self.assertFalse(any(len(a)>2 and a[2]=='rollback' for a in f.calls))
                if expected_pass:
                    self.assertEqual(series['state'],'PASS');self.assertEqual(series['pass_count'],6)
                    self.assertEqual(series['failed_count'],1);self.assertFalse(series['consecutive_failures'])
                    self.assertEqual(len(series['rows']),7);self.assertEqual(series['rows'][-1]['start_mono']-series['rows'][0]['start_mono'],120)
                    failed=[x for x in series['rows'] if not x['all_required_flags']]
                    self.assertIn('TimeoutExpired',failed[0]['errors'][0]['traceback'])
                    self.assertTrue((f.out/'ACTIVATION-HEALTH.json').exists())
                    self.assertFalse(any(a[0]=='/usr/bin/curl' for a in f.calls+f.native_queries))
                else:
                    self.assertEqual(r['state'],'STABILITY_STOP_NO_ACTION')
                    self.assertEqual(series['state'],'STABILITY_RULE_FAILED_STOP_NO_ACTION')
                    self.assertEqual(series['failed_count'],2)
                    self.assertFalse((f.out/'ACTIVATION-HEALTH.json').exists())
                    self.assertTrue((f.out/'lead-stability-stop.request.json').is_file())
        with self.subTest(row='stability_positive_identity_STOPhasno_recovery'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));original_run=f.native_run;calls=[0]
            def run(argv,**kw):
                if f.restart and argv==[str(f.check),'--observe-runtime','status']:
                    calls[0]+=1
                    if calls[0]==3:
                        bad=f.sample();bad['installed']['version']='abcdef0'
                        return subprocess.CompletedProcess(argv,0,json.dumps(bad),'')
                return original_run(argv,**kw)
            f.native_run=run;rc,r=f.main()
            self.assertEqual(rc,11);self.assertIn('positive_installed_identity_changed_during_stability_STOP',r['error'])
            self.assertFalse(any(len(a)>2 and a[2]=='rollback' for a in f.calls))
            self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1)
        with self.subTest(row='stability_insufficient_original300_NOquery_NOrenewal'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));out=Path(td)/'observe';out.mkdir();f.clock.now=1200
            with f.installed():series=f.o.stability_window(out,SHA,RUNTIME,1000,1300,f.bound_install())
            self.assertEqual(series['state'],'INSUFFICIENT_ORIGINAL_BUDGET_STOP_NO_ACTION')
            self.assertEqual(series['original_deadline'],1300);self.assertEqual(f.native_queries,[]);self.assertEqual(f.calls,[])
        with self.subTest(row='stability_finalpublication_fsync_late_NOTPASS'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));f.switch(f.target);f.restart=True;out=Path(td)/'observe';out.mkdir();native_fsync=os.fsync
            def fsync(fd):
                native_fsync(fd)
                summary=out/'STABILITY-WINDOW.json'
                if summary.exists() and os.fstat(fd).st_ino==summary.stat().st_ino:f.clock.now=1301
            with f.installed(),patch.object(f.o.os,'fsync',fsync):series=f.o.stability_window(out,SHA,RUNTIME,1000,1300,f.bound_install())
            self.assertEqual(series['state'],'ORIGINAL_DEADLINE_EXHAUSTED_STOP_NO_ACTION')
            self.assertEqual(len(series['rows']),7);self.assertEqual(series['original_deadline'],1300)
            self.assertEqual(json.loads((out/'STABILITY-WINDOW.json').read_text())['publication'],'CANDIDATE_ONLY')
            self.assertEqual(f.calls,[])
        for delay_kind in ('status','persistence','remaining_budget'):
            with self.subTest(row='actual_stability_spacing_'+delay_kind),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));f.switch(f.target);f.restart=True;out=Path(td)/'observe';out.mkdir();original_run=f.native_run;queries=[0];delayed=[];native_fsync=os.fsync
                def run(argv,**kw):
                    if argv==[str(f.check),'--observe-runtime','status']:
                        queries[0]+=1
                        if delay_kind=='status' and queries[0]==2:f.clock.now+=65
                    return original_run(argv,**kw)
                def fsync(fd):
                    native_fsync(fd);sample=out/'stability-sample-1.json'
                    if delay_kind!='status' and not delayed and sample.exists() and os.fstat(fd).st_ino==sample.stat().st_ino:
                        delayed.append(True);f.clock.now+=210 if delay_kind=='remaining_budget' else 65
                f.native_run=run
                with f.installed(),patch.object(f.o.os,'fsync',fsync):series=f.o.stability_window(out,SHA,RUNTIME,1000,1300,f.bound_install())
                starts=[x['start_mono'] for x in series['rows']]
                self.assertTrue(all(b-a>=20 for a,b in zip(starts,starts[1:])),starts)
                self.assertEqual(series['original_deadline'],1300);self.assertEqual(f.calls,[])
                if delay_kind=='remaining_budget':
                    self.assertEqual(series['state'],'INSUFFICIENT_ORIGINAL_BUDGET_STOP_NO_ACTION')
                    self.assertEqual(len(starts),2);self.assertEqual(queries[0],2);self.assertEqual(f.clock.now,1230)
                else:
                    self.assertEqual(series['state'],'PASS');self.assertEqual(len(starts),7)
                    self.assertGreaterEqual(starts[-1]-starts[0],165)
        for kind in ('same_commit_foreign_canonical','available_foreign_runtime','unavailable_stale_runtime'):
            with self.subTest(row='stability_postinitial_identity_'+kind),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));original_run=f.native_run;queries=[0]
                def run(argv,**kw):
                    if f.restart and argv==[str(f.check),'--observe-runtime','status']:
                        queries[0]+=1
                        if queries[0]==3:
                            if kind=='same_commit_foreign_canonical':
                                foreign=f.releases/(f.release+'-foreign');f.make_release(foreign,f.version,RUNTIME);f.switch(foreign)
                                sample=f.sample()
                            else:
                                sample=f.sample();sample['probe']['runtime_release_id']='f'*24
                                if kind=='unavailable_stale_runtime':sample['probe']['runtime_available']=False;sample['runtime_on_installed']=False
                            return subprocess.CompletedProcess(argv,0,json.dumps(sample),'')
                    return original_run(argv,**kw)
                f.native_run=run;rc,r=f.main()
                self.assertTrue((f.out/'BOUND-INSTALL-OWNERSHIP.json').is_file())
                self.assertTrue((f.out/'stability-sample-0.json').is_file())
                self.assertFalse(any(len(a)>2 and a[2]=='rollback' for a in f.calls))
                self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1)
                if kind=='unavailable_stale_runtime':
                    self.assertEqual(rc,0,r);self.assertEqual(r['stability']['pass_count'],6)
                    self.assertFalse(r['stability']['rows'][1]['all_required_flags'])
                    self.assertEqual(r['stability']['rows'][1]['status']['probe']['runtime_release_id'],'f'*24)
                else:
                    self.assertEqual(rc,11);self.assertIn('positive_installed_release_changed_during_stability_STOP' if kind=='same_commit_foreign_canonical' else 'positive_available_runtime_identity_changed_during_stability_STOP',r['error'])
                    self.assertFalse((f.out/'ACTIVATION-HEALTH.json').exists())
        with self.subTest(row='main_finalownership_changed_after_summary'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));native_fsync=os.fsync;changed=[]
            def fsync(fd):
                native_fsync(fd);summary=f.out/'STABILITY-WINDOW.json'
                if not changed and summary.exists() and os.fstat(fd).st_ino==summary.stat().st_ino:
                    changed.append(True);foreign=f.releases/(f.release+'-late-foreign');f.make_release(foreign,f.version,RUNTIME);f.switch(foreign)
            with patch.object(f.o.os,'fsync',fsync):rc,r=f.main()
            self.assertEqual(changed,[True]);self.assertEqual(rc,11)
            self.assertIn('final_stability_install_ownership_changed_STOP',r['error'])
            self.assertFalse((f.out/'ACTIVATION-HEALTH.json').exists())
            self.assertFalse(any(len(a)>2 and a[2]=='rollback' for a in f.calls))
            self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1)
        with self.subTest(row='main_healthpublication_fsync_originaldeadline_STOP'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));native_fsync=os.fsync;aged=[]
            def fsync(fd):
                native_fsync(fd);health=f.out/'ACTIVATION-HEALTH.json'
                if not aged and health.exists() and os.fstat(fd).st_ino==health.stat().st_ino:
                    aged.append(True);f.clock.now=1301
            with patch.object(f.o.os,'fsync',fsync):rc,r=f.main()
            self.assertEqual(aged,[True]);self.assertEqual(rc,11)
            self.assertIn('final_health_publication_original_deadline_exhausted_STOP',r['error'])
            artifact=json.loads((f.out/'ACTIVATION-HEALTH.json').read_text())
            self.assertEqual(artifact['publication'],'CANDIDATE_PENDING_FINAL_OWNERSHIP_AND_ORIGINAL_DEADLINE')
            self.assertFalse((f.out/'lead-healthy-line.request.json').exists())
            self.assertFalse(any(len(a)>2 and a[2]=='rollback' for a in f.calls))
            self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1)
        for row,git_rc,git_result in [('nonresolving',1,''),('foreign_commit',0,'f'*40)]:
            with self.subTest(row=row),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));f.git_rc=git_rc;f.git_result=git_result;rc,r=f.main()
                self.assertEqual(rc,11);self.assertIn('installed_abbreviation_does_not_uniquely_resolve',r['error'])
                self.assertFalse(any(a[0]==str(f.check) and 'watch' in a for a in f.calls))
        # Reach BOTH real rollback call sites via main, not a mirrored guard.
        for branch in ('host','runtime'):
            for change in (False,True):
                with self.subTest(row=branch+'_rollback_marker_'+str(change)),tempfile.TemporaryDirectory() as td:
                    f=Fixture(Path(td),9,'host_fail_marker' if branch=='host' and change else 'host_fail' if branch=='host' else 'happy')
                    if branch=='runtime':
                        original_run=f.native_run
                        def run(argv,**kw):
                            if f.restart and argv==[str(f.check),'--observe-runtime','status']:
                                s=f.sample();s['probe']['runtime_available']=False;s['runtime_on_installed']=False;return subprocess.CompletedProcess(argv,0,json.dumps(s),'')
                            if change and f.restart and argv[:4]==['/usr/bin/systemctl','--user','show','clarp-runtime.service']:
                                (f.target/'DEPLOYED_VERSION').write_text('abcdef0\n')
                            return original_run(argv,**kw)
                        f.native_run=run
                        f.unit['ActiveState']='failed'
                    rc,r=f.main();self.assertEqual(rc,11)
                    rolls=[a for a in f.calls if len(a)>2 and a[2]=='rollback']
                    if change:
                        self.assertEqual(rolls,[])
                        self.assertIn('rollback_ownership_UNKNOWN',r['error'])
                    else:
                        self.assertEqual(len(rolls),1,r)
                        self.assertEqual(rolls[0][-1],f.previous.name)
        for branch in ('host','runtime'):
            for drift in ('canonical_same_version','runtime_marker','release_marker'):
                with self.subTest(row=branch+'_ownership_'+drift),tempfile.TemporaryDirectory() as td:
                    f=Fixture(Path(td),9,'host_fail' if branch=='host' else 'happy')
                    changed=[]
                    def mutate():
                        if changed:return
                        changed.append(drift)
                        if drift=='canonical_same_version':
                            foreign=f.releases/(f.release+'-foreign')
                            f.make_release(foreign,f.version,RUNTIME);f.switch(foreign)
                        elif drift=='runtime_marker':(f.target/'RUNTIME_RELEASE_ID').write_text('f'*24+'\n')
                        else:(f.target/'DEPLOYED_RELEASE_ID').write_text(f.release+'-different-marker\n')
                    if branch=='host':
                        original_child=f.child
                        def child(argv,**kw):
                            result=original_child(argv,**kw)
                            if argv[0]==str(f.check) and 'watch' in argv:mutate()
                            return result
                        f.child=child
                    else:
                        f.unit['ActiveState']='failed';f.unit['MainPID']='0'
                        original_run=f.native_run
                        def run(argv,**kw):
                            if f.restart and argv==[str(f.check),'--observe-runtime','status']:
                                s=f.sample();s['probe']['runtime_available']=False;s['runtime_on_installed']=False
                                return subprocess.CompletedProcess(argv,0,json.dumps(s),'')
                            if f.restart and argv[:4]==['/usr/bin/systemctl','--user','show','clarp-runtime.service']:mutate()
                            return original_run(argv,**kw)
                        f.native_run=run
                    rc,r=f.main();self.assertEqual(rc,11);self.assertEqual(changed,[drift])
                    self.assertIn(('runtime_failure_' if branch=='runtime' else '')+'rollback_ownership_UNKNOWN',r['error'])
                    self.assertTrue((f.out/'BOUND-INSTALL-OWNERSHIP.json').is_file())
                    self.assertTrue((f.out/'normal-Host-readiness.return.json').is_file())
                    self.assertFalse(any(len(a)>2 and a[2]=='rollback' for a in f.calls))
                    self.assertEqual(sum(a[:3]==['/usr/bin/systemctl','--user','restart'] for a in f.calls),1 if branch=='runtime' else 0)
                    if branch=='runtime':
                        rows=[json.loads(x) for x in (f.out/'runtime-health.jsonl').read_text().splitlines()]
                        self.assertEqual(rows[-1]['classification'],'RUNTIME_UNIT_FAILED')
                    if drift=='canonical_same_version':
                        self.assertNotEqual(f.current.resolve(),f.target)
                        self.assertEqual((f.current/'DEPLOYED_VERSION').read_text().strip(),f.version)
        with self.subTest(row='status_acquisition_timeout_independent_failed_unit'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));f.switch(f.target);f.restart=True
            f.unit['ActiveState']='failed';f.unit['MainPID']='0'
            f.sample_queue=[subprocess.TimeoutExpired(['synthetic-status'],3)]
            out=Path(td)/'observe';out.mkdir()
            with f.installed():healthy,u,rows=f.o.runtime_wait(out,SHA,RUNTIME,1000,1300)
            self.assertIsNone(healthy);self.assertEqual(rows[0]['classification'],'RUNTIME_UNIT_FAILED')
            self.assertEqual(f.clock.now,1000);self.assertEqual(f.calls,[])
            self.assertEqual(rows[0]['observation_errors'][0]['part'],'status')
            self.assertIn('TimeoutExpired',rows[0]['observation_errors'][0]['traceback'])
            self.assertEqual(len(f.native_queries),2)
            self.assertEqual(f.native_queries[1][:4],['/usr/bin/systemctl','--user','show','clarp-runtime.service'])
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));f.switch(f.target);f.restart=True;template=f.sample()
        malformed=[[],None,{'installed':None},'bad',{'probe':None}]
        for container,key,value in [('installed','version',[]),('probe','status_code',True),('probe','runtime_available',1),('probe','health_ready',None),('probe','release_id',{}),('probe','runtime_release_id',[])]:
            bad=copy.deepcopy(template);bad[container][key]=value;malformed.append(bad)
        bad=copy.deepcopy(template);bad['host_on_installed']=1;malformed.append(bad)
        cases=[('timeout',subprocess.TimeoutExpired(['synthetic'],3)),('unreachable','unreachable'),('readyfalse','readyfalse')]+[('malformed_'+str(i),s) for i,s in enumerate(malformed)]
        for name,bad in cases:
            with self.subTest(row=name),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));f.switch(f.target);f.restart=True;good=f.sample()
                if bad=='unreachable':bad=copy.deepcopy(good);bad['probe']['runtime_available']=False
                elif bad=='readyfalse':bad=copy.deepcopy(good);bad['probe']['health_ready']=False
                f.sample_queue=[bad,good]
                out=Path(td)/'observe';out.mkdir()
                with f.installed():s,u,rows=f.o.runtime_wait(out,SHA,RUNTIME,1000,1300)
                self.assertEqual(s,good);self.assertEqual(len(rows),2)
                self.assertTrue('UNKNOWN' in rows[0] or rows[0].get('classification')=='TRANSIENT_UNKNOWN_OR_WARMUP_NO_IDENTITY_FAILURE')
                self.assertEqual(f.calls,[],'observer must never control/restart/rollback')
        for drift in ('installed','Host'):
            with self.subTest(row='positive_'+drift+'_STOP'),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));f.switch(f.target);f.restart=True;s=f.sample()
                if drift=='installed':s['installed']['version']='abcdef0'
                else:s['probe']['release_id']='foreign-release'
                f.sample_queue=[s];out=Path(td)/'observe';out.mkdir()
                with f.installed(),self.assertRaisesRegex(RuntimeError,'positive_.*identity_changed_STOP'):f.o.runtime_wait(out,SHA,RUNTIME,1000,1300)
                self.assertEqual(f.calls,[])
                self.assertIn('POSITIVE_',json.loads((out/'runtime-health.jsonl').read_text())['classification'])
        for mixed in ('foreign_install_partial_timeout','Host200_mismatch_missing_health','failed_unit_partial_timeout'):
            with self.subTest(row=mixed),tempfile.TemporaryDirectory() as td:
                f=Fixture(Path(td));f.switch(f.target);f.restart=True;s=f.sample()
                if mixed=='Host200_mismatch_missing_health':
                    s['probe']['release_id']='foreign-release';del s['probe']['health_ready']
                else:
                    s['probe']={'status_code':0,'error':'TimeoutError'}
                    if mixed=='foreign_install_partial_timeout':s['installed']['version']='abcdef0'
                    else:f.unit['ActiveState']='failed';f.unit['MainPID']='0'
                f.sample_queue=[s];out=Path(td)/'observe';out.mkdir()
                with f.installed():
                    if mixed=='failed_unit_partial_timeout':
                        healthy,u,rows=f.o.runtime_wait(out,SHA,RUNTIME,1000,1300)
                        self.assertIsNone(healthy);self.assertEqual(rows[0]['classification'],'RUNTIME_UNIT_FAILED')
                    else:
                        with self.assertRaisesRegex(RuntimeError,'positive_.*identity_changed_STOP'):f.o.runtime_wait(out,SHA,RUNTIME,1000,1300)
                        rows=[json.loads(x) for x in (out/'runtime-health.jsonl').read_text().splitlines()]
                        self.assertEqual(rows[0]['classification'],'POSITIVE_INSTALLED_IDENTITY_CHANGED' if mixed=='foreign_install_partial_timeout' else 'POSITIVE_HOST_IDENTITY_CHANGED')
                self.assertEqual(f.clock.now,1000,'independent positive evidence must not wait300')
                self.assertEqual(f.calls,[])
        with self.subTest(row='persist_crosses_original300_noPASS'),tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td));f.switch(f.target);f.restart=True;f.clock.now=1299
            out=Path(td)/'observe';out.mkdir();native_fsync=os.fsync
            def fsync(fd):
                native_fsync(fd)
                if os.fstat(fd).st_ino==(out/'runtime-health.jsonl').stat().st_ino:f.clock.now=1301
            with f.installed(),patch.object(f.o.os,'fsync',fsync):s,u,rows=f.o.runtime_wait(out,SHA,RUNTIME,1000,1300)
            self.assertIsNone(s);self.assertEqual(rows[-1]['classification'],'POSITIVE_BUT_PERSISTENCE_LATE_NOT_ACCEPTED')
            self.assertTrue(all(x['original_deadline']==1300 for x in rows));self.assertEqual(f.calls,[])

if __name__=='__main__':unittest.main()
