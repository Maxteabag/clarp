"""UNEXECUTED finite private controls. No live DB, sudo, systemctl or balance."""
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import controller as subject
from owned import Owned, CustodyUnknown, DispatchPermit

class Clock:
    def __init__(self):
        self.value = 10.
    def __call__(self):
        return self.value

class SyntheticCommands:
    """External responses only; actual Controller run/stop/pass/restore stays the subject."""
    def __init__(self):
        self.calls = []
        self.fault = None
        self.metadata = 3 * 1024**3
    def __call__(self, obj, argv, seconds=10):
        self.calls.append(tuple(argv))
        if self.fault:
            self.fault(obj, argv)
        if 'show' in argv:
            name = argv[argv.index('show') + 1]
            state = 'inactive' if obj.stopped and obj.phase == 'work' else 'active'
            return 0, ('ActiveState=%s\nMainPID=%s\nInvocationID=inv-%s\nControlGroup=\n' %
                       (state, '0' if state == 'inactive' else '100', name))
        if 'status' in argv:
            return 0, "No balance found on '/'\n"
        if 'usage' in argv:
            return 0, 'Device unallocated: %d\n' % self.metadata
        return 0, ''

class Fixture:
    def __init__(self, directory):
        self.commands = SyntheticCommands()
        self.b = {'auxiliary_units':['clarp-bookkeeping-private.service'], 'timers':['private.timer'],
                  'expected_active_invocations':{x:'inv-'+x for x in subject.MAIN+['clarp-bookkeeping-private.service']}}
        self.owner = subject.Controller(self.b, Path(directory))
        self.patches = [patch.object(subject.Controller, 'command', lambda obj, argv, seconds=10: self.commands(obj,argv,seconds)),
                        patch.object(subject.Controller, 'same_release', lambda obj: None),
                        patch.object(subject.Controller, 'writer_census', lambda obj: None)]
    def __enter__(self):
        self.handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
        self.mask=signal.pthread_sigmask(signal.SIG_BLOCK,set())
        for p in self.patches: p.start()
        return self
    def __exit__(self, *args):
        for p in reversed(self.patches): p.stop()
        for signum,handler in self.handlers.items(): signal.signal(signum,handler)
        signal.pthread_sigmask(signal.SIG_SETMASK,self.mask)
    def starts(self):
        return [x[-1] for x in self.commands.calls if 'start' in x and '/usr/bin/systemctl' in x]

class Controls(unittest.TestCase):
    def test_partial_stop_faults_do_not_balance_and_attempt_both_starts(self):
        for name in ['private.timer','clarp-bookkeeping-private.service']+subject.MAIN:
            with self.subTest(unit=name), tempfile.TemporaryDirectory() as d, Fixture(d) as f:
                fired=[]
                def fail(obj, argv):
                    if 'stop' in argv and argv[-1] == name and not fired:
                        fired.append(True); raise OSError('synthetic stop fault')
                f.commands.fault=fail
                with patch.object(subject.Controller, 'pass_balance', side_effect=AssertionError('forbidden balance')):
                    self.assertEqual(f.owner.run(),11)
                self.assertEqual(f.starts()[:2], ['clarp-runtime.service','clarp.service'])
                self.assertEqual(f.starts()[2:],f.b['auxiliary_units']+f.b['timers'])

    def test_runtime_start_and_log_fault_still_attempts_host_and_is_not_green(self):
        with tempfile.TemporaryDirectory() as d, Fixture(d) as f:
            def fail(obj, argv):
                if 'stop' in argv:
                    raise OSError('force recovery before balance')
                if 'start' in argv and argv[-1] == 'clarp-runtime.service':
                    raise OSError('runtime start fault')
            f.commands.fault=fail
            with patch('controller.os.fsync',side_effect=OSError('log fault')):
                self.assertEqual(f.owner.run(),11)
            self.assertIn('clarp.service',f.starts())
            self.assertTrue(f.owner.diagnostic_errors)
            self.assertTrue(any('runtime' in x for x in f.owner.operational_errors))

    def test_single_clock_expiry_after_census_prevents_every_balance_spawn(self):
        for usage in (10,25):
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as d:
                clock=Clock();o=subject.Controller({},Path(d));o.work_deadline=20
                def census(obj): clock.value=21
                with patch('controller.time.monotonic',clock),patch.object(subject.Controller,'writer_census',census),patch('controller.Owned',side_effect=AssertionError('forbidden spawn')):
                    with self.assertRaisesRegex(subject.Refused,'work_budget_exhausted'):
                        o.pass_balance(usage)

    def test_pass_threshold_no_third_pass_and_expiry_after_stop(self):
        for below in (False,True):
            with self.subTest(below=below),tempfile.TemporaryDirectory() as d,Fixture(d) as f:
                seen=[]
                f.commands.metadata=1 if below else 3*1024**3
                def passed(obj,usage): seen.append(usage)
                with patch.object(subject.Controller,'pass_balance',passed):
                    self.assertEqual(f.owner.run(),0)
                self.assertEqual(seen,[10,25] if below else [10])
        with tempfile.TemporaryDirectory() as d,Fixture(d) as f:
            def expired(obj):
                obj.stopped=True;obj.offline_start=time.monotonic()-601;obj.work_deadline=time.monotonic()-1;obj.phase='work'
            with patch.object(subject.Controller,'stop',expired),patch('controller.Owned',side_effect=AssertionError('forbidden spawn')):
                self.assertEqual(f.owner.run(),11)
            self.assertEqual(f.starts()[:2],['clarp-runtime.service','clarp.service'])

    def test_unknown_cancel_terminal_blocks_starts_and_no_cancel_retry(self):
        with tempfile.TemporaryDirectory() as d,Fixture(d) as f:
            def balance(obj,usage):
                obj.balance_terminal=False;raise TimeoutError('synthetic work deadline')
            def fail(obj,argv):
                if 'status' in argv and obj.stopped:
                    raise OSError('status UNKNOWN')
            f.commands.fault=fail
            with patch.object(subject.Controller,'pass_balance',balance):
                self.assertEqual(f.owner.run(),11)
            self.assertEqual(f.starts(),[])
            self.assertEqual(sum('cancel' in x for x in f.commands.calls),1)
            with self.assertRaisesRegex(subject.Refused,'cancel_already_attempted'):
                f.owner.cancel_balance()

    def test_confirmed_cancel_with_logging_fault_still_restores_and_retains_failure(self):
        with tempfile.TemporaryDirectory() as d,Fixture(d) as f:
            def balance(obj,usage):
                obj.balance_terminal=False;raise TimeoutError('synthetic work deadline')
            with patch.object(subject.Controller,'pass_balance',balance),patch('controller.os.fsync',side_effect=OSError('log IO')):
                self.assertEqual(f.owner.run(),11)
            self.assertEqual(sum('cancel' in x for x in f.commands.calls),1)
            self.assertEqual(f.starts()[:2],['clarp-runtime.service','clarp.service'])
            self.assertTrue(f.owner.diagnostic_errors)

    def test_owned_real_exit_timeout_and_no_signals_after_reap_or_ECHILD(self):
        for code,seconds,expected in [('raise SystemExit(7)',2,7),
                                      ('import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)',.2,None)]:
            with self.subTest(code=code),tempfile.TemporaryFile() as out:
                child=Owned([sys.executable,'-c',code],out,os.environ.copy())
                real_killpg=os.killpg
                def checked(group,sig):
                    self.assertEqual(group,child.p.pid)
                    self.assertFalse(child.reap_started)
                    self.assertEqual(child.observe(),child.identity)
                    return real_killpg(group,sig)
                try:
                    with patch('owned.os.killpg',side_effect=checked):
                        if expected is None:
                            with self.assertRaises(TimeoutError): child.wait(seconds)
                        else:
                            self.assertEqual(child.wait(seconds),expected)
                    with patch('owned.os.killpg',side_effect=AssertionError('forbidden postreap signal')):
                        with self.assertRaises(CustodyUnknown):child.signal_group(signal.SIGTERM)
                finally:
                    if not child.reap_started: child.cleanup()
        with tempfile.TemporaryFile() as out:
            child=Owned([sys.executable,'-c','pass'],out,os.environ.copy())
            self.assertTrue(child.terminal_before(time.monotonic()+3))
            pid,status=os.waitpid(child.p.pid,0) # Private fixture deliberately removes reservation.
            child.p.returncode=os.waitstatus_to_exitcode(status)
            with patch('owned.os.killpg',side_effect=AssertionError('forbidden ECHILD signal')):
                with self.assertRaises(CustodyUnknown):child.signal_group(signal.SIGTERM)

class BindingAndWriterControls(unittest.TestCase):
    def test_actual_main_refuses_approval_stale_inventory_and_self_cgroup_before_controller(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);share=root/'share';release=share/'releases'/'private';release.mkdir(parents=True)
            for name in ('RUNTIME_RELEASE_ID','INSTALL_OK','RUNTIME_READY'): (release/name).write_text('private')
            base={'schema':'clarp.disk-maintenance-binding/1',
                  'peter_decision':'decision-0795e2c8e5ebd591f6c242a8','peter_yes':True,'lead_go_receipt':'synthetic',
                  'pre_state':'ZERO','writer_inventory_reconciled':True,'quiescence_receipt':'synthetic',
                  'window_start_epoch':1.,'window_end_epoch':200.,'pre_end_epoch':99.,
                  'auxiliary_units':[],'timers':[], 'expected_active_invocations':{x:'synthetic' for x in subject.MAIN},
                  'current_real':str(release),'release_files':{n:'synthetic' for n in ('RUNTIME_RELEASE_ID','INSTALL_OK','RUNTIME_READY')}}
            cases=[({'peter_yes':False},'/safe'),({'lead_go_receipt':''},'/safe'),
                   ({'pre_state':'UNKNOWN'},'/safe'),({'pre_end_epoch':-1000},'/safe'),
                   ({'expected_active_invocations':{}},'/safe'),({},'/clarp.service'),
                   ({'auxiliary_units':['private.service'],'expected_active_invocations':{x:'synthetic' for x in subject.MAIN+['private.service']}},'/private.service')]
            original=Path.read_text
            for index,(delta,group) in enumerate(cases):
                with self.subTest(delta=delta,group=group):
                    binding=dict(base,**delta);bp=root/'binding.json';raw=json.dumps(binding).encode();bp.write_bytes(raw)
                    def text(path,*a,**k):
                        return '0::'+group if str(path)=='/proc/self/cgroup' else original(path,*a,**k)
                    with patch.object(subject,'SHARE',share),patch('controller.time.time',return_value=100.),patch.object(Path,'read_text',text),patch('controller.sys.argv',['controller',str(bp),hashlib.sha256(raw).hexdigest(),str(root/('out'+str(index)))]),patch.object(subject.Controller,'run',side_effect=AssertionError('forbidden controller run')):
                        with self.assertRaises(subject.Refused): subject.main()
                    self.assertFalse((root/('out'+str(index))).exists())

    def test_actual_writer_census_refuses_named_holder_and_permission_unknown(self):
        from types import SimpleNamespace
        real_path=Path
        class FDs:
            def iterdir(self): return iter([real_path('/private-fixture-fd')])
        class Proc:
            name=str(os.getpid()+100000)
            def stat(self): return SimpleNamespace(st_uid=os.getuid())
            def __truediv__(self,name): return FDs()
        class ProcRoot:
            def iterdir(self): return iter([Proc()])
        def paths(value): return ProcRoot() if value=='/proc' else real_path(value)
        with tempfile.TemporaryDirectory() as d:
            owner=subject.Controller({},real_path(d))
            with patch.object(subject,'Path',side_effect=paths),patch('controller.os.readlink',return_value=str(subject.SHARE/'state.sqlite')):
                with self.assertRaisesRegex(subject.Refused,'writer_still_open'): owner.writer_census()
            with patch.object(subject,'Path',side_effect=paths),patch('controller.os.readlink',side_effect=PermissionError('private fixture')):
                with self.assertRaisesRegex(subject.Refused,'writer_census_permission_UNKNOWN'): owner.writer_census()

class FollowupControls(unittest.TestCase):
    def test_real_gate_restores_child_mask_and_normal_TERM_is_not_forced_KILL(self):
        with tempfile.TemporaryFile() as out:
            code="import signal,time,json;print(json.dumps([int(x) for x in signal.pthread_sigmask(signal.SIG_BLOCK,set())]),flush=True);time.sleep(30)"
            child=Owned([sys.executable,'-c',code],out,os.environ.copy())
            real=os.killpg
            def guarded(group,sig):
                self.assertEqual(group,child.p.pid)
                self.assertFalse(child.reap_started)
                self.assertEqual(child.observe(),child.identity)
                return real(group,sig)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    out.seek(0);line=out.readline()
                    if line:break
                    time.sleep(.025)
                self.assertTrue(line,'private child mask receipt missing')
                mask=json.loads(line)
                self.assertNotIn(int(signal.SIGTERM),mask)
                self.assertNotIn(int(signal.SIGINT),mask)
                with patch('owned.os.killpg',side_effect=guarded):
                    with self.assertRaises(TimeoutError):child.wait(.1)
                self.assertEqual(child.p.returncode,-int(signal.SIGTERM))
            finally:
                if not child.reap_started: child.cleanup()

    def test_actual_observe_credential_transition_requires_exact_gated_lineage(self):
        from types import SimpleNamespace
        with tempfile.TemporaryFile() as out:
            child=Owned([sys.executable,'-c','import time;time.sleep(30)'],out,os.environ.copy())
            original=Path.stat
            def root_stat(path,*a,**kw):
                if str(path)=='/proc/'+str(child.p.pid):return SimpleNamespace(st_uid=0)
                return original(path,*a,**kw)
            try:
                with patch.object(Path,'stat',root_stat):
                    with self.assertRaises(CustodyUnknown):child.observe()
                    child.privileged=True;child.source_bindings=[('SYNTHETIC_NATIVE_QUALIFICATION_NOT_REAL_SUDO',)]
                    self.assertEqual(child.observe(),child.identity)
                    with patch('owned.os.waitid',side_effect=ChildProcessError('fixture ECHILD')),patch('owned.os.killpg',side_effect=AssertionError('forbidden unreserved root signal')):
                        with self.assertRaises(CustodyUnknown):child.signal_group(signal.SIGTERM)
            finally:
                child.privileged=False;child.source_bindings=[]
                child.cleanup()
        for argv in ([ '/usr/bin/sudo','-n','/bin/sh'],
                     ['/usr/bin/sudo','-n','/usr/bin/btrfs','balance','status','/']):
            with self.subTest(argv=argv),tempfile.TemporaryFile() as out,patch('owned.subprocess.Popen',side_effect=AssertionError('forbidden native spawn')):
                with self.assertRaises(CustodyUnknown):Owned(argv,out,os.environ.copy())

    def test_registered_abort_during_actual_work_log_prevents_25_and_restores(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d,Fixture(d) as f:
            f.commands.metadata=1
            launched=[]
            def synthetic_child(argv,*args,**kwargs):
                launched.append(argv)
                return SimpleNamespace(wait=lambda seconds:0,reap_started=True,cleanup=lambda:None)
            original=subject.Controller.log
            def log(obj,kind,**values):
                if kind=='balance_pass':
                    def deliver(fd):signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
                    with patch('controller.os.fsync',side_effect=deliver):return original(obj,kind,**values)
                return original(obj,kind,**values)
            with patch('controller.Owned',side_effect=synthetic_child),patch.object(subject.Controller,'log',log):
                self.assertEqual(f.owner.run(),11)
            self.assertTrue(f.owner.abort_requested)
            self.assertEqual(len(launched),1)
            self.assertIn('-dusage=10',launched[0])
            self.assertEqual(f.starts()[:2],['clarp-runtime.service','clarp.service'])
            self.assertTrue(f.owner.diagnostic_errors)

class GateAndCleanupControls(unittest.TestCase):
    def test_setup_ages_original_permit_and_private_effect_never_executes(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryFile() as out:
            marker=Path(directory)/'effect'
            deadline=time.monotonic()+.2
            permit=DispatchPermit(deadline,SimpleNamespace(abort_requested=False))
            real_observe=Owned.observe
            real_killpg=os.killpg
            births=[]
            def aged(child):
                result=real_observe(child)
                if child.identity is None:
                    births.append(child)
                    time.sleep(.35) # Controlled setup cost crosses the SAME absolute permit.
                return result
            def guarded(group,sig):
                self.assertTrue(births)
                child=births[0]
                self.assertEqual(group,child.p.pid)
                self.assertFalse(child.reap_started)
                self.assertEqual(real_observe(child),child.identity)
                return real_killpg(group,sig)
            code='from pathlib import Path;Path('+repr(str(marker))+').write_text("executed")'
            with patch.object(Owned,'observe',aged),patch('owned.os.killpg',side_effect=guarded):
                with self.assertRaises(CustodyUnknown) as caught:
                    Owned([sys.executable,'-c',code],out,os.environ.copy(),work_permit=permit)
            self.assertFalse(marker.exists())
            self.assertFalse(caught.exception.child_evidence['gate_released'])
            self.assertIn('work_permit_expired_before_exec',caught.exception.child_evidence['primary'])
            self.assertEqual(permit.deadline,deadline)
            self.assertIsNotNone(caught.exception.child_evidence['birth'])

    def test_gate_own_expiry_check_refuses_lower_exec_effect(self):
        import child_exec
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'effect'
            def forbidden(*args):marker.write_text('executed')
            with patch('child_exec.sys.argv',['gate','123','','0',sys.executable,'-c','pass']),patch('child_exec.select.select',return_value=([123],[],[])),patch('child_exec.os.read',return_value=b'G'),patch('child_exec.os.close'),patch('child_exec.signal.pthread_sigmask'),patch('child_exec.os.execvpe',side_effect=forbidden) as execute:
                with self.assertRaises(SystemExit) as caught: child_exec.main()
            self.assertEqual(caught.exception.code,126)
            execute.assert_not_called()
            self.assertFalse(marker.exists())

    def test_each_close_failure_preserves_primary_both_attempts_and_parent_mask(self):
        original_close=os.close
        original_mask=signal.pthread_sigmask
        for faults in ({0},{1},{0,1}):
            for mask_fault in (False,True):
                with self.subTest(close=faults,mask_fault=mask_fault),tempfile.TemporaryFile() as out:
                    before=original_mask(signal.SIG_BLOCK,set())
                    closed=[];mask_restores=[]
                    def close(fd):
                        index=len(closed);closed.append(fd)
                        original_close(fd) # Adapter: private actual close then injected outcome fault.
                        if index in faults:raise OSError('synthetic close '+str(index))
                    def masks(how,value):
                        result=original_mask(how,value)
                        if how==signal.SIG_SETMASK:
                            mask_restores.append(set(value))
                            if mask_fault:raise OSError('synthetic mask restore outcome')
                        return result
                    try:
                        with patch('owned.subprocess.Popen',side_effect=RuntimeError('primary spawn fault')),patch('owned.os.close',side_effect=close),patch('owned.signal.pthread_sigmask',side_effect=masks),patch('owned.os.killpg',side_effect=AssertionError('forbidden unbound signal')):
                            with self.assertRaises(CustodyUnknown) as caught:
                                Owned([sys.executable,'-c','pass'],out,os.environ.copy())
                        self.assertEqual(len(closed),2)
                        self.assertEqual(len(set(closed)),2)
                        self.assertEqual(mask_restores,[before])
                        self.assertEqual(original_mask(signal.SIG_BLOCK,set()),before)
                        evidence=caught.exception.child_evidence
                        self.assertIn('primary spawn fault',evidence['primary'])
                        self.assertEqual(len(evidence['cleanup_errors']),len(faults)+int(mask_fault))
                    finally:
                        original_mask(signal.SIG_SETMASK,before)

if __name__=='__main__': unittest.main()
