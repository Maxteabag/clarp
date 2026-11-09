"""ONE UNEXECUTED source-qualified group, synthetic lower native/process inputs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import controller
import roster
import post_check
from owned import CustodyUnknown
from writer_policy import validate_workers, read_exit, context_of, version_of, WriterUnknown
from test_writer_focus import WriterFocus

class CurrentRosterFocus(unittest.TestCase):
    def test_actual_current_roster_freeze_new_writer_refusal_and_admission(self):
        # Regression owner: actual run->producer->registry->receipt->stop gate and
        # actual post-stop census/classifier; no copied producer result fixture.
        for scenario in ('current11','new_fd','changed_birth','helper_unknown','missing_registry','roster_change','setup_age','final_capture_hb_expiry','setup_hb_expiry'):
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);case=WriterFocus();case.setUp()
                case.rows=case.rows[:11];case.observed={w['unit']:dict(w,state='active') for w in case.rows}
                rows=[{'job_id':w['job_id'],'session':w['owner'],'generation':1,'status':'running','heartbeat_at':1000000,
                       'worker_pid':w['pid'],'worker_start_token':w['boot']+':'+w['start'],'terminal_reason':'',
                       'log_path':str(root/('payload-'+str(i)+'.LOG'))} for i,w in enumerate(case.rows)]
                if scenario=='final_capture_hb_expiry':
                    for row in rows:row['heartbeat_at']=941000 #59s old at fresh read
                if scenario=='setup_hb_expiry':
                    for row in rows:row['heartbeat_at']=940500 #59.5s, but receipt itself is new
                old=root/'payload-0.LOG.exit';old.write_text('143\n');oldstat=old.stat()
                image=root/'class-source';image.write_text('private synthetic source\n')
                _,seal=roster.read_bound(image)
                policy={'worker_images':{str(image):seal},'helper_images':{}}
                binding={'auxiliary_units':['private-listener.service'],'timers':['private.timer'],
                         'expected_active_invocations':{n:'private' for n in controller.MAIN+['private-listener.service']},
                         'protected_db_files':case.protected,'roster_policy':policy,'roster_policy_sha256':roster.digest(policy),
                         'roster_policy_review':{'sha256':'private-reviewed-policy'},'retired_worker_history':[{'unit':'old-retired','identity':'UNKNOWN','exit':'ABSENT'}],
                         # Deliberately poisoned cached input; it must not provide fresh admission.
                         'tolerated_workers':[{'pre_registry':{'heartbeat_at':0}}]}
                owner=controller.Controller(binding,root);now=[100.];events=[];reads=[0];enumerations=[0];native_counts={}
                states={n:'active' for n in controller.MAIN+['private-listener.service','private.timer']}
                class File:
                    def __init__(self,name):self.name=name
                    def stat(self):return SimpleNamespace(st_dev=9,st_ino=12+list(case.protected).index(self.name))
                    def __str__(self):return '/private/'+self.name
                class Share:
                    def __truediv__(self,name):return File(name)
                owner.protected_root=Share()
                class FD:
                    name='4'
                    def stat(self):return SimpleNamespace(st_dev=9,st_ino=12)
                class FDDir:
                    def iterdir(self):return iter([FD()])
                class Proc:
                    def __init__(self,pid):self.name=str(pid)
                    def __truediv__(self,name):
                        if name!='fd':raise AssertionError('unmodelled proc field '+name)
                        return FDDir()
                class ProcRoot:
                    def __truediv__(self,value):
                        text=str(value)
                        if not text.isascii() or not text.isdigit() or int(text) not in {w['pid'] for w in case.rows}:
                            raise AssertionError('unmodelled real process access')
                        return Proc(int(text))
                def proc_path(value):
                    text=str(value)
                    if text=='/proc':return ProcRoot()
                    if text.startswith('/proc/') and text.rsplit('/',1)[-1].isdigit():
                        return ProcRoot()/text.rsplit('/',1)[-1]
                    if text.startswith('/proc/') and text!='/proc/sys/kernel/random/boot_id':
                        raise AssertionError('unmodelled proc path '+text)
                    return Path(value)
                def process_observation(proc):
                    row=next(w for w in case.rows if w['pid']==int(proc.name))
                    return {k:row[k] for k in ('pid','start','uid','cgroup','boot')}
                def image_observation(n):
                    if scenario=='final_capture_hb_expiry' and all(v>1 for v in native_counts.values()):now[0]+=1.1
                    return {'image':seal,'argv_sha256':'private'}
                class Child:
                    def __init__(self,argv,output,env,**kw):
                        self.reap_started=False;self.argv=argv
                        if 'stop' in argv and not any(e[0]=='stop' for e in events):
                            if scenario=='setup_age':now[0]+=3
                            if scenario=='setup_hb_expiry':now[0]+=.6
                        if kw.get('work_permit'):kw['work_permit'].check()
                        text=''
                        if 'list-units' in argv:
                            enumerations[0]+=1;names=sorted(case.observed)
                            if scenario=='roster_change' and enumerations[0]>1:names+=['clarp-job-new.service']
                            text='\n'.join(n+' loaded active running' for n in names)
                        elif argv[0]=='/usr/bin/sqlite3':
                            reads[0]+=1;events.append(('real_registry_read',reads[0]))
                            text=json.dumps(rows[:-1] if scenario=='missing_registry' else rows)
                        elif 'status' in argv:text="No balance found on '/'\n"
                        elif 'usage' in argv:text='Device unallocated: 3221225472\n'
                        elif 'stop' in argv:events.append(('stop',argv[-1]));states[argv[-1]]='inactive'
                        elif 'start' in argv:events.append(('start',argv[-1]));states[argv[-1]]='active'
                        output.write(text.encode())
                    def wait(self,seconds):self.reap_started=True;return 0
                    def cleanup(self):self.reap_started=True
                def native(producer,name):
                    native_counts[name]=native_counts.get(name,0)+1
                    result=copy.deepcopy(case.observed[name])
                    if scenario=='changed_birth' and native_counts[name]>1:result['start']='changed'
                    return result
                def helper(producer,pid,n,row):
                    if scenario=='helper_unknown':raise WriterUnknown('effective_agent_bg_unqualified')
                    return {'native':dict(n),'exe':seal,'source_files':{str(image):seal},'context':{'job_id':row['job_id']}}
                def unit(name):return {'ActiveState':states[name],'MainPID':'123' if states[name]=='active' else '0','InvocationID':'private','ControlGroup':''}
                def census():
                    events.append(('frozen_census',len(owner.b['tolerated_workers'])))
                    case.rows=copy.deepcopy(owner.b['tolerated_workers']);case.workers=validate_workers(case.rows,[])
                    case.census('/new-unlisted-job' if scenario=='new_fd' else None)
                handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
                try:
                    with patch('controller.time.monotonic',side_effect=lambda:now[0]),patch('controller.time.time',side_effect=lambda:1000.+now[0]-100.),patch('controller.Owned',Child),patch.object(owner,'same_release'),patch.object(owner,'unit',side_effect=unit),patch.object(owner,'writer_census',side_effect=census),patch.object(owner,'pass_balance',side_effect=lambda usage:events.append(('balance',usage)) or True),patch.object(roster.Producer,'native',native),patch.object(roster.Producer,'worker_image',side_effect=image_observation),patch.object(roster.Producer,'pids',side_effect=lambda n:[n['pid']]),patch.object(roster.Producer,'helper',helper),patch('roster.Path',side_effect=proc_path),patch.object(owner,'process_observation',side_effect=process_observation):
                        rc=owner.run()
                    if scenario=='current11':
                        self.assertEqual(rc,0);self.assertEqual(len(owner.b['tolerated_workers']),11)
                        self.assertEqual(reads[0],2);self.assertGreaterEqual(enumerations[0],2)
                        self.assertLess(events.index(('real_registry_read',2)),events.index(('stop','private.timer')))
                        self.assertIn(('frozen_census',11),events);self.assertIn(('balance',10),events)
                        admission=owner.roster_admission;frozen=roster.load_receipt(admission['path'],admission['sha256'])
                        self.assertEqual(frozen['retired_history'],binding['retired_worker_history'])
                        fds=frozen['capture']['db_fds'];self.assertEqual(len(fds),11)
                        for record,witness in zip(sorted(fds,key=lambda x:x['pid']),sorted(case.rows,key=lambda x:x['pid'])):
                            self.assertEqual({k:record[k] for k in ('unit','pid','start','uid','boot','cgroup')},
                                             {k:witness[k] for k in ('unit','pid','start','uid','boot','cgroup')})
                            self.assertEqual((record['role'],record['dev'],record['inode'],record['fd']),('state.sqlite',9,12,4))
                        self.assertNotIn('old-retired',{w['unit'] for w in frozen['active_workers']})
                        w=frozen['active_workers'][0];self.assertEqual(w['log_exit']['pre_state'],'HISTORICAL')
                        oldbinding=dict(w['log_exit']['historical'])
                        with self.assertRaisesRegex(WriterUnknown,'unchanged_historical_exit'):read_exit(oldbinding,w,frozen['end_epoch_ms'])
                        old.write_text('143\n');os.utime(old,ns=(oldstat.st_atime_ns,oldstat.st_mtime_ns+2000000));s=old.stat()
                        newbinding={'path':str(old),'context':context_of(w),'version':version_of(s),'sha256':hashlib.sha256(old.read_bytes()).hexdigest()}
                        self.assertEqual(read_exit(newbinding,w,frozen['end_epoch_ms'])['rc'],143) # same rc/bytes, new version
                        # Actual post entry consumes the sealed frozen roster, not caller-selected rows.
                        data={'schema':'clarp.disk-writer-post-metadata/1','roster_receipt':{'path':admission['path'],'sha256':admission['sha256']},
                              'original_binding_sha256':frozen['original_binding_sha256'],'controller_binding':{'tolerated_workers':[]},
                              'pre_ms':frozen['end_epoch_ms'],'restart_ms':1000000,'has_exit_receipt':{x['unit']:False for x in frozen['active_workers']},'exit_bindings':{}}
                        inp=root/'post.json';raw=json.dumps(data).encode();inp.write_bytes(raw);out=root/'post-result.json'
                        postrows={x['job_id']:dict(x,heartbeat_at=1000001) for x in rows}
                        with patch('post_check.sys.argv',['post',str(inp),hashlib.sha256(raw).hexdigest(),str(out)]),patch('post_check.capture_registry',return_value=postrows),patch.object(controller.Controller,'worker_snapshot',side_effect=lambda x:case.observed[x['unit']]):
                            self.assertEqual(post_check.main(),0)
                        self.assertEqual(len(json.loads(out.read_bytes())['results']),11)
                    else:
                        self.assertEqual(rc,11);self.assertFalse(any(e[0]=='balance' for e in events))
                        if scenario!='new_fd':self.assertFalse(any(e[0]=='stop' for e in events))
                        if scenario=='new_fd':self.assertIn(('frozen_census',11),events)
                        if scenario in ('setup_age','setup_hb_expiry'):self.assertTrue(owner.roster_admission)
                        if scenario in ('final_capture_hb_expiry','setup_hb_expiry'):
                            self.assertTrue(any('heartbeat_expired' in e for e in owner.operational_errors))
                            if scenario=='final_capture_hb_expiry':self.assertIsNone(owner.roster_admission)
                            else:
                                self.assertLess(owner.roster_admission['deadline'],owner.roster_admission['end']+2)
                                self.assertLess(now[0]-owner.roster_admission['end'],2) # receipt age still legal
                        if scenario=='setup_age':
                            self.assertTrue(any('current_roster_stop_admission_stale' in e for e in owner.operational_errors))
                finally:
                    for s,handler in handlers.items():signal.signal(s,handler)


        # Actual Producer.helper parsing; only lower proc/env/native files respond.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'lib').mkdir();(root/'scripts').mkdir()
            script=root/'scripts/agent_bg.py';exe=root/'python';db=root/'state.sqlite'
            files=[script,root/'lib/background_jobs.py',root/'lib/db.py',root/'lib/timing.py',root/'lib/background_job_process.py']
            for path in files+[exe,db]:path.write_text('private class fixture\n')
            seals={str(path):roster.read_bound(path)[1] for path in files};exe_seal=roster.read_bound(exe)[1]
            n={'pid':111,'start':'worker-start','uid':os.getuid(),'cgroup':'/private-worker','boot':'private-boot'}
            row={'session':'owner','generation':1,'job_id':'job'}
            helper_native=dict(n,pid=222,start='helper-start')
            producer=object.__new__(roster.Producer)
            producer.o=SimpleNamespace(process_observation=lambda p:dict(helper_native),protected_root=root)
            producer.policy={'helper_images':{str(script):{'exe':exe_seal,'code_root':str(root),'files':seals}}}
            class Field:
                def __init__(self,name):self.name=name
                def read_text(self):
                    if self.name!='stat':raise AssertionError('unmodelled field')
                    return '222 (private) S 111'
                def resolve(self,strict=True):
                    if self.name!='exe':raise AssertionError('unmodelled resolve')
                    return exe
            class Proc:
                def __truediv__(self,name):return Field(name)
            class HelperProcRoot:
                def __truediv__(self,value):
                    if str(value)!='222':raise AssertionError('unmodelled helper PID')
                    return Proc()
            def helper_path(value):
                text=str(value)
                if text=='/proc':return HelperProcRoot()
                if text=='/proc/222':return HelperProcRoot()/'222'
                if text.startswith('/proc/') and text!='/proc/sys/kernel/random/boot_id':
                    raise AssertionError('unmodelled helper proc path '+text)
                return Path(value)
            for selector in ('111','999','',None):
                with self.subTest(effective_worker_pid=selector):
                    env={'CLARP_CODE_ROOT':str(root),'CLAUDE_PWA_DB':str(db)}
                    if selector is not None:env['CLARP_BACKGROUND_WORKER_PID']=selector
                    env_raw=b'\0'.join((k+'='+v).encode() for k,v in env.items())+b'\0'
                    argv=b'\0'.join(x.encode() for x in [str(exe),str(script),'owner','job-active','bg1:1:job'])+b'\0'
                    def proc_read(path,limit):
                        return {'cmdline':argv,'environ':env_raw}[path.name]
                    with patch.object(producer,'tick'),patch('roster.Path',side_effect=helper_path),patch('roster.proc_bytes',side_effect=proc_read):
                        if selector=='111':
                            witness=producer.helper(222,n,row)
                            self.assertEqual(witness['effective_worker_pid'],111)
                            self.assertEqual(witness['effective_worker_start_token'],'private-boot:worker-start')
                            self.assertIn(str(root/'lib/background_job_process.py'),witness['source_files'])
                        else:
                            with self.assertRaisesRegex(WriterUnknown,'helper_effective_worker_pid_unknown_or_changed'):
                                producer.helper(222,n,row)

if __name__=='__main__':unittest.main()
