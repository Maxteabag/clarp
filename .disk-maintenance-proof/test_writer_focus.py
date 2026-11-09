"""Four source-only focused groups. Synthetic syscall receipts; no live service/DB."""
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import controller
import post_check
from writer_policy import validate_workers,classify_fd,WriterUnknown,classify_post

class WriterFocus(unittest.TestCase):
    def setUp(self):
        self.rows=[{'unit':'clarp-job-private-%d.service'%n,'pid':1000+n,'start':str(2000+n),
                    'phase':'ACTIVE','boot':'private-boot','invocation':'private-'+str(n),
                    'cgroup':'/private/unit-'+str(n),'uid':os.getuid(),
                    'job_id':'private-'+str(n),'owner':'private-owner-'+str(n),'generation':1} for n in range(12)]
        self.workers=validate_workers(self.rows,[])
        self.observed={w['unit']:dict(w,state='active') for w in self.rows}
        self.protected={role:{'dev':9,'inode':12+i} for i,role in enumerate(('state.sqlite','state.sqlite-wal','state.sqlite-shm'))}
        self.fd={'role':'state.sqlite','boot':'private-boot','dev':9,'inode':12,'pid':1000,'start':'2000',
                 'uid':os.getuid(),'cgroup':'/private/unit-0'}

    def census(self,fd_group=None):
        # External native filesystem/unit responders; actual writer_census runs.
        case=self
        class File:
            def __init__(self,role):self.role=role;self.name='4'
            def stat(self):return SimpleNamespace(st_dev=9,st_ino=12+list(case.protected).index(self.role))
            def __str__(self):return '/private/'+self.role
        class Share:
            def __truediv__(self,role):return File(role)
        class FDDir:
            def iterdir(self):return iter([File('state.sqlite')])
        class Proc:
            name='1000'
            def stat(self):return SimpleNamespace(st_uid=os.getuid())
            def __truediv__(self,name):return FDDir()
        class ProcRoot:
            def iterdir(self):return iter([Proc()])
        owner=controller.Controller({'tolerated_workers':self.rows,'auxiliary_units':[],'timers':[],
                                     'protected_db_files':self.protected},Path('/private-unused'))
        events=[];identity={k:self.fd[k] for k in ('pid','start','uid','cgroup','boot')}
        if fd_group:identity['cgroup']=fd_group
        with patch.object(controller,'SHARE',Share()),patch.object(controller,'Path',return_value=ProcRoot()),patch('controller.os.readlink',return_value='/private/state.sqlite'),patch.object(owner,'worker_snapshot',side_effect=lambda w:self.observed[w['unit']]),patch.object(owner,'process_observation',side_effect=lambda p:dict(identity)),patch.object(owner,'require',return_value=''),patch.object(owner,'log',side_effect=lambda kind,**kw:events.append((kind,kw))):
            owner.writer_census()
        return events

    def test_exact_tolerated_set_accepted_without_stopping_units(self):
        for w in self.rows:
            fd=dict(self.fd,pid=w['pid'],start=w['start'],cgroup=w['cgroup'])
            self.assertEqual(classify_fd(fd,self.workers,self.observed,self.protected),w['unit'])
        events=self.census()
        self.assertEqual(events[0][1]['accepted'][0]['unit'],self.rows[0]['unit'])
        # Preserve original universe with a real private prefinished receipt.
        with tempfile.TemporaryDirectory() as d:
            w=self.rows[1];log=Path(d)/'finished.LOG.exit';log.write_text('0\n');st=log.stat()
            context={'job_id':w['job_id'],'generation':1,'owner':w['owner'],'worker_pid':w['pid'],'worker_start_token':w['boot']+':'+w['start']}
            seal={'path':str(log),'context':context,'version':[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns],'sha256':hashlib.sha256(log.read_bytes()).hexdigest()}
            w.update(phase='PREFINISHED',log_exit={'path':str(log),'sealed':seal},
                     pre_registry={'job_id':w['job_id'],'session':w['owner'],'generation':1,'worker_pid':w['pid'],'worker_start_token':w['boot']+':'+w['start'],'status':'succeeded','terminal_reason':''})
            self.observed[w['unit']].update(state='inactive',pid=0,original_gone=True,cgroup_empty=True)
            self.census()
            self.observed[w['unit']]['state']='active'
            with self.assertRaisesRegex(controller.Refused,'terminal_native_unknown'):self.census()

    def test_unlisted_db_fd_including_listener_is_rejected(self):
        for group in ('/foreign','/private/unit-0-similar','/bookkeeping'):
            with self.subTest(group=group),self.assertRaisesRegex(controller.Refused,'unlisted_db_fd'):
                self.census(group)
        with self.assertRaisesRegex(WriterUnknown,'protected_db_fd_rebound'):
            classify_fd(dict(self.fd,inode=999),self.workers,self.observed,self.protected)

    def test_changed_pid_start_or_unit_generation_rejected(self):
        for key,value in (('pid',5000),('start','newbirth'),('invocation','newinv'),('cgroup','/foreign')):
            with self.subTest(key=key):
                original=copy.deepcopy(self.observed)
                self.observed[self.rows[0]['unit']][key]=value
                with self.assertRaisesRegex(controller.Refused,'worker_incarnation_changed'):self.census()
                self.observed=original
        owner=controller.Controller({},Path('/private-unused'))
        proc=SimpleNamespace(name='1000');fd=SimpleNamespace(name='4',stat=lambda:SimpleNamespace(st_dev=9,st_ino=12))
        before={k:self.fd[k] for k in ('pid','start','uid','cgroup','boot')}
        for key,value in (('cgroup','/foreign'),('uid',os.getuid()+1),('start','changed')):
            with self.subTest(helper_identity=key),patch.object(owner,'process_observation',side_effect=[before,dict(before,**{key:value})]):
                with self.assertRaisesRegex(controller.Refused,'fd_full_identity_rebound'):owner.capture_fd(proc,fd,'state.sqlite')

    def test_postcheck_heartbeat_finished_and_mismark_are_distinct_no_edit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);pre_ms=int(time.time()*1000)-1000
            w=self.rows[0];log=root/'private.LOG.exit'
            for row in self.rows:row['log_exit']={'path':str(log),'pre_state':'ABSENT'}
            def registry():
                return {x['job_id']:{'job_id':x['job_id'],'session':x['owner'],'generation':1,'status':'running',
                                    'worker_pid':x['pid'],'worker_start_token':x['boot']+':'+x['start'],'heartbeat_at':101} for x in self.rows}
            scenarios=[(0,'failed','worker_vanished','MIS_MARK_REPORT_LEAD_AND_OWNER'),
                       (1,'failed','heartbeat_expired','MIS_MARK_REPORT_LEAD_AND_OWNER'),
                       (1,'failed','exit 1','FINISHED_LOG_EXIT_RECONCILED')]
            for index,(rc,status,reason,expected) in enumerate(scenarios):
                with self.subTest(reason=reason):
                    log.write_text(str(rc)+'\n');s=log.stat();raw=log.read_bytes()
                    context={'job_id':w['job_id'],'owner':w['owner'],'generation':1,'worker_pid':w['pid'],'worker_start_token':w['boot']+':'+w['start']}
                    receipt={'path':str(log),'context':context,'version':[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns],'sha256':hashlib.sha256(raw).hexdigest()}
                    rows=registry();rows[w['job_id']].update(status=status,terminal_reason=reason)
                    native=copy.deepcopy(self.observed)
                    native[w['unit']].update(state='inactive',pid=0,original_gone=True,cgroup_empty=True)
                    for variant in ('valid','changed-native','stale-receipt','changed-row-birth'):
                        with self.subTest(variant=variant):
                            actual=copy.deepcopy(native);db=copy.deepcopy(rows);binding=copy.deepcopy(receipt)
                            if variant=='changed-native':actual[w['unit']]['invocation']='foreign'
                            if variant=='stale-receipt':binding['sha256']='0'*64
                            if variant=='changed-row-birth':db[w['job_id']]['worker_start_token']='foreign:birth'
                            data={'schema':'clarp.disk-writer-post-metadata/1','controller_binding':{'tolerated_workers':self.rows,'auxiliary_units':[],'timers':[],'protected_db_files':self.protected},
                                  'pre_ms':pre_ms,'restart_ms':100,'has_exit_receipt':{x['unit']:x['unit']==w['unit'] for x in self.rows},'exit_bindings':{w['unit']:binding}}
                            path=root/'input.json';payload=json.dumps(data).encode();path.write_bytes(payload)
                            output=root/('out-'+str(index)+'-'+variant+'.json')
                            with patch('post_check.sys.argv',['post',str(path),hashlib.sha256(payload).hexdigest(),str(output)]),patch('post_check.capture_registry',return_value=db),patch.object(controller.Controller,'worker_snapshot',side_effect=lambda x:actual[x['unit']]):
                                result_rc=post_check.main()
                            result=json.loads(output.read_bytes())['results'][0]
                            self.assertEqual(result['state'],expected if variant=='valid' else 'UNKNOWN_REQUIRES_OWNER')
                            self.assertEqual(result['automatic_status_edit'],False)
                            if expected.startswith('MIS_') or variant!='valid':self.assertEqual(result_rc,11)
                            else:self.assertEqual(result_rc,0)
                            if variant=='stale-receipt':
                                self.assertIn('Traceback (most recent call last)',result['traceback'])
                                self.assertIn('read_exit',result['traceback'])
                                self.assertIn('WriterUnknown',result['traceback'])
                                self.assertIn('exit_version_hash_changed',result['traceback'])
                                self.assertIn('exit_version_hash_changed',result['error'])
                            self.assertEqual(db[w['job_id']]['status'],status)

if __name__=='__main__':unittest.main()
