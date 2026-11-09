"""UNEXECUTED synthetic owner-boundary control; no systemd/native child/app use.

Owns effect admission and attribution lifecycle, not native timing/manager proof.
Credible regressions: preownership stop, abort->dispatch, late final loader read.
Prior restore/STOP controls cannot reach the external Coordinator/Gate contract.
No production seam: only lower process/manager/native/clock/private-file inputs.
"""
import contextlib,copy,hashlib,importlib.util,json,os,signal,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('fixture_supervisor_protocol',ROOT/'protocol.py')
P=importlib.util.module_from_spec(spec);spec.loader.exec_module(P)

class SupervisionFocus(unittest.TestCase):
    def test_attributed_gate_abort_and_common_deadline_contract(self):
        original_read=P.read_private
        original_compile=compile
        original_present=P.present
        actual_uid=os.getuid()
        class Fixture:
            def __init__(s,case):
                s.case=case;s.temp=tempfile.TemporaryDirectory();s.root=Path(s.temp.name);s.capture=s.root/'capture';s.capture.mkdir(mode=0o700)
                s.now=101.;s.pending=set();s.creates=0;s.stops=0;s.manager_calls=0;s.loader_reads=0;s.native_reads=0;s.sleep_count=0;s.child=None;s.coordinator=None;s.gate=None;s.effects=s.root/'effect.txt'
                s.final_input_seen=False;s.boot='fixture-boot';s.inv='ab'*16;s.nonce='12'*12;s.unit='clarp-disk-maintenance-rf-releasec-20261110-'+s.nonce+'.service'
                s.native={'pid':12345,'start':'777','uid':os.getuid(),'boot':s.boot,'cgroup':'/fixture/'+s.unit,'exe':'/fixture/python','argv_sha256':'a'*64}
                s.e={'schema':'clarp.controller-effect-envelope/1','nonce':s.nonce,'unit':s.unit,'origin':100.,'admit_deadline':180.,'soft_deadline':880.,'hard_deadline':1000.,'unit_runtime_us':700000000,'unit_stop_us':120000000,'boot':s.boot,'native_files':{n:{'fixture':n} for n in ('/usr/bin/python3','/usr/bin/systemd-run','/usr/bin/systemctl','/usr/bin/busctl')}}
                binding={'schema':'clarp.disk-maintenance-binding/1','peter_decision':'decision-0795e2c8e5ebd591f6c242a8','peter_yes':True,'lead_go_receipt':'SYNTHETIC-NOT-ACTUAL-GO','pre_state':'ZERO','window_start_epoch':900.,'window_end_epoch':2000.,'pre_end_epoch':990.,'controller_evidence_path':str(s.root/'not-real-controller-evidence')}
                for key in ('binding','loader','gate','protocol','supervisor','B_final'):
                    path=s.root/(key+'.py' if key!='binding' else key+'.json')
                    if key=='binding':raw=json.dumps(binding).encode()
                    elif key=='loader':raw=('from pathlib import Path\np=Path('+repr(str(s.effects))+')\np.write_text((p.read_text() if p.exists() else "")+"X")\n').encode()
                    else:raw=b'# benign synthetic input, never operational\n'
                    path.write_bytes(raw);path.chmod(0o600);_,s.e[key]=original_read(path)
                s.ep=s.capture/'ENVELOPE.json';P.write_new(s.ep,s.e);_,s.seal=original_read(s.ep);s.e['envelope_sha256']=s.seal['sha256']
                s.m={'ActiveState':'active','SubState':'running','InvocationID':s.inv,'Description':'Clarp maintenance gate '+s.nonce,'ActiveEnterTimestampMonotonic':100500000,'Transient':True,'MainPID':12345,'ControlGroup':s.native['cgroup'],'Type':'exec','KillMode':'mixed','RuntimeMaxUSec':700000000,'TimeoutStopUSec':120000000,'ExecMainStartTimestampMonotonic':100500000}
                s.stack=contextlib.ExitStack()
            def __enter__(s):
                for obj,key,value in ((P,'PROGRAM',s.root),(P,'__file__',s.e['protocol']['path']),(P,'boot',s.read_boot),(P.os,'getpid',lambda:12345),(P.time,'monotonic',lambda:s.now),(P.time,'time',lambda:1000.),(P.time,'sleep',s.sleep),(P,'native',s.read_native),(P,'native_file',s.native_file),(P,'read_private',s.read),(P,'present',s.present),(P.subprocess,'run',s.run),(P.subprocess,'Popen',s.popen),(P.signal,'signal',lambda *a:None),(P.signal,'sigpending',lambda:s.pending),(P.signal,'pthread_sigmask',s.mask)):
                    s.stack.enter_context(mock.patch.object(obj,key,value))
                s.stack.enter_context(mock.patch.dict(P.os.environ,{'INVOCATION_ID':s.inv}))
                return s
            def __exit__(s,*args):s.stack.close();s.temp.cleanup()
            def read_boot(s):
                if s.case=='final_boot_late' and s.final_input_seen:s.now=181.
                return s.boot
            def present(s,path):
                value=original_present(path)
                if s.case=='final_abort_lstat_late' and s.final_input_seen and Path(path).name=='ABORT.json':s.now=181.
                return value
            def native_file(s,path,expected):
                # Lower installed-binary observation. This table does not certify
                # a real installed file; production source pin checks remain B/native bars.
                if s.case=='native_file_changed':raise P.Refused('fixture_native_file_changed')
                return expected
            def read_native(s,pid):
                s.native_reads+=1;n=copy.deepcopy(s.native)
                if s.case=='native_birth' and s.manager_calls:n['start']='778'
                if s.case=='native_cgroup' and s.manager_calls:n['cgroup']='/different'
                if s.case=='cancel_rebound' and s.coordinator and s.coordinator.cancel_attempted:n['start']='778'
                return n
            def read(s,path,*args,**kwargs):
                result=original_read(path,*args,**kwargs)
                if str(path)==s.e['loader']['path']:
                    s.loader_reads+=1
                    # First is envelope validation; second is the final loader
                    # read after Gate.step revalidates the envelope (third overall).
                    if s.loader_reads>=3:
                        s.final_input_seen=True
                        if s.case=='final_loader_late':s.now=181.
                        if s.case=='final_loader_abort':P.write_new(s.capture/'ABORT.json',{'synthetic':'during final loader IO'})
                return result
            def mask(s,how,signals):
                if how==signal.SIG_SETMASK and s.pending and s.coordinator:
                    s.pending.clear();s.coordinator.signal_handler(signal.SIGTERM,None)
                return set()
            def raw(s,key):
                sig={'ActiveState':'s','SubState':'s','InvocationID':'ay','Description':'s','ActiveEnterTimestampMonotonic':'t','Transient':'b','MainPID':'u','ControlGroup':'s','Type':'s','KillMode':'s','RuntimeMaxUSec':'t','TimeoutStopUSec':'t','ExecMainStartTimestampMonotonic':'t'}[key]
                value=s.m[key]
                if sig=='ay':value=[list(bytes.fromhex(value))]
                else:value=[value]
                if s.case=='wire_type' and key=='MainPID':value=['12345']
                if s.case=='wire_signature' and key=='MainPID':sig='s'
                return json.dumps({'type':sig,'data':value})
            def run(s,argv,**kw):
                if argv[0]=='/usr/bin/busctl':
                    s.manager_calls+=1;return SimpleNamespace(returncode=0,stdout=s.raw(argv[-1]),stderr='')
                if argv[:4]==['/usr/bin/systemctl','--user','stop','--no-block']:
                    s.stops+=1;return SimpleNamespace(returncode=1 if s.case=='cancel_failure' else 0,stdout='',stderr='synthetic failure' if s.case=='cancel_failure' else '')
                if argv[0]=='/usr/bin/systemctl':return SimpleNamespace(returncode=0,stdout='LoadState=loaded\n' if s.case=='collision' else 'UNKNOWN\n' if s.case=='precreate_unknown' else 'LoadState=not-found\n',stderr='')
                raise AssertionError('unexpected native command '+repr(argv))
            def popen(s,argv,**kw):
                s.creates+=1
                if s.case=='dispatch_abort':s.pending.add(signal.SIGTERM)
                if s.case!='pending_no_ready':s.gate=P.Gate(s.e,s.seal,s.capture)
                s.child=SimpleNamespace(returncode=None,poll=s.poll)
                return s.child
            def poll(s):return s.child.returncode
            def sleep(s,seconds):
                s.sleep_count+=1
                if s.case in ('soft_expired','hard_expired') and s.sleep_count==1:
                    s.now=880. if s.case=='soft_expired' else 1000.;return
                if s.case in ('soft_expired','hard_expired') and s.coordinator.abort:
                    s.child.returncode=11;return
                if s.case=='pending_no_ready':
                    s.now=181.;s.coordinator.abort_latch('synthetic_pending_abort');s.child.returncode=11;return
                if s.coordinator and s.coordinator.granted:
                    s.gate.step()
                    P.write_new(s.capture/'TARGET-RETURN.json',{'nonce':s.nonce,'envelope_sha256':s.e['envelope_sha256'],'controller_rc_candidate':0,'traceback':None})
                    s.child.returncode=7 if s.case=='client_nonzero' else 0
                elif s.coordinator and s.coordinator.abort:s.child.returncode=11
                else:s.now+=min(seconds,.1)
            def observed(s):
                s.gate=P.Gate(s.e,s.seal,s.capture);s.coordinator=P.Coordinator(s.e,s.capture);return s.coordinator.observe_ready()
            def grant_for_gate(s):
                # main_gate creates READY itself; its natural wait reaches this
                # lower scheduling response, which runs real observe+grant.
                s.coordinator=P.Coordinator(s.e,s.capture)
                def schedule(_):s.coordinator.grant(s.coordinator.observe_ready())
                s.stack.enter_context(mock.patch.object(P.time,'sleep',schedule))
            def effects_count(s):return len(s.effects.read_text()) if s.effects.exists() else 0

        def refuse(reason,call):
            with self.assertRaisesRegex(P.Refused,reason):call()

        # Real owner dispatch/signal/loop, real Gate READY/step and typed13 manager.
        for case in ('positive','abort_before','dispatch_abort','pending_no_ready','client_nonzero','wire_type','wire_signature','native_birth','native_cgroup','soft_expired','hard_expired','terminal_late','capture_failure'):
            with self.subTest(owner=case),Fixture(case) as f:
                c=f.coordinator=P.Coordinator(f.e,f.capture)
                if case=='abort_before':c.signal_handler(signal.SIGTERM,None)
                real_write=P.write_new
                if case in ('terminal_late','capture_failure'):
                    def terminal_write(path,value):
                        if Path(path).name=='MONITOR-TERMINAL.json':
                            if case=='capture_failure':raise OSError('synthetic_terminal_fsync_fault')
                            real_write(path,value);f.now=1000.;return
                        return real_write(path,value)
                    f.stack.enter_context(mock.patch.object(P,'write_new',terminal_write))
                rc=c.run(['SYNTHETIC-CLIENT-NO-NATIVE-CHILD'])
                terminal=json.loads((f.capture/'MONITOR-TERMINAL.json').read_text()) if case!='capture_failure' else {'errors':c.errors}
                if case=='positive':self.assertEqual(rc,0);self.assertTrue(c.granted);self.assertEqual(f.manager_calls,13)
                else:
                    self.assertEqual(rc,11)
                    if case in ('client_nonzero','soft_expired','hard_expired','terminal_late','capture_failure'):self.assertTrue(c.granted)
                    else:self.assertFalse(c.granted)
                    if case=='client_nonzero':self.assertEqual(terminal['client_rc'],7)
                    if case=='soft_expired':self.assertEqual(terminal['abort_reason'],'external_soft780');self.assertEqual(f.stops,1)
                    if case=='hard_expired':self.assertIn('boot_or_absolute_hard_deadline',''.join(terminal['errors']))
                    if case=='capture_failure':self.assertIn('synthetic_terminal_fsync_fault',''.join(c.errors))
                    if case=='wire_type':self.assertIn('manager_scalar_type',''.join(terminal['errors']))
                    if case=='wire_signature':self.assertIn('manager_reply_signature',''.join(terminal['errors']))
                    if case in ('native_birth','native_cgroup'):self.assertIn('new_unit_not_positively_attributed',''.join(terminal['errors']))
                self.assertEqual(f.creates,0 if case=='abort_before' else 1)
                if c.owned is None:self.assertEqual(f.stops,0)
                self.assertEqual(f.effects_count(),0) # this phase never enters loader

        # Attribution is real; malformed lower Manager/READY values cannot own.
        for key,value in (('Description','wrong'),('InvocationID','cd'*16),('MainPID',99),('ControlGroup','/wrong'),('Transient',False),('Type','simple'),('KillMode','control-group'),('RuntimeMaxUSec',700000001),('TimeoutStopUSec',120000001)):
            with self.subTest(manager_key=key),Fixture('attribution') as f:
                f.m[key]=value;f.gate=P.Gate(f.e,f.seal,f.capture);c=f.coordinator=P.Coordinator(f.e,f.capture)
                refuse('new_unit_not_positively_attributed',c.observe_ready);c.cancel_owned();self.assertIsNone(c.owned);self.assertEqual(f.stops,0);self.assertEqual(f.manager_calls,13)
        for field,value in (('nonce','34'*12),('envelope_sha256','0'*64),('boot','wrong'),('source_pins',{})):
            with self.subTest(ready_field=field),Fixture('ready') as f:
                f.gate=P.Gate(f.e,f.seal,f.capture);p=f.capture/'UNIT-READY.json';d=json.loads(p.read_text());d[field]=value;p.write_text(json.dumps(d));p.chmod(0o600)
                c=f.coordinator=P.Coordinator(f.e,f.capture);refuse('new_unit_not_positively_attributed',c.observe_ready);self.assertIsNone(c.owned);self.assertEqual(f.stops,0)

        # Real monitor entry refuses a loadable/unknown fresh name before Popen.
        for case in ('collision','precreate_unknown'):
            with self.subTest(precreate=case),Fixture(case) as f:
                bp=Path(f.e['binding']['path']);b=json.loads(bp.read_text())
                b['external_sources']={k:f.e[k] for k in ('loader','gate','protocol','supervisor')};b['external_native_files']=f.e['native_files'];bp.write_text(json.dumps(b));bp.chmod(0o600)
                f.stack.enter_context(mock.patch.object(P,'BASE',f.root));f.stack.enter_context(mock.patch.object(P,'CAPTURE',f.root/'monitor-capture'))
                uid_reads=[]
                def monitor_uid():
                    uid_reads.append(1);return 1000 if len(uid_reads)==1 else actual_uid
                f.stack.enter_context(mock.patch.object(P.os,'getuid',monitor_uid))
                refuse('unit_name_collision_or_unknown',lambda:P.main_monitor(str(bp),hashlib.sha256(bp.read_bytes()).hexdigest(),f.e['B_final']['path'],f.e['B_final']['sha256'],f.e['supervisor']['path']))
                self.assertEqual(f.creates,0);self.assertEqual(f.stops,0)
        for key,value in (('ActiveState','inactive'),('SubState','dead'),('ActiveEnterTimestampMonotonic',181000000),('ExecMainStartTimestampMonotonic',99000000)):
            with self.subTest(admission=key),Fixture('admission') as f:
                f.m[key]=value;obs=f.observed();reason='attributed_unit_not_running' if key in ('ActiveState','SubState') else 'activation_exceeds_common_admission'
                refuse(reason,lambda:f.coordinator.grant(obs));self.assertFalse(f.coordinator.granted);self.assertFalse((f.capture/'EFFECT-GRANT.json').exists())
        with Fixture('abort_before_grant') as f:
            obs=f.observed();f.coordinator.signal_handler(signal.SIGTERM,None);f.coordinator.grant(obs);self.assertFalse(f.coordinator.granted);self.assertEqual(f.stops,0)
        with Fixture('late_grant') as f:
            obs=f.observed();f.now=181.;refuse('boot_or_absolute_admission_deadline',lambda:f.coordinator.grant(obs));self.assertFalse(f.coordinator.granted)

        # One exact owned stop, never before attribution, never a second request.
        for case in ('cancel_ok','cancel_failure','cancel_rebound'):
            with self.subTest(cancel=case),Fixture(case) as f:
                f.observed();c=f.coordinator;self.assertIsNotNone(c.owned)
                if case=='cancel_failure':refuse('owned_cancel_request_unknown',c.cancel_owned)
                elif case=='cancel_rebound':refuse('owned_cancel_identity_unknown',c.cancel_owned)
                else:c.cancel_owned()
                c.cancel_owned();self.assertTrue(c.cancel_attempted);self.assertEqual(f.stops,0 if case=='cancel_rebound' else 1)

        # Real main Gate -> actual binding/envelope/file custody -> benign stdlib
        # loader marker, never app/controller code. F3 after-last-read counterexample.
        for case in ('gate_positive','final_loader_late','final_loader_abort','final_compile_late','final_boot_late','final_abort_lstat_late','binding_changed','source_changed','boot_changed','grant_abort','grant_late','grant_nonce','grant_owned'):
            with self.subTest(final_gate=case),Fixture(case) as f:
                f.grant_for_gate()
                if case=='final_compile_late':
                    def late_compile(source,filename,*a,**kw):
                        code=original_compile(source,filename,*a,**kw)
                        if filename==f.e['loader']['path']:f.now=181.
                        return code
                    f.stack.enter_context(mock.patch.object(P,'compile',late_compile,create=True))
                original_schedule=P.time.sleep
                def schedule(seconds):
                    original_schedule(seconds)
                    if case=='binding_changed':Path(f.e['binding']['path']).write_text('{}')
                    elif case=='source_changed':Path(f.e['loader']['path']).write_text('#changed')
                    elif case=='boot_changed':f.boot='different'
                    elif case=='grant_abort':P.write_new(f.capture/'ABORT.json',{'synthetic':'pending grant'})
                    elif case.startswith('grant_') and case not in ('grant_abort',):
                        p=f.capture/'EFFECT-GRANT.json';g=json.loads(p.read_text())
                        if case=='grant_late':g['mono']=181.
                        elif case=='grant_nonce':g['nonce']='34'*12
                        elif case=='grant_owned':g['owned']['invocation']='ee'*16
                        p.write_text(json.dumps(g));p.chmod(0o600)
                f.stack.enter_context(mock.patch.object(P.time,'sleep',schedule))
                if case in ('binding_changed','source_changed','boot_changed','grant_abort','grant_late','grant_nonce','grant_owned'):
                    reason={'binding_changed':'private_input_rebound','source_changed':'private_input_rebound','boot_changed':'boot_or_absolute_admission_deadline','grant_abort':'gate_ABORT_no_controller_effect','grant_late':'effect_grant_provenance_or_clock','grant_nonce':'effect_grant_provenance_or_clock','grant_owned':'effect_grant_owned_join'}[case]
                    refuse(reason,lambda:P.main_gate(str(f.ep),f.seal['sha256'],f.e['gate']['path']))
                else:
                    rc=P.main_gate(str(f.ep),f.seal['sha256'],f.e['gate']['path'])
                    self.assertEqual(rc,0 if case=='gate_positive' else 11)
                    result=json.loads((f.capture/'TARGET-RETURN.json').read_text())
                    if case=='final_loader_abort':self.assertIn('gate_final_ABORT_at_exec_commit',result['traceback'])
                    elif case in ('final_loader_late','final_compile_late','final_boot_late'):self.assertIn('boot_or_absolute_admission_deadline',result['traceback'])
                    elif case=='final_abort_lstat_late':self.assertIn('gate_exec_commit_absolute_admission_deadline',result['traceback'])
                self.assertEqual(f.effects_count(),1 if case=='gate_positive' else 0)
                self.assertGreaterEqual(f.loader_reads,1)

        with Fixture('native_file_changed') as f:
            refuse('fixture_native_file_changed',lambda:P.main_gate(str(f.ep),f.seal['sha256'],f.e['gate']['path']));self.assertEqual(f.effects_count(),0)

        # Immutable origin80/780/900 no renewal, late publication/client nonzero UNKNOWN.
        for now,reason in ((180.,'boot_or_absolute_admission_deadline'),(1000.,'boot_or_absolute_hard_deadline')):
            with self.subTest(expiry=now),Fixture('expiry') as f:
                f.now=now;refuse(reason,lambda:P.tick(f.e,now==180.))
        with Fixture('soft') as f:
            f.observed();c=f.coordinator;f.now=880.;c.abort_latch('external_soft780');c.cancel_owned();self.assertEqual(f.stops,1);self.assertFalse(c.granted)
        # Decoder invalid array and strict scalar bool/int distinction are real wire input.
        refuse('manager_invocation_type',lambda:P.decode_bus(json.dumps({'type':'ay','data':[1,2]}),'ay'))
        refuse('manager_scalar_type',lambda:P.decode_bus(json.dumps({'type':'u','data':[True]}),'u'))
        self.assertEqual(P.decode_bus(json.dumps({'type':'ay','data':list(range(16))}),'ay'),'000102030405060708090a0b0c0d0e0f')
