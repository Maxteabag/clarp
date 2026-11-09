"""ONE UNEXECUTED finite timer group; synthetic clock/commands, actual owner flow."""
import os
import hashlib
import stat
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import controller
from owned import CustodyUnknown
from writer_policy import validate_heartbeat_age, WriterUnknown

class Clock:
    def __init__(self):self.now=100.
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds

class TimingFocus(unittest.TestCase):
    def test_actual_controller_shared_phase_cancel_order_and_420_narrow_recovery(self):
        cases=[
            {'name':'enough','finish':10,'space':3*1024**3,'passes':[10],'overlap':False},
            {'name':'late25','finish':151,'space':1,'passes':[10],'overlap':False},
            {'name':'setup25','finish':149,'space':1,'age25':2,'passes':[10],'overlap':False},
            {'name':'two','finish':20,'space':1,'passes':[10,25],'overlap':False},
            {'name':'cancel300','finish':1000,'space':1,'terminal':280,'passes':[10],'overlap':False},
            {'name':'statusabortthenpositive','finish':1000,'space':1,'terminal':250,'passes':[10],'overlap':False,'status_abort':True},
            {'name':'unknown420','finish':1000,'space':1,'terminal':None,'passes':[10],'overlap':True},
            {'name':'unknown420startlogfault','finish':1000,'space':1,'terminal':None,'passes':[10],'overlap':True,'fault':True},
        ]
        for case in cases:
            with self.subTest(case=case['name']),tempfile.TemporaryDirectory() as directory:
                clock=Clock();events=[];owner=None;handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
                row={'job_id':'private','generation':1,'session':'private-owner','worker_pid':123,
                     'worker_start_token':'private-boot:234','status':'running','heartbeat_at':1000000}
                worker={'job_id':'private','generation':1,'owner':'private-owner','pid':123,'boot':'private-boot','start':'234','pre_registry':row}
                states={name:'active' for name in controller.MAIN+['private-listener.service','private.timer']}
                binding={'auxiliary_units':['private-listener.service'],'timers':['private.timer'],
                         'expected_active_invocations':{name:'private' for name in controller.MAIN+['private-listener.service']},'tolerated_workers':[worker],
                         'balance_timing':{'overlap_at420':True}}
                owner=controller.Controller(binding,Path(directory))
                initial=clock.now
                class Child:
                    def __init__(self,argv,stream,env,**kw):
                        self.reap_started=False;self.signals=[]
                        usage=int(argv[-2].split('=')[1]);self.usage=usage
                        if usage==25:clock.sleep(case.get('age25',0))
                        permit=kw['work_permit']
                        try:permit.check()
                        except CustodyUnknown as exc:
                            raise CustodyUnknown('fixture expiry',{'primary':str(exc),'gate_released':False,'birth':['private'],'cleanup_errors':[]}) from exc
                        events.append(('balance',usage,clock.now))
                        self.finish=owner.balance_origin+case['finish']
                    def terminal_before(self,deadline):
                        if self.finish<=deadline:
                            clock.now=max(clock.now,self.finish);return True
                        clock.now=max(clock.now,deadline);return False
                    def wait(self,seconds):self.reap_started=True;events.append(('balance_terminal_wait',clock.now));return 0
                    def cleanup(self):self.reap_started=True;events.append(('balance_cleanup',clock.now))
                def command(argv,seconds=10):
                    events.append(('command',tuple(argv),clock.now))
                    owner.last_gate_bracket=(clock.now,clock.now)
                    if 'stop' in argv:
                        states[argv[-1]]='inactive';clock.sleep(1)
                    if 'cancel' in argv:events.append(('cancel',clock.now));return 0,''
                    if 'usage' in argv:return 0,'Device unallocated: '+str(case['space'])+'\n'
                    if 'status' in argv:
                        if owner.balance_origin is None:return 0,"No balance found on '/'\n"
                        if owner.cancel_attempted:
                            ended=case.get('terminal')
                            if ended is not None and clock.now>=owner.balance_origin+ended:return 0,"No balance found on '/'\n"
                            events.append(('status_unknown',clock.now))
                            if case.get('status_abort') and not owner.abort_requested:
                                events.append(('signal_during_status',clock.now))
                                signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
                            return 0,'Balance is running\n'
                        return 0,"No balance found on '/'\n"
                    if 'start' in argv:
                        events.append(('start',argv[-1],clock.now));states[argv[-1]]='active'
                        if case.get('fault') and argv[-1]=='clarp-runtime.service':raise OSError('synthetic runtime start')
                    return 0,''
                def unit(name):
                    events.append(('unit',name,states[name]))
                    return {'ActiveState':states[name],'MainPID':'123' if states[name]=='active' else '0','InvocationID':'private','ControlGroup':''}
                real_log=owner.log
                def log(kind,**values):
                    if case.get('fault'):
                        with patch('controller.os.fsync',side_effect=OSError('synthetic disk log')):return real_log(kind,**values)
                    return real_log(kind,**values)
                try:
                    with patch('controller.time.monotonic',clock.monotonic),patch('controller.time.sleep',clock.sleep),patch('controller.time.time',return_value=1000.),patch('controller.Owned',Child),patch.object(owner,'command',command),patch.object(owner,'unit',unit),patch.object(owner,'same_release'),patch.object(owner,'writer_census'),patch.object(owner,'log',side_effect=log):
                        rc=owner.run()
                    self.assertEqual([e[1] for e in events if e[0]=='balance'],case['passes'])
                    self.assertEqual(owner.work_deadline,initial+600)
                    origin=owner.balance_origin;self.assertIsNotNone(origin)
                    self.assertEqual(next(e[2] for e in events if e[:2]==('balance',10)),origin)
                    self.assertIn(('unit','private.timer','active'),events)
                    self.assertEqual([e[1][-1] for e in events if e[0]=='command' and 'stop' in e[1]],
                                     ['private.timer','private-listener.service','clarp.service','clarp-runtime.service'])
                    if len(case['passes'])==2:
                        self.assertLessEqual(next(e[2] for e in events if e[:2]==('balance',25))-origin,150)
                    cancel=[e for e in events if e[0]=='cancel']
                    starts=[e for e in events if e[0]=='start']
                    self.assertEqual(starts[0][1:2],('clarp-runtime.service',));self.assertEqual(starts[1][1:2],('clarp.service',))
                    if case['finish']>232:
                        self.assertEqual(len(cancel),1);self.assertLessEqual(cancel[0][1]-origin,240)
                        clean=[i for i,e in enumerate(events) if e[0]=='balance_cleanup']
                        if clean:self.assertLess(events.index(cancel[0]),clean[0])
                    else:self.assertEqual(cancel,[])
                    if case['overlap']:
                        self.assertTrue(owner.overlap_recovery);self.assertFalse(owner.balance_terminal)
                        self.assertEqual([e[1] for e in starts],['clarp-runtime.service','clarp.service'])
                        self.assertGreaterEqual(starts[0][2]-origin,420)
                        self.assertTrue(any(e[0]=='status_unknown' and e[1]-origin<420 for e in events))
                        self.assertTrue(owner.target300_missed)
                        self.assertEqual(rc,11)
                    else:
                        self.assertFalse(owner.overlap_recovery);self.assertEqual(rc,11 if case.get('status_abort') else 0)
                        if case.get('status_abort'):
                            self.assertTrue(owner.abort_requested)
                            self.assertTrue(any('status_control_abort:Refused:controller_signal_' in e for e in owner.operational_errors))
                            raw=(Path(directory)/'events.jsonl').read_text()
                            self.assertIn('Traceback (most recent call last)',raw)
                            self.assertIn('controller_signal_',raw)
                        if cancel:self.assertLessEqual(starts[0][2]-origin,300)
                finally:
                    for signum,handler in handlers.items():signal.signal(signum,handler)
        with tempfile.TemporaryDirectory() as directory:
            clock=Clock();clock.now=519
            owner=controller.Controller({'balance_timing':{'overlap_at420':True},'auxiliary_units':[],'timers':[]},Path(directory))
            owner.stopped=True;owner.balance_origin=100;owner.overlap_recovery=True;owner.balance_terminal=False
            with patch('controller.time.monotonic',clock.monotonic),patch.object(owner,'command',side_effect=AssertionError('no start at419')):
                with self.assertRaisesRegex(controller.Refused,'invalid_overlap_recovery'):owner.restore()
        # Actual command -> Owned constructor -> DispatchPermit after setup/birth.
        # No production command/process runs: only Popen/native observations are fixtures.
        for setup_age in (0,9):
            with self.subTest(cancel_setup_age=setup_age),tempfile.TemporaryDirectory() as directory:
                clock=Clock();clock.now=332.;events=[]
                payload=b'private native fixture'
                version=(1,2,len(payload),3,4,0,stat.S_IFREG|0o755)
                native={name:{'sha256':hashlib.sha256(payload).hexdigest(),'stat_version':list(version)}
                        for name in ('/usr/bin/sudo','/usr/bin/btrfs')}
                owner=controller.Controller({'native_sources':native},Path(directory))
                owner.phase='work';owner.balance_origin=100.;owner.work_deadline=700.
                class Native:
                    def stat(self):
                        return SimpleNamespace(**dict(zip(('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_uid','st_mode'),version)))
                    def read_bytes(self):return payload
                def path(value):return Native() if str(value) in native else Path(value)
                def popen(argv,**kw):
                    events.append(('popen',argv));return SimpleNamespace(pid=123456,returncode=None)
                def observe(child):
                    child.initial_uid=os.getuid();clock.sleep(setup_age)
                    return (123456,'private',123456,123456,'private-boot')
                def grant(fd,data):events.append(('grant',clock.now,data));return len(data)
                def cleanup(child):child.reap_started=True;events.append(('cleanup',clock.now))
                def wait(child,seconds):events.append(('wait',clock.now,seconds));child.reap_started=True;return 0
                with patch('controller.time.monotonic',clock.monotonic),patch('owned.Path',side_effect=path),patch('owned.subprocess.Popen',side_effect=popen),patch.object(controller.Owned,'observe',observe),patch.object(controller.Owned,'cleanup',cleanup),patch.object(controller.Owned,'wait',wait),patch('owned.os.write',side_effect=grant),patch.object(owner,'observe_balance_terminal'):
                    owner.cancel_balance()
                launched=[e[1] for e in events if e[0]=='popen']
                self.assertEqual(len(launched),1)
                self.assertEqual(launched[0][-6:],['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','cancel','/'])
                self.assertEqual(float(launched[0][7]),340.) # child exec gate receives SAME absolute240.
                grants=[e for e in events if e[0]=='grant']
                if setup_age:
                    self.assertEqual(grants,[])
                    self.assertIsNone(owner.last_gate_bracket)
                    self.assertTrue(any('cancel:CustodyUnknown' in e for e in owner.operational_errors))
                    text=(Path(directory)/'events.jsonl').read_text()
                    self.assertIn('work_permit_expired_before_exec',text)
                    self.assertIn('"gate_released": false',text)
                    self.assertTrue(any(e[0]=='cleanup' for e in events))
                else:
                    self.assertEqual(grants,[('grant',332.,b'G')])
                    self.assertEqual(owner.last_gate_bracket,(332.,332.))
                    self.assertEqual([e[2] for e in events if e[0]=='wait'],[8.])
                    self.assertEqual(owner.operational_errors,[])
                self.assertTrue(owner.cancel_attempted)
                self.assertIsNone(owner.command_deadline)
        # Same new production pre-age guard, not an inferred outage guarantee.
        w={'job_id':'p','owner':'o','generation':1,'pid':1,'boot':'b','start':'s',
           'pre_registry':{'job_id':'p','session':'o','generation':1,'worker_pid':1,'worker_start_token':'b:s','status':'running','heartbeat_at':40000}}
        validate_heartbeat_age([w],100000)
        with self.assertRaisesRegex(WriterUnknown,'pre_heartbeat_age'):validate_heartbeat_age([w],100001)

if __name__=='__main__':unittest.main()
