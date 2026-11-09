#!/usr/bin/env python3
"""Lead892 conditional single operating procedure; frozen883 observation/math, rc5/r3 tools unchanged."""
from pathlib import Path
import datetime,fcntl,hashlib,importlib.util,json,os,signal,subprocess,sys,time,sqlite3,traceback
P=Path('/var/tmp/clarp-refactor-program');L=P/'logs/releases';TAG=None;ROOT=Path(__file__).resolve().parent;PREV=None;SHARE=Path('/home/peter/.local/share/clarp')
spec=importlib.util.spec_from_file_location('accepted888',ROOT/'eligibility-watch.py');obs=importlib.util.module_from_spec(spec);spec.loader.exec_module(obs);m=obs.metrics
avspec=importlib.util.spec_from_file_location('runtime_guard892',ROOT/'runtime-availability.py');av=importlib.util.module_from_spec(avspec);avspec.loader.exec_module(av)
bcspec=importlib.util.spec_from_file_location('baseline904',ROOT/'baseline-reuse904.py');bc=importlib.util.module_from_spec(bcspec);bcspec.loader.exec_module(bc)
rssspec=importlib.util.spec_from_file_location('rss911',ROOT/'rss-continuity911.py');rss911=importlib.util.module_from_spec(rssspec);rssspec.loader.exec_module(rss911)
err927=obs.load('error_window927',ROOT/'error-window927.py')
bridge_spec=importlib.util.spec_from_file_location('accepted_runtime_bridge931',ROOT.parent/'support/runtime_bridge931.py')
bridge=importlib.util.module_from_spec(bridge_spec);sys.modules[bridge_spec.name]=bridge;bridge_spec.loader.exec_module(bridge)
tspec=importlib.util.spec_from_file_location('transition933',ROOT/'transition933.py');tr=importlib.util.module_from_spec(tspec);sys.modules[tspec.name]=tr;tspec.loader.exec_module(tr)
n933=obs.load('natural_runtime933',ROOT/'natural-runtime933.py')
CAPSULE=None
transition=None
expected_probe_runtime=None
runtime_context=None
def confirm_runtime(budget):
 if current()!=target_id:raise RuntimeError('direct_probe_Host_ownership_changed')
 result=av.direct_runtime_probe(bridge,'/home/peter/.cache/clarp/runtime.sock',budget,expected_probe_runtime)
 if current()!=target_id:raise RuntimeError('direct_probe_Host_ownership_changed')
 return result

def save(s,data,exclusive=False):
 path=L/(TAG+s)
 if exclusive:
  with path.open('x') as f:json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
 else:
  tmp=path.with_suffix(path.suffix+'.pending')
  with tmp.open('w') as f:json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)

def run(args,timeout=40,env=None):return subprocess.run(args,capture_output=True,text=True,timeout=timeout,env=env)

def notice(kind,data):
 path=L/(TAG+'.892notice.'+kind+'.json')
 if path.exists():return
 save('.892notice.'+kind+'.json',{'state':'attempted','kind':kind,'numbers':data,'epoch':time.time()},True)
 r=run(['clarp-admin','prompt','--to','hobbles-18b0','--from','rf-releasec-29c5','--text','892 '+kind+': '+json.dumps(data)],30)
 ans=json.loads(r.stdout) if r.returncode==0 else {}
 if ans.get('ok') is not True or ans.get('session')!='hobbles-18b0' or not ans.get('trace_id'):raise RuntimeError('notice_unknown_no_resend')
 save('.892notice.'+kind+'.accepted.json',{'trace_id':ans['trace_id'],'epoch':time.time()},True)

def status():
 r=run([str(P/'bin/release-check'),'status'],35)
 if r.returncode:raise RuntimeError('status_unknown')
 return json.loads(r.stdout)

def props(name):
 r=run(['systemctl','--user','show',name,'--property=MainPID,ActiveState,NRestarts,InvocationID'],15)
 if r.returncode:raise RuntimeError('unit_unknown')
 return dict(x.split('=',1) for x in r.stdout.splitlines() if '=' in x)

def current():return (SHARE/'current/DEPLOYED_RELEASE_ID').read_text().strip()

def stop_owned(child):
 if child and child.poll() is None:
  os.killpg(child.pid,signal.SIGTERM)
  try:child.wait(timeout=15)
  except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)

def rollback(trigger,ship_child=None,expected_runtime_identity=None):
 save('.rollback892.requested.json',{'authority':892,'policy':883,'trigger':trigger,'epoch':time.time(),'attempt':1},True)
 notice('ROLLBACK_TRIGGER',{'trigger':trigger,'tag':TAG})
 stop_owned(ship_child)
 # Retire only this exact release observer, never a foreign service.
 own='clarp-refactor-observe-'+TAG.removeprefix('refactor-')
 r=run(['systemctl','--user','list-units','--all','--plain','--no-legend','clarp-refactor-observe-*','clarp-refactor-follow-*'],15)
 if r.returncode:raise RuntimeError('observer_inventory_unknown')
 for line in r.stdout.splitlines():
  f=line.split()
  if len(f)>2 and f[2] in ('active','activating','deactivating','reloading'):
   if f[0]!=own+'.service':raise RuntimeError('foreign_observer_no_rollback_effect')
   run(['systemctl','--user','stop',own],45)
 with (P/'release.lock').open('a') as lock:
  until=time.monotonic()+30
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>=until:raise RuntimeError('rollback_lock_unknown')
    time.sleep(1)
  rid=current()
  if rid!=target_id:raise RuntimeError('rollback_host_ownership_changed')
  if expected_runtime_identity is not None and av.identity(props('clarp-runtime.service'))!=expected_runtime_identity:
   raise RuntimeError('runtime_identity_changed_before_rollback_no_effect')
  prev=SHARE/'releases'/PREV;py=(prev/'SERVICE_PYTHON').read_text().strip();rt=(prev/'RUNTIME_RELEASE_ID').read_text().strip();before=props('clarp-runtime.service');epoch=time.time()
  env=dict(os.environ,CLARP_SHARE_DIR=str(SHARE),CLARP_CONFIG_DIR='/home/peter/.config/clarp',CLARP_CACHE_DIR='/home/peter/.cache/clarp',CLAUDE_PWA_CONFIG='/home/peter/.config/clarp/config.toml',CLAUDE_PWA_DB=str(SHARE/'state.sqlite'),CLARP_CODE_ROOT=str(prev))
  save('.rollback892.command-requested.json',{'previous':PREV,'epoch':epoch,'authority':892,'attempt':1},True)
  r=run([py,str(prev/'bin/clarp-admin.py'),'rollback',PREV],120,env);save('.rollback892.command.json',{'returncode':r.returncode,'stdout_present':bool(r.stdout),'stderr_present':bool(r.stderr)})
  if r.returncode or current()!=PREV:raise RuntimeError('rollback_unknown_no_retry')
  s=status();restart=not(s['probe'].get('runtime_available') is True and s['probe'].get('runtime_release_id')==rt)
  if restart:
   save('.rollback892.runtime-requested.json',{'epoch':time.time(),'authority':830,'attempt':1},True)
   r=run(['systemctl','--user','restart','clarp-runtime.service'],60)
  deadline=time.monotonic()+300
  while time.monotonic()<deadline:
   try:
    s=status()
    if s.get('host_on_installed') and s.get('runtime_on_installed') and s['probe'].get('health_ready') is True and s['probe'].get('runtime_available') is True:break
   except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):pass
   time.sleep(3)
  else:raise RuntimeError('previous_health_unproven')
 resumes=None
 if restart:
  hu=props('clarp.service');ru=props('clarp-runtime.service')
  helper=['python3',str(P/'release-tooling/v8n2-supervised-r3/bin/resume-after-handover.py'),'--config','/home/peter/.config/clarp/config.toml','--share',str(SHARE),'--lock',str(P/'release.lock'),'--receipt-root',str(L/'handover-resumes'),'--host',PREV,'--runtime',rt,'--from','rf-releasec-29c5','--host-pid',hu['MainPID'],'--host-restarts',hu['NRestarts'],'--supervised-restart','--expected-runtime-invocation',ru['InvocationID'],'--expected-runtime-pid',ru['MainPID']]
  r=run(helper,300);save('.rollback892.helper.json',{'exit':r.returncode,'stdout':r.stdout[:4096],'stderr':r.stderr[:1024]})
  if r.returncode:raise RuntimeError('rollback_resumes_need_union_reconciliation')
  resumes=json.loads(r.stdout)
 result={'result':'ROLLED_BACK_892_883','trigger':trigger,'release_id':PREV,'runtime_id':rt,'rollback_runtime_restarts':int(restart),'resumes':resumes,'failed_forward_union_audit_required':restart,'at':datetime.datetime.now().astimezone().isoformat()}
 save('.892.summary.json',result);notice('ROLLED_BACK_BOTH_HEALTHY',result)
 raise SystemExit(10)


def register_actual_one(summary):
 if n933.completion_kind(summary)!=1:raise n933.NaturalUnknown('planned_registry_requires_exact_one')
 # Register actual completed803 request/summary/journal; frozen adapter validates all joins.
 entries=[]
 for key,suffix in [('request','.restart.requested.json'),('summary','.summary.json'),('events','.forward.interrupted.json')]:
  file=L/(TAG+'.runtime830'+suffix);entries.append((key,{'path':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}))
 control=P/'registers/reship883-supervised-controls.json'
 with (P/'release.lock').open('a') as registry_lock:
  fcntl.flock(registry_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  m.supervised_restart_witnesses(control,L)  # preserve/revalidate every prior receipt
  body=json.loads(control.read_text()) if control.exists() else {'version':1,'entries':[]}
  if len(body['entries'])>=16:raise RuntimeError('supervised_registry_full')
  body['entries'].append(dict(entries));pending=control.with_suffix('.pending')
  with pending.open('x') as out:json.dump(body,out,indent=2);out.flush();os.fsync(out.fileno())
  os.replace(pending,control)
  directory=os.open(control.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(directory);os.close(directory)
 return m.supervised_restart_witnesses(control,L)

def complete_handover(summary,natural=None,unit=None,native=None,row=None,union=None):
 kind=n933.completion_kind(summary)
 if kind==1:
  return dict(kind=1,witnesses=register_actual_one(summary))
 return dict(kind=0,natural=natural.finish_zero(summary,unit,native,row,time.time(),time.monotonic(),L,TAG,tr.read_json,union))

def choose_baseline(now=None):
 # Policy functions unchanged; only actual newly-admitted recorder provenance is
 # future-bound. No stale Hotseat recorder/old Host window relabelling fallback.
 rec=CAPSULE['recorder']
 recorder_root=Path(rec['root']);source=rec['source'];recorder_manifest=rec['manifest']
 if hashlib.sha256((recorder_root/'ops/release-metrics.py').read_bytes()).hexdigest()!=hashlib.sha256((ROOT/'release-metrics.py').read_bytes()).hexdigest():raise bc.BaselineRefused('R3_eligibility_policy_changed')
 provenance=bc.admitted_watch(P,rec['job'],rec['prefix'],recorder_root,source,recorder_manifest)
 if provenance.get('source')!=source or provenance.get('manifest')!=recorder_manifest:raise bc.BaselineRefused('fresh_R3_source_identity')
 choice=bc.window(bc.read_rows(provenance['sample_path']),m,time.time(),reuse=False)
 choice['mode']='AS_IS_ACTUAL_933_ADMITTED_RECORDER';choice['provenance']=provenance
 return choice

def invoke_host_stage(plan,shiplog):
 # Blocking notices/setup precede the ONE final eligibility/freshness decision.
 notice('HOST_INSTALL_PENDING_BASELINE',{'tag':TAG,'target':plan['sha'],'deployment_effects':0})
 if current()!=PREV:raise RuntimeError('previous_Host_changed_before_effect')
 choice=choose_baseline()
 # The recorder is not the Host: bind the measured incarnation to the live unit.
 if tuple(choice['identity'])!=bc.native_host_identity():
  raise bc.BaselineRefused('baseline_Host_incarnation_changed')
 # PRE executes once, then the final native/age decision is adjacent to real
 # unchanged rc5 Popen. PRE refuses terminally, never the baseline retry loop.
 def native_at_effect():
  if current()!=PREV:raise tr.Refused('previous_Host_changed_after_PRE')
  ds=Path(CAPSULE['db']['path']).stat()
  if (ds.st_dev,ds.st_ino)!=(CAPSULE['db']['dev'],CAPSULE['db']['inode']):raise tr.Refused('DB_incarnation_changed_after_PRE')
  return bc.native_host_identity()
 try:
  child,invoked=transition.before_host(choice,native_at_effect,
    [str(P/'release-tooling/v8n2-814-rc5/bin/release-deploy'),'ship',TAG,'--from=rf-releasec-29c5'],shiplog)
 except Exception:
  shiplog.close();raise
 # Fixed once at invocation. Installer duration never creates a second gate.
 save('.baseline904-before.json',choice,True)
 save('.baseline892.json',dict(choice['baseline'],authority=[892,895,904,905,906],
      provenance=choice['provenance'],mode=choice['mode'],no_relabel_no_backfill=True),True)
 save('.ship-child.json',{'pid':child.pid,'epoch':time.time(),'argv_tool':'approvedrc5','baseline_mode':choice['mode'],'invocation_boundary_epoch':invoked,
      'window_age_at_invocation_seconds':invoked-choice['baseline']['end_epoch']},True)
 notice('HOST_INSTALL_INVOKED',{'tag':TAG,'sha':plan['sha'],'baseline':choice['baseline'],'provenance':choice['provenance']})
 return choice,child,shiplog

def runtime_stage():
 global runtime_context
 runtime_log=(L/(TAG+'.runtime830.job.log')).open('x')
 launched_mono=time.monotonic();launched_epoch=time.time()
 try:child=subprocess.Popen(['python3',str(ROOT/'supervised-runtime.py')],stdout=runtime_log,stderr=subprocess.STDOUT,start_new_session=True)
 except Exception:runtime_log.close();raise
 runtime_context={'tag':TAG,'supervisor_pid':child.pid,'supervisor_start':int(Path('/proc/'+str(child.pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]),'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'launch_mono':launched_mono,'launch_epoch':launched_epoch}
 save('.runtime-operator.json',dict(runtime_context,pid=child.pid,epoch=time.time(),authority=[895,892,830,803],forward_attempts_max=1),True)
 notice('SUPERVISED_RUNTIME_ACTIVATION_STARTED',{'tag':TAG,'boot_budget_seconds':300,'BOTH_rollback':True})
 return child,runtime_log

def main():
 global target_id,expected_probe_runtime,TAG,PREV,CAPSULE,transition
 CAPSULE=tr.load_attempt(ROOT.parent);TAG=CAPSULE['tag'];PREV=CAPSULE['previous_host'];transition=tr.Transition(CAPSULE)
 obs.proof_ready(P/'registers/queue933-natural0-operator-source-selection-r1.json')
 proof=json.loads((P/'registers/queue933-natural0-operator-source-selection-r1.json').read_text())
 assert hashlib.sha256((ROOT.parent/'SHA256SUMS').read_bytes()).hexdigest()==proof['manifest']
 assert (ROOT.parent/'SOURCE_COMMIT').read_text().strip()==proof['source']
 for line in (ROOT.parent/'SHA256SUMS').read_text().splitlines():
  digest,name=line.split(maxsplit=1);assert hashlib.sha256((ROOT.parent/name.lstrip('*')).read_bytes()).hexdigest()==digest
 plan=json.loads((P/'release-src'/(TAG+'.plan.json')).read_text());assert plan['sha']==CAPSULE['sha'] and plan['live_release']==PREV and plan['predicted']['runtime_release_id']==CAPSULE['predicted_runtime']
 assert run(['git','ls-remote','origin','refs/heads/main']).stdout.split()[0]==plan['sha']
 for name in ('v8n2-814-rc5','v8n2-supervised-r3'):
  root=P/'release-tooling'/name
  for line in (root/'SHA256SUMS').read_text().splitlines():
   h,f=line.split(maxsplit=1);assert hashlib.sha256((root/f.lstrip('*')).read_bytes()).hexdigest()==h
 s=status();assert s['host_on_installed'] and s['probe']['health_ready'] is True and current()==PREV
 deadline=CAPSULE['deadline_epoch']
 shiplog=(L/(TAG+'.ship.log')).open('x')
 while True:
  if time.time()>=deadline:
   shiplog.close();raise bc.BaselineRefused('eligibility_deadline_no_deploy')
  expected_probe_runtime=plan['live_runtime_release_id']
  runtime_guard=av.RuntimeAvailability(props('clarp-runtime.service'))
  try:initial_choice,ship,shiplog=invoke_host_stage(plan,shiplog);break
  except bc.BaselineRefused:
   print(json.dumps({'event':'WAITING_QUALIFIED_ADMITTED_BASELINE','deployment_effects':0}),flush=True)
   time.sleep(min(10,max(0,deadline-time.time())))
 baseline=initial_choice['baseline'];rss_guard=rss911.RSSContinuity(baseline)
 expected_boot_bound=False;timeout_dump_attempted=False;rpc_intervals=[]
 telemetry=m.readonly('/home/peter/.local/share/clarp/telemetry.sqlite');state=m.readonly('/home/peter/.local/share/clarp/state.sqlite');reader=m.TurnReader(state,int(time.time()*1000))
 post=[];error_windows=[];since=time.time();turn_since=since
 natural_guard=None;runtime_completion_kind=None;runtime_terminal_saved=False
 target_id=None;switch_epoch=None;switch_mono=None;runtime_child=None;runtime_log=None;runtime_started=False;runtime_done=False;host_pass=False;reported30=False;fault_episode=False;error_fault_episode=False;last_ready_bad=None;http_down=None
 class DeferredReader:
  def poll(self,a,b):return {'failed_turn_counts':{'unknown':1},'turn_state_rows_observed':0,'scope':'planned_boot_pending_completed_witness'}
 while True:
  begin=time.monotonic();now=time.time();rid=current()
  transition.poll()  # bounded small envelopes only; R4 siblings below always run
  if target_id is None and rid!=PREV:
   assert rid.startswith(CAPSULE['sha'][:8]+'-')
   switched=status()
   if switched.get('probe',{}).get('release_id')!=rid:
    time.sleep(1);continue
   upper=time.time();target_id=rid;switch_epoch=upper;switch_mono=time.monotonic()
   transition.observed_host(rid,tr.host_native(props('clarp.service')))
   natural_guard=n933.NaturalRuntime(CAPSULE['predicted_runtime'],rid,transition.states['Host_observation']['native'])
   q=switched.get('probe',{})
   if q.get('runtime_available') is True and q.get('runtime_release_id')==CAPSULE['predicted_runtime']:
    transition.natural_observation('first_Host')
   notice('HOST_SWITCH',{'tag':TAG,'release_id':rid,'baseline':baseline,
      'baseline_validated_at_invocation_only':True,
      'baseline_provenance':initial_choice['provenance'],'runtime_restarts':0})
  if target_id and rid!=target_id:raise RuntimeError('Host_replaced_or_rc5_recovered_read_receipts')
  deferred=runtime_started and not runtime_done and runtime_completion_kind!=0
  row=obs.collect(post,DeferredReader() if deferred else reader,telemetry,since,now)
  if deferred: row['supervised_classification_pending']=True
  if not deferred:
   turn_since=now
   if runtime_done or runtime_completion_kind==0:
    for prior in post:
     if prior.get('supervised_classification_pending'):
      prior['error_counts']['genuine_failed_turns']=0
      prior['supervised_classification_pending']=False
      prior['genuine_turn_count_scope']=('natural_no_new_exclusion_actual_catchup_counted_in_current_row' if runtime_completion_kind==0 else 'completed_witness_catchup_counted_in_current_row')
  if not row.get('unknown') or set(row.get('unknown',{}))<= {'failed_turn_classifier'}:
   pass
  if target_id:
   post.append(row);elapsed=time.monotonic()-switch_mono
   win=[x for x in post if now-x['epoch']<=1800]
   counts={k:sum(x['error_counts'][k] for x in win) if all(type(x['error_counts'].get(k)) is int for x in win) else None for k in m.FLOORS}
   row['post_error_counts']=counts;decision=m.recalibrated_triggers(row,baseline,elapsed)
   try:
    error_interval=err927.capture(telemetry,run,since,now)
    error_interval['legacy_counts']={k:row['error_counts'].get(k) for k in (err927.REPORT_ONLY,)+err927.CATEGORIES}
   except Exception as error:
    error_interval={'since_epoch':since,'until_epoch':now,'raw_records':None,'capture_unknown':type(error).__name__}
   error_windows.append(error_interval)
   error_windows=[x for x in error_windows if x['until_epoch']>=now-1800]
   row['actual_Host_switch_epoch']=switch_epoch
   try:error_unit=props('clarp-runtime.service')
   except Exception as error:error_unit={'observation_unknown':type(error).__name__}
   request_path=L/(TAG+'.runtime830.restart.requested.json')
   planned_request_present=os.path.lexists(request_path)
   natural_input=None
   if not planned_request_present:
    try:
     if error_unit.get('ActiveState')=='active':
      natural_input=n933.current_native(error_unit,tr.host_native,lambda:props('clarp-runtime.service'))
      admitted=natural_guard.admit(transition.states.get('runtime_healthy'),error_unit,natural_input,row,time.time(),time.monotonic(),tr.qualify_natural,runtime_guard)
      if admitted:expected_probe_runtime=CAPSULE['predicted_runtime']
      row['natural_admission933']=dict(state='ADMITTED' if admitted else 'NOT_POSITIVELY_JOINED',target=CAPSULE['predicted_runtime'],refusal=natural_guard.last_refusal)
      if not admitted and (row.get('runtime_id')==CAPSULE['predicted_runtime'] or av.identity(error_unit)!=runtime_guard.expected):
       transition.natural_observation('native_admission')
    except Exception as error:
     row['natural_admission933']=dict(state='UNKNOWN',type=type(error).__name__,message=str(error),traceback=traceback.format_exc())
   if not planned_request_present and natural_guard.anchor is not None:
    try:
     decision=n933.apply_errors(err927,natural_guard,row,decision,error_windows,baseline,now,row.get('mono'))
     boot=row['error_policy927']['boot_window']
    except Exception as error:
     boot=dict(state='UNKNOWN',code='natural_healthy_clock_unknown',diagnostic=dict(type=type(error).__name__,message=str(error),traceback=traceback.format_exc()))
     decision=err927.apply(row,decision,error_windows,boot,baseline,now)
   else:
    boot=err927.load_boot(L,TAG,runtime_context,error_unit,row,target_id,plan['predicted']['runtime_release_id'],runtime_started)
    decision=err927.apply(row,decision,error_windows,boot,baseline,now)
   row['raw_error_interval927']=error_interval
   if row.get('db_locked_lines_10min',0):
    try:notice('DB_LOCKED_REPORT_ONLY_'+str(int(elapsed//600)),{'count':row['db_locked_lines_10min'],'rollback_trigger':False})
    except Exception as error:print(json.dumps({'event':'DB_REPORT_NOTICE_UNKNOWN_NO_RESEND','class':type(error).__name__}),flush=True)
   rss_request=L/(TAG+'.runtime830.restart.requested.json')
   try:rss_boot=json.loads(rss_request.read_text()) if rss_request.exists() else None
   except (OSError,ValueError):rss_boot=None
   if not planned_request_present and natural_guard.anchor is not None:
    decision=n933.apply_rss(natural_guard,row,decision,rss_guard)
   else:
    decision=rss911.apply(row,decision,rss_guard,now,rss_boot,runtime_started,runtime_context);row['policy883']=decision
   row['policy883']=decision
   row['elapsed_since_switch_seconds']=elapsed
   with (L/(TAG+'.watch883.jsonl')).open('a') as out:out.write(json.dumps(row)+'\n');out.flush()
   print(json.dumps(row),flush=True)
   if decision['triggers']:rollback(decision['triggers'],ship)
   # Runtime unavailability during deliberate boot belongs exclusively to830's300s supervisor.
   # 892: unit ownership is independent from RPC availability.830 owns only its
   # proven supervised boot, never the whole later continuation-helper interval.
   ru=props('clarp-runtime.service');supervised_boot=False
   if runtime_started and not expected_boot_bound:
    hp=L/(TAG+'.runtime830.healthy.json');hl=L/(TAG+'.runtime830.health.jsonl')
    if hp.exists() and hl.exists():
     h=json.loads(hp.read_text());hr=json.loads(hl.read_text().splitlines()[-1])
     if (h.get('runtime_on_installed') and h['probe'].get('runtime_available') is True
         and h['probe'].get('runtime_release_id')==plan['predicted']['runtime_release_id']
         and hr.get('invocation')==ru['InvocationID'] and str(hr.get('runtime_pid'))==ru['MainPID']):
      runtime_guard.supervised_boot_complete(ru);expected_boot_bound=True;expected_probe_runtime=plan['predicted']['runtime_release_id']
      transition.healthy(dict(route='joined830_healthy',health=h,health_row=hr,unit=ru,native=tr.host_native(ru)))
    if not expected_boot_bound:
     request=L/(TAG+'.runtime830.restart.requested.json')
     supervised_boot=(request.exists() and 0<=now-json.loads(request.read_text())['epoch']
                      and runtime_child is not None and runtime_child.poll() is None)
     # The unchanged830 child owns its300s targetboot AND any controlled rollback.
     # As soon as targethealth is proven above, this exemption ends even while
     # continuation sends run. Never race its rollback with a second operator.
   try:
    if os.path.lexists(L/(TAG+'.runtime830.restart.requested.json')):
     availability=runtime_guard.sample(row,ru,time.monotonic(),confirm_runtime,
        lambda:props('clarp-runtime.service'),supervised=supervised_boot)
    else:
     availability=natural_guard.availability(runtime_guard,row,ru,time.monotonic(),confirm_runtime,
        lambda:props('clarp-runtime.service'),av.identity,supervised=supervised_boot)
   except Exception as error:
    availability={'rollback':None,'unknown':type(error).__name__}
    notice('RUNTIME_AVAILABILITY_OBSERVATION_UNKNOWN',availability)
   row['runtime_availability892']=availability
   save('.runtime-availability892.latest.json',dict(availability,epoch=now,unit=ru))
   if availability.get('rollback'):rollback([availability['rollback']],ship,runtime_guard.last_observed)
   # Timeout events are counted independently; neither one nor many is a trigger.
   try:
    rpc=av.rpc_timeout_count(run,since,now);rpc_intervals.append((now,rpc))
    total_rpc=sum(n for t,n in rpc_intervals if now-t<=600)
    row['runtime_rpc_timeouts_10min']=total_rpc
    save('.runtime-timeouts892.latest.json',{'epoch':now,'events_last10min':total_rpc,'events_this_interval':rpc})
    if rpc:notice('RPC_TIMEOUTS_10MIN_'+str(int((now-switch_epoch)//600)),{'count':total_rpc,'scope':'timeouts alone never rollback'})
    restart_file=L/(TAG+'.runtime830.restart.requested.json')
    if rpc and runtime_started and restart_file.exists() and not timeout_dump_attempted and now-json.loads(restart_file.read_text())['epoch']<=600:
     timeout_dump_attempted=True
     dump_unit=props('clarp-runtime.service');dump_pid=dump_unit['MainPID']
     save('.runtime-stacks892.requested.json',{'pid':dump_pid,'invocation':dump_unit['InvocationID'],'epoch':now,'attempt':1,'locals':False},True)
     if dump_unit['ActiveState']=='active' and int(dump_pid)>0 and props('clarp-runtime.service')==dump_unit:
      with (L/(TAG+'.runtime-stacks892.txt')).open('x') as out:
       os.chmod(out.name,0o600)
       try:
        capture=subprocess.run(['sudo','-n','/var/tmp/pyspy-venv/bin/py-spy','dump','--pid',dump_pid],stdout=out,stderr=subprocess.STDOUT,timeout=40)
        save('.runtime-stacks892.result.json',{'exit':capture.returncode,'pid':dump_pid,'locals':False})
       except subprocess.TimeoutExpired:save('.runtime-stacks892.result.json',{'result':'timeout_unknown_no_repeat','pid':dump_pid,'locals':False})
   except Exception as error:notice('RUNTIME_TIMEOUT_COUNT_UNKNOWN',{'class':type(error).__name__})
   with (L/(TAG+'.availability892.jsonl')).open('a') as out:out.write(json.dumps({'epoch':now,'runtime_guard':availability,'timeouts_10min':row.get('runtime_rpc_timeouts_10min')})+'\n');out.flush()
   bad=row.get('host_ready') is False
   last_ready_bad=(last_ready_bad or now) if bad else None
   down=type(row.get('host_code')) is int and row['host_code']!=200
   http_down=(http_down or now) if down else None
   if (not deferred and last_ready_bad and now-last_ready_bad>=120) or (http_down and now-http_down>60):rollback(['Host_health_734'],ship)
   fault=bool(row.get('unknown')) and not(deferred and set(row['unknown'])<= {'failed_turn_classifier'})
   if fault and not fault_episode:notice('OBSERVATION_UNKNOWN',{'unknown':row['unknown'],'elapsed':elapsed,'other_probes_and_trigger_checks_continue':True})
   fault_episode=fault
   error_fault=bool(row.get('error_policy927',{}).get('partition',{}).get('capture_unknown')) or boot.get('state')=='UNKNOWN'
   if error_fault and not error_fault_episode:
    try:notice('ERROR_WINDOW_OBSERVATION_UNKNOWN',{'boot_state':boot.get('state'),'codes':row['error_policy927']['partition'].get('capture_unknown'),
          'eligible_counts':row['error_policy927']['partition'].get('eligible'),'other_triggers_continue':True})
    except Exception as error:print(json.dumps({'event':'ERROR_WINDOW_NOTICE_UNKNOWN_NO_RESEND','class':type(error).__name__}),flush=True)
   error_fault_episode=error_fault
   if row.get('push_transport_failure_records',0) or any(int(k)>=500 for k in row.get('push_apns_status_counts',{})):
    notice('PUSH_REVIEW_REQUIRED',{'transport_failures':row.get('push_transport_failure_records'),'codes':row.get('push_apns_status_counts'),'scope':'uncorrelated passive evidence; owner adjudication required'})
  if ship.poll() is not None and not host_pass:
   code=ship.returncode;save('.ship.result.json',{'exit':code,'epoch':time.time()});(L/(TAG+'.ship.rc')).write_text(str(code)+'\n');(L/(TAG+'.ship.log.exit')).write_text(str(code)+'\n');shiplog.close()
   if code:raise RuntimeError('rc5_Host_ship_nonzero_read_retained_receipts')
   r=run(['python3',str(ROOT/'capture-host.py')],90)
   if r.returncode:raise RuntimeError('Host_pass_capture_unknown')
   captured=json.loads((L/(TAG+'.final-status.json')).read_text())
   if captured.get('runtime_on_installed') is True and captured.get('probe',{}).get('runtime_available') is True and captured['probe'].get('runtime_release_id')==CAPSULE['predicted_runtime']:
    transition.natural_observation('Host_PASS')
   host_pass=True;notice('HOST_WATCH_PASS',json.loads((L/(TAG+'.batch-Host-summary.json')).read_text()))
  if host_pass and not runtime_started:
   # One attempt before explicit830; DB UNKNOWN/candidates are report-only.
   # Owned-child uncertainty is separately retained, never a database veto.
   natural=True if transition.states.get('runtime_healthy',{}).get('route')=='joined_natural_status_attestation' else None
   if transition.before_runtime(natural):
    runtime_child,runtime_log=runtime_stage()
    runtime_started=runtime_child is not None
  if runtime_child is not None and runtime_child.poll() is not None and not runtime_done:
   code=runtime_child.returncode
   if not runtime_terminal_saved:
    runtime_log.close();(L/(TAG+'.runtime830.job.log.exit')).write_text(str(code)+'\n');runtime_terminal_saved=True
   summarypath=L/(TAG+'.runtime830.summary.json')
   if code or not summarypath.exists():raise RuntimeError('runtime_operator_nonzero_read_receipts_no_second_restart')
   summary=json.loads(summarypath.read_text());assert summary['result']=='runtime_activation_health_pass' and not summary['rolled_back']
   runtime_completion_kind=n933.completion_kind(summary)
   if runtime_completion_kind==1:
    completion=complete_handover(summary)
    reader.restart_witnesses=completion['witnesses'];runtime_done=True
    if 'replay' not in transition.states:transition.natural_observation('830_completion')
    transition.audit_continues(summary)
    notice('RUNTIME_HEALTH_AND_CONTINUES',summary)
   else:
    transition.audit_continues(summary)
    if natural_guard.anchor is None:
     transition.natural_observation('830_completion')
    pending_natural=any(stage=='natural' for _child,_path,stage in transition.children.values())
    union=transition.states.get('union',{})
    if natural_guard.anchor is not None and union.get('state')=='UNION_RECONCILED_METADATA':
     latest_unit=props('clarp-runtime.service');latest_native=n933.current_native(latest_unit,tr.host_native,lambda:props('clarp-runtime.service'))
     completed=complete_handover(summary,natural_guard,latest_unit,latest_native,row,union)['natural']
     save('.runtime-natural933.complete.json',completed,True)
     runtime_done=True
     notice('RUNTIME_HEALTH_AND_CONTINUES',dict(summary,route='NATURAL_ZERO_NO_PLANNED_EXCLUSION'))
    elif union.get('state')!='RUNNING' and not pending_natural:
     raise n933.NaturalUnknown('natural_zero_completion_missing_positive_join_or_union')
  watch_elapsed=(boot.get('elapsed_seconds') if boot.get('state')=='NATURAL_NO_EXPLICIT_RESTART' else now-boot['healthy_epoch'] if boot.get('state')=='PROVEN' else None) if target_id else None
  if target_id and runtime_done and watch_elapsed is not None and watch_elapsed>=1800 and not reported30:
   notice('THIRTY_MINUTE_MARK',{'release_id':target_id,'runtime_healthy_and_continues_complete':runtime_done,'baseline':baseline,'sample':row,'no_ship_GO_for_next_release':True,'healthy_clock_anchor':boot['healthy_epoch'],'healthy_elapsed':watch_elapsed});reported30=True
  if target_id and watch_elapsed is not None and watch_elapsed>=7200:
   if not runtime_done:raise RuntimeError('runtime_completion_missing')
   result={'result':'TWO_HOUR_WATCH_COMPLETE','tag':TAG,'release_id':target_id,'runtime':plan['predicted']['runtime_release_id'],'samples':len(post),'baseline':baseline,'last_sample':row,'unknown_samples':sum(bool(x.get('unknown')) for x in post),'scope':'passivepushuncorrelated; unknown coverage explicitly retained','healthy_clock_anchor':boot['healthy_epoch'],'healthy_elapsed':watch_elapsed,'error_policy_unknown_samples':sum(bool(x.get('error_policy927',{}).get('partition',{}).get('capture_unknown')) for x in post)}
   save('.892.summary.json',result);notice('TWO_HOUR_MARK',result);break
  since=now
  time.sleep(max(0,10-(time.monotonic()-begin)))

if __name__=='__main__':
 try:main()
 except SystemExit:raise
 except Exception as error:
  failure={'event':'RESHIP892_CONTROL_FAILED','class':type(error).__name__,'retry':False}
  print(json.dumps(failure),file=sys.stderr,flush=True)
  try:notice('WATCH_CONTROL_FAILED',failure)
  except Exception:pass
  raise

 finally:
  if transition is not None:
   try:
    # Distinct terminal observation on official rollback/control-abort; no new
    # service effects, no replacement of missing healthy-handover evidence.
    transition.terminal_replay('operator_terminal_forward_or_rollback')
   except Exception as error:
    print(json.dumps({'event':'TERMINAL_REPLAY_UNKNOWN','class':type(error).__name__}),flush=True)
   try:transition.finish('operator_terminal_read_primary_exit_and_raw_receipts')
   except Exception as error:
    print(json.dumps({'event':'TRANSITION_OWNERSHIP_OR_SINK_UNKNOWN','class':type(error).__name__}),flush=True)
