"""Three finite SOURCE controls. UNEXECUTED; no native/provider/DB proof.

Owner boundary: actual completion dispatcher + private receipt consumers and
unchanged R4 policy modules. Fixtures are protocol inputs, not helper execution.
No new test-only production flags, exports or service substitutions.
"""
from pathlib import Path
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


op = load('natural933_operator_control', ROOT/'ops/reship-operator.py')
n = op.n933


class NaturalZero933(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='queue933-natural-private-')
        self.addCleanup(self.temp.cleanup)
        self.program = Path(self.temp.name).resolve()
        self.logs = self.program/'logs'; self.logs.mkdir()
        (self.program/'registers').mkdir()
        self.tag = 'refactor-20990101-1'; self.host = 'private-host'; self.target = 'private-runtime'
        self.unit = dict(ActiveState='active', MainPID='41', InvocationID='b'*32, NRestarts='0')
        self.old = dict(ActiveState='active', MainPID='31', InvocationID='a'*32, NRestarts='0')
        self.native = dict(pid=41, start=51, boot='private-boot', invocation='b'*32,
                           cgroup='/user.slice/private/clarp-runtime.service')
        manager = {k:self.unit[k] for k in ('ActiveState','MainPID','InvocationID')}
        manager['ControlGroup'] = self.native['cgroup']
        kernel = {k:self.native[k] for k in ('pid','start','boot')}
        status = dict(release_id=self.target, instance='c'*32, draining=False,
                      attestation=dict(pid=41,start_ticks=51,boot_id='private-boot',
                                       cgroup=self.native['cgroup'],invocation='b'*32))
        evidence = dict(status_before=status,status_after=copy.deepcopy(status),
                        unit_before=manager,unit_after=dict(manager),native_before=kernel,
                        native_after=dict(kernel),cgroup_before=self.native['cgroup'],
                        cgroup_after=self.native['cgroup'],
                        healthy_clock=dict(wall_before=999,mono_before=99,wall_after=1000,mono_after=100))
        self.observation = op.tr.qualify_natural(evidence,self.target)
        self.row = dict(epoch=1000,mono=100,pid=61,process_start=71,boot='private-boot',
                        host_code=200,host_ready=True,runtime_available=True,runtime_id=self.target,
                        rss_bytes=100,rss_pid=61,rss_start=71,actual_Host_switch_epoch=990)
        self.guard = op.av.RuntimeAvailability(self.old)
        self.natural = n.NaturalRuntime(self.target,self.host,dict(pid=61,start=71,boot='private-boot'))

    def write(self, suffix, value):
        path = self.logs/(self.tag+'.runtime830'+suffix)
        path.write_text(json.dumps(value));path.chmod(0o600)
        return path

    def receipt_inputs(self, count):
        root = self.logs/'handover-resumes'/('b'*32);root.mkdir(parents=True)
        summary = dict(result='runtime_activation_health_pass',tag=self.tag,release_id=self.host,
                       runtime_id=self.target,runtime_invocation='b'*32,forward_restart_attempts=count,
                       rolled_back=False,interrupted_current_boot=0,
                       resume_receipts=dict(accepted=0,skipped=0,unknown=0),helper_receipt_root=str(root))
        self.write('.summary.json',summary)
        self.write('.healthy.json',dict(host_on_installed=True,runtime_on_installed=True,
                    probe=dict(release_id=self.host,runtime_release_id=self.target,runtime_available=True,health_ready=True)))
        self.write('.helper.json',dict(exit=0,stdout=json.dumps(dict(invocation='b'*32,interrupted=0,receipt_root=str(root))),stderr=''))
        self.write('.forward.interrupted.json',dict(start_epoch=1000,events=[],restart_interrupt_failures=0))
        union = dict(state='UNION_RECONCILED_METADATA',union=op.tr.continuation_union([]))
        return summary,union

    def test_natural_zero_positive_join_completion_and_ordinary_policies(self):
        # Real caller/callee task production; only the lower process constructor
        # is replaced, so a forbidden boundary cannot pass through a method fake.
        evidence=self.program/'early-native';evidence.mkdir(mode=0o700)
        binding_path=evidence/'inert-binding.json';binding_path.write_text('{}');binding_path.chmod(0o600)
        _body,binding=op.tr.bounded(binding_path)
        config=dict(evidence_root=str(evidence),_binding=binding,sha='private-source',tag=self.tag,
                    job='private-job',native_session='private-native',goal='private-goal',predicted_runtime=self.target)
        transition=op.tr.Transition(config);dispatches=[]
        class DispatchOnly:
            def __init__(child,argv,log,whole):
                dispatches.append((argv,str(log),whole))
                child.identity=dict(pid=91,start=101,boot='private-dispatch-only')
        with patch.object(op.tr,'Owned',DispatchOnly):
            transition.natural_observation('native_admission')
            task=evidence/'natural_native_admission.task.json';raw=task.read_bytes()
            self.assertEqual(json.loads(raw)['target'],self.target)
            self.assertEqual(json.loads(raw)['context'],transition.context)
            self.assertEqual(len(dispatches),1)
            self.assertEqual(dispatches[0][0],['/usr/bin/timeout','--signal=TERM','--kill-after=2s','32s','/usr/bin/python3',str(ROOT/'ops/transition933.py'),'natural',str(task)])
            self.assertEqual(dispatches[0][2],36)
            first_state=dict(transition.states['natural_native_admission'])
            first_child=transition.children['natural_native_admission'][0]
            transition.natural_observation('native_admission')
            self.assertEqual(len(dispatches),1);self.assertEqual(task.read_bytes(),raw)
            self.assertEqual(transition.states['natural_native_admission'],first_state)
            self.assertIs(transition.children['natural_native_admission'][0],first_child)
            with self.assertRaisesRegex(ValueError,'natural_boundary'):
                transition.natural_observation('arbitrary_new_boundary')
            self.assertEqual(len(dispatches),1)
            self.assertEqual(set(transition.children),{'natural_native_admission'})
        self.assertTrue(self.natural.admit(self.observation,self.unit,self.native,self.row,1001,101,op.tr.qualify_natural,self.guard))
        summary,union = self.receipt_inputs(0)
        registry = self.program/'registers/reship883-supervised-controls.json'
        registry.write_text('{"version":1,"entries":[]}');registry.chmod(0o600);before=registry.read_bytes()
        with patch.object(op,'P',self.program),patch.object(op,'L',self.logs),patch.object(op,'TAG',self.tag),patch.object(op.time,'time',return_value=1002),patch.object(op.time,'monotonic',return_value=102):
            completed = op.complete_handover(summary,self.natural,self.unit,self.native,self.row,union)
        self.assertEqual(completed['kind'],0)
        self.assertEqual(completed['natural']['state'],'NATURAL_ZERO_COMPLETED')
        self.assertEqual(completed['natural']['healthy_clock']['healthy_epoch'],1001)
        self.assertFalse(completed['natural']['planned_restart_exclusion'])
        self.assertEqual(registry.read_bytes(),before)
        self.assertFalse((self.logs/(self.tag+'.runtime830.restart.requested.json')).exists())
        self.assertFalse((self.logs/(self.tag+'.runtime830.health.jsonl')).exists())
        self.assertEqual(self.natural.availability(self.guard,self.row,self.unit,102,lambda _:None,lambda:self.unit,op.av.identity)['unavailable_seconds'],0)
        foreign=dict(self.unit,MainPID='99',InvocationID='d'*32)
        self.assertEqual(self.natural.availability(self.guard,self.row,foreign,103,lambda _:None,lambda:foreign,op.av.identity)['rollback'],'runtime_unit_identity_changed_outside_supervision')
        confirmations=[]
        def unavailable(budget):
            confirmations.append(budget)
            return dict(runtime_available=False)
        for elapsed in range(0,121,10):
            result=self.natural.availability(self.guard,dict(self.row,runtime_available=False),self.unit,2001+elapsed,unavailable,lambda:self.unit,op.av.identity)
            self.assertEqual(bool(result['rollback']),elapsed==120)
        self.assertEqual(confirmations,[20.0])
        rss=op.rss911.RSSContinuity(dict(rss_median_bytes=100));decision=None
        for elapsed in range(0,301,10):
            row=dict(self.row,epoch=1001+elapsed,mono=101+elapsed,rss_bytes=151)
            decision=n.apply_rss(self.natural,row,dict(triggers=[],unknown=[]),rss)
        self.assertIn('rss',decision['triggers']);self.assertEqual(row['rss_r2_911']['continuous_seconds'],300)
        events=[dict(at_us=1001500000,id=str(i),code='http_5xx') for i in range(16)]
        interval=dict(since_epoch=1001,until_epoch=1002,raw_records=dict(http_5xx=events,runtime_writer_giveups=[],db_locked_records=[]),legacy_counts=dict(http_5xx=16,runtime_writer_giveups=0,db_locked_records=0))
        row=dict(self.row,epoch=1002,mono=102)
        decision=n.apply_errors(op.err927,self.natural,row,dict(triggers=[],unknown=[]),[interval],dict(baseline_error_counts=dict(http_5xx=100,runtime_writer_giveups=100)),1002,102)
        self.assertIn('http_5xx',decision['triggers']);self.assertEqual(row['error_policy927']['partition']['excluded']['http_5xx'],0)
        self.assertEqual(row['error_policy927']['boot_window']['state'],'NATURAL_NO_EXPLICIT_RESTART')
        for elapsed,amount,trigger in [(2699,13,False),(2700,12,False),(2700,13,True)]:
            now=1001+elapsed;mono=101+elapsed
            events=[dict(at_us=round((now-.1)*1000000),id=str(i),code='http_5xx') for i in range(amount)]
            interval=dict(since_epoch=now-1,until_epoch=now,raw_records=dict(http_5xx=events,runtime_writer_giveups=[],db_locked_records=[]),legacy_counts=dict(http_5xx=amount,runtime_writer_giveups=0,db_locked_records=0))
            row=dict(self.row,epoch=now,mono=mono)
            decision=n.apply_errors(op.err927,self.natural,row,dict(triggers=[],unknown=[]),[interval],dict(baseline_error_counts=dict(http_5xx=4,runtime_writer_giveups=2)),now,mono)
            self.assertEqual('http_5xx' in decision['triggers'],trigger,(elapsed,amount))
            self.assertEqual(row['error_policy927']['partition']['excluded']['http_5xx'],0)
        self.assertEqual(self.natural.clock(8201,7301)['elapsed_seconds'],7200)

    def test_natural_zero_without_positive_join_is_unknown_not_admission(self):
        for fault in ('missing','foreign','stale','wrong_host'):
            with self.subTest(fault=fault):
                observed=copy.deepcopy(self.observation);native=dict(self.native);row=dict(self.row)
                if fault=='missing':observed=None
                if fault=='foreign':native['start']=99
                if fault=='stale':observed['join']['healthy_clock']['wall_after']=950;observed['join']['healthy_clock']['mono_after']=50
                if fault=='wrong_host':row['pid']=999
                self.assertFalse(self.natural.admit(observed,self.unit,native,row,1001,101,op.tr.qualify_natural,self.guard))
                result=self.natural.availability(self.guard,row,self.unit,101,lambda _:None,lambda:self.unit,op.av.identity)
                self.assertIsNone(result['rollback']);self.assertIn('unknown',result)
        summary,union=self.receipt_inputs(0)
        with patch.object(op,'P',self.program),patch.object(op,'L',self.logs),patch.object(op,'TAG',self.tag),patch.object(op.time,'time',return_value=1002),patch.object(op.time,'monotonic',return_value=102):
            with self.assertRaisesRegex(n.NaturalUnknown,'positive_join_missing'):
                op.complete_handover(summary,self.natural,self.unit,self.native,self.row,union)
        self.assertFalse((self.program/'registers/reship883-supervised-controls.json').exists())
        self.assertTrue(self.natural.admit(self.observation,self.unit,self.native,self.row,1001,101,op.tr.qualify_natural,self.guard))
        helper=self.logs/(self.tag+'.runtime830.helper.json');original_helper=helper.read_text()
        with patch.object(op,'P',self.program),patch.object(op,'L',self.logs),patch.object(op,'TAG',self.tag),patch.object(op.time,'time',return_value=1002),patch.object(op.time,'monotonic',return_value=102):
            helper.unlink()
            with self.assertRaises(FileNotFoundError):
                op.complete_handover(summary,self.natural,self.unit,self.native,self.row,union)
            helper.write_text(original_helper);helper.chmod(0o600)
            bad=json.loads(original_helper);bad['exit']=1;helper.write_text(json.dumps(bad))
            with self.assertRaisesRegex(n.NaturalUnknown,'helper_journal_accounting'):
                op.complete_handover(summary,self.natural,self.unit,self.native,self.row,union)
            helper.write_text(original_helper)
            with self.assertRaisesRegex(n.NaturalUnknown,'helper_union_binding'):
                op.complete_handover(summary,self.natural,self.unit,self.native,self.row,dict(state='UNKNOWN'))
            with self.assertRaisesRegex(n.NaturalUnknown,'incarnation_changed'):
                op.complete_handover(summary,self.natural,self.unit,dict(self.native,start=999),self.row,union)
        for count in (False,True,None,-1,2,'0'):
            with self.assertRaises(n.NaturalUnknown):n.completion_kind(dict(forward_restart_attempts=count))

    def test_actual_one_real_registry_validator_and_policy_path_retained(self):
        summary,_=self.receipt_inputs(1)
        request=dict(authority=830,attempt=1,argv=['systemctl','--user','restart','clarp-runtime.service'],epoch=1000)
        self.write('.restart.requested.json',request)
        with patch.object(op,'P',self.program),patch.object(op,'L',self.logs),patch.object(op,'TAG',self.tag):
            completed=op.complete_handover(summary)
        self.assertEqual(completed,dict(kind=1,witnesses=()))
        registry=self.program/'registers/reship883-supervised-controls.json'
        body=json.loads(registry.read_text());self.assertEqual(len(body['entries']),1)
        self.assertEqual(op.m.supervised_restart_witnesses(registry,self.logs),())
        summary['forward_restart_attempts']=0;changed=self.write('.summary.json',summary)
        body['entries'][0]['summary']['sha256']=hashlib.sha256(changed.read_bytes()).hexdigest()
        registry.write_text(json.dumps(body))
        with self.assertRaisesRegex(op.m.ObservationUnknown,'supervised_control_unproven'):
            op.m.supervised_restart_witnesses(registry,self.logs)
        unit=dict(self.unit);ctx=dict(tag=self.tag,supervisor_pid=81,supervisor_start=91,boot='private-boot',launch_epoch=990,launch_mono=90)
        request.update(ctx,monotonic=100,epoch=1000)
        healthy=dict(host_on_installed=True,runtime_on_installed=True,probe=dict(release_id=self.host,runtime_release_id=self.target,health_ready=True,runtime_available=True,t='1970-01-01T00:16:41+00:00'))
        healthrows=[dict(host=self.host,runtime=self.target,available=True,ready=True,unit_active='active',invocation='b'*32,runtime_pid=41,t='1970-01-01T00:16:41+00:00')]
        boot=op.err927.boot_window(request,healthy,healthrows,ctx,unit,1002,102,self.tag,self.host,self.target,'private-boot')
        self.assertEqual(boot['state'],'PROVEN');self.assertEqual(boot['healthy_epoch'],1001)
        rss=op.rss911.RSSContinuity(dict(rss_median_bytes=100));row=dict(self.row,mono=101,epoch=1001,rss_bytes=999)
        result=op.rss911.apply(row,dict(triggers=['rss'],unknown=[]),rss,1001,request,True,ctx)
        self.assertNotIn('rss',result['triggers']);self.assertEqual(row['rss_r2_911']['state'],'SUPPRESSED_830_BOOT')


if __name__=='__main__':
    unittest.main()
