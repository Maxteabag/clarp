#!/usr/bin/python3
"""Owner's one-shot orchestration of the official installer and supported units.
No product edits, tests, PRE/replay ceremony, prompts, model/config mutation.
Only run after captain's exact main-GREEN handoff is independently retained.
"""
import ast,datetime,fcntl,hashlib,json,os,pathlib,re,sqlite3,subprocess,sys,time,traceback,tomllib
P=pathlib.Path('/var/tmp/clarp-refactor-program')
SHARE=pathlib.Path('/home/peter/.local/share/clarp')
CHECK=P/'release-tooling/v8n2-814-rc5/bin/release-check'
SESSION='rf-releasec-29c5'

def digest(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def write(root,name,data):
    path=root/name
    with path.open('x') as f:
        os.chmod(path,0o600);json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    return path

def command(root,name,argv,env=None,cwd=None,timeout=None):
    write(root,name+'.request.json',{'argv':argv,'start_epoch':time.time(),'start_mono':time.monotonic(),'attempt':1})
    result={'argv':argv,'rc':None,'start_mono':time.monotonic()}
    try:
        with (root/(name+'.stdout')).open('xb') as so,(root/(name+'.stderr')).open('xb') as se:
            proc=subprocess.Popen(argv,stdout=so,stderr=se,env=env,cwd=cwd)
            result['pid']=proc.pid
            try:result['rc']=proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Never signal a possibly admitted service operation or retry.
                result['state']='COMMAND_RETURN_UNKNOWN_NO_RETRY';raise
            so.flush();se.flush();os.fsync(so.fileno());os.fsync(se.fileno())
    finally:
        result['end_mono']=time.monotonic();write(root,name+'.return.json',result)
    return result['rc']

def unit(name):
    r=subprocess.run(['/usr/bin/systemctl','--user','show',name,'--property=ActiveState,SubState,MainPID,InvocationID,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic'],capture_output=True,text=True,timeout=5)
    if r.returncode:raise RuntimeError('unit_read_UNKNOWN:'+name)
    return dict(x.split('=',1) for x in r.stdout.splitlines() if '=' in x)

def status():
    r=subprocess.run([str(CHECK),'--observe-runtime','status'],capture_output=True,text=True,timeout=12)
    if r.returncode:raise RuntimeError('normal_health_status_UNKNOWN:'+str(r.returncode))
    return json.loads(r.stdout)

class ObservationShapeUnknown(ValueError):
    pass

def validated_observation(s,u):
    # Validate only observation data. Persistence/control errors never enter
    # this transient classification boundary.
    if type(s) is not dict or type(u) is not dict:
        raise ObservationShapeUnknown('status_or_unit_not_object')
    i=s.get('installed');q=s.get('probe')
    if type(i) is not dict or type(q) is not dict:
        raise ObservationShapeUnknown('installed_or_probe_not_object')
    if any(type(s.get(k)) is not bool for k in ('host_on_installed','runtime_on_installed')):
        raise ObservationShapeUnknown('installed_flags_not_bool')
    if any(type(i.get(k)) is not str for k in ('version','release_id')):
        raise ObservationShapeUnknown('installed_identity_not_strings')
    if type(q.get('status_code')) is not int or any(type(q.get(k)) is not bool for k in ('health_ready','runtime_available')):
        raise ObservationShapeUnknown('probe_status_or_health_types')
    if any(type(q.get(k)) is not str for k in ('release_id','runtime_release_id')):
        raise ObservationShapeUnknown('probe_identity_not_strings')
    if type(u.get('ActiveState')) is not str or type(u.get('MainPID')) is not str or not re.fullmatch('[0-9]+',u['MainPID']):
        raise ObservationShapeUnknown('runtime_unit_state_or_pid_types')
    return s,u

def target_host(s,sha):
    if type(s) is not dict:return False
    q=s.get('probe');i=s.get('installed')
    if type(q) is not dict or type(i) is not dict:return False
    return bool(s.get('host_on_installed') is True and isinstance(i.get('version'),str) and re.fullmatch('[0-9a-f]{7,40}',i['version']) and sha.startswith(i['version']) and type(i.get('release_id')) is str and i['release_id'].startswith(i['version']+'-') and q.get('release_id')==i['release_id'] and q.get('health_ready') is True and type(q.get('status_code')) is int and q['status_code']==200)

def healthy_target_runtime(s,target):
    if type(s) is not dict or type(s.get('probe')) is not dict:return False
    q=s['probe']
    return bool(s.get('runtime_on_installed') is True and q.get('runtime_available') is True and q.get('runtime_release_id')==target and q.get('health_ready') is True)

def owns_bound_install(bound,sha):
    # Rejoin exactly the canonical installation whose abbreviation was already
    # resolved to this full commit. Never infer ownership from a fixed prefix.
    current=(SHARE/'current').resolve(strict=True)
    return bool(bound['resolved_commit']==sha and current==pathlib.Path(bound['canonical_directory']) and current.parent==(SHARE/'releases').resolve(strict=True) and current.name==bound['release_id'] and
        (current/'DEPLOYED_RELEASE_ID').read_text().strip()==bound['release_id'] and
        (current/'DEPLOYED_VERSION').read_text().strip()==bound['version'] and
        (current/'RUNTIME_RELEASE_ID').read_text().strip()==bound['runtime_id'] and
        re.fullmatch('[0-9a-f]{7,40}',bound['version']) and sha.startswith(bound['version']) and bound['release_id'].startswith(bound['version']+'-'))

def runtime_wait(root,sha,target,origin,deadline):
    rows=[]
    def record(row):
        rows.append(row)
        with (root/'runtime-health.jsonl').open('a') as f:json.dump(row,f);f.write('\n');f.flush();os.fsync(f.fileno())
    while time.monotonic()<deadline:
        s=None;u=None;observation_errors=[]
        for kind,read in (('status',status),('unit',lambda:unit('clarp-runtime.service'))):
            try:
                value=read()
                if kind=='status':s=value
                else:u=value
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
                observation_errors.append({'part':kind,'error':type(exc).__name__+':'+str(exc),'traceback':traceback.format_exc()})
            except RuntimeError as exc:
                if not str(exc).startswith(('normal_health_status_UNKNOWN:','unit_read_UNKNOWN:')):raise
                observation_errors.append({'part':kind,'error':type(exc).__name__+':'+str(exc),'traceback':traceback.format_exc()})
        row={'mono':time.monotonic(),'status':s,'runtime_unit':u,'observation_errors':observation_errors,'original_origin':origin,'original_deadline':deadline}
        # Independent positive evidence has precedence over unrelated missing
        # health fields. Only a HEALTH PASS requires the complete tuple.
        installed=s.get('installed') if type(s) is dict else None
        probe=s.get('probe') if type(s) is dict else None
        version=installed.get('version') if type(installed) is dict else None
        if type(version) is str and re.fullmatch('[0-9a-f]{7,40}',version) and not sha.startswith(version):
            row['classification']='POSITIVE_INSTALLED_IDENTITY_CHANGED';record(row);raise RuntimeError('positive_installed_identity_changed_STOP')
        if type(probe) is dict and type(installed) is dict and type(probe.get('status_code')) is int and probe['status_code']==200 and type(probe.get('release_id')) is str and type(installed.get('release_id')) is str and probe['release_id']!=installed['release_id']:
            row['classification']='POSITIVE_HOST_IDENTITY_CHANGED';record(row);raise RuntimeError('positive_Host_identity_changed_STOP')
        if type(u) is dict and u.get('ActiveState')=='failed':
            row['classification']='RUNTIME_UNIT_FAILED';record(row);return None,u,rows
        try:
            validated_observation(s,u)
        except ObservationShapeUnknown as exc:
            row.update(UNKNOWN=type(exc).__name__+':'+str(exc),traceback=traceback.format_exc())
        else:
            if target_host(s,sha) and healthy_target_runtime(s,target) and u['ActiveState']=='active' and int(u['MainPID'])>0:
                row['classification']='POSITIVE_SAMPLE_CANDIDATE_PENDING_PERSISTENCE_DEADLINE'
                record(row)
                # No IO after this SAME-deadline acceptance check. Slow fsync
                # cannot turn a late observation into a positive return.
                if time.monotonic()>=deadline:
                    record({'classification':'POSITIVE_BUT_PERSISTENCE_LATE_NOT_ACCEPTED','mono':time.monotonic(),'original_origin':origin,'original_deadline':deadline})
                    return None,u,rows
                return s,u,rows
            row['classification']='TRANSIENT_UNKNOWN_OR_WARMUP_NO_IDENTITY_FAILURE'
        # Evidence errors propagate. They are never swallowed as probe warmup.
        record(row)
        time.sleep(min(2,max(0,deadline-time.monotonic())))
    return None,None,rows

def stability_window(root,sha,target,origin,deadline,bound_install):
    """Actuallead105714: seven observations, >=6 positive, no adjacent failures.
    Read-only. SAME original activation deadline, no restart/recovery/retry.
    A failed window reports STOP; it never enters the pre-health rollback path.
    """
    window_origin=time.monotonic();rows=[];last_positive=None;last_unit=None;previous_start=None
    expected_release=bound_install['release_id'];expected_version=bound_install['version']
    if bound_install['resolved_commit']!=sha or bound_install['runtime_id']!=target:
        raise RuntimeError('stability_captured_install_binding_changed_STOP')
    def finish(outcome):
        if outcome=='POLICY_PASS' and not owns_bound_install(bound_install,sha):
            outcome='INSTALL_OWNERSHIP_CHANGED_STOP_NO_ACTION'
        payload={'publication':'CANDIDATE_ONLY','candidate_outcome':outcome,
            'rows':rows,'window_origin':window_origin,'original_origin':origin,
            'original_deadline':deadline,'interval_seconds':20,'required_samples':7,
            'required_positive':6,'pass_count':sum(r['all_required_flags'] for r in rows),
            'failed_count':sum(not r['all_required_flags'] for r in rows),
            'consecutive_failures':any(not a['all_required_flags'] and not b['all_required_flags'] for a,b in zip(rows,rows[1:]))}
        write(root,'STABILITY-WINDOW.json',payload)
        accepted_at=time.monotonic()
        if accepted_at>=deadline:
            return dict(payload,state='ORIGINAL_DEADLINE_EXHAUSTED_STOP_NO_ACTION')
        if outcome=='POLICY_PASS':
            return dict(payload,state='PASS',accepted_mono=accepted_at,last_positive_status=last_positive,last_positive_unit=last_unit)
        return dict(payload,state=outcome)
    if window_origin+120>=deadline:
        return finish('INSUFFICIENT_ORIGINAL_BUDGET_STOP_NO_ACTION')
    for index in range(7):
        scheduled=max(window_origin+20*index,previous_start+20 if previous_start is not None else window_origin)
        if max(scheduled,time.monotonic())+20*(6-index)>=deadline:
            return finish('INSUFFICIENT_ORIGINAL_BUDGET_STOP_NO_ACTION')
        while time.monotonic()<scheduled:
            now=time.monotonic()
            if now>=deadline:return finish('ORIGINAL_DEADLINE_EXHAUSTED_STOP_NO_ACTION')
            time.sleep(min(1,scheduled-now,deadline-now))
        if time.monotonic()>=deadline:return finish('ORIGINAL_DEADLINE_EXHAUSTED_STOP_NO_ACTION')
        started=time.monotonic()
        if started+20*(6-index)>=deadline:
            return finish('INSUFFICIENT_ORIGINAL_BUDGET_STOP_NO_ACTION')
        previous_start=started;sample=None;native=None;errors=[]
        for kind,read in (('status',status),('unit',lambda:unit('clarp-runtime.service'))):
            try:
                value=read()
                if kind=='status':sample=value
                else:native=value
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
                errors.append({'part':kind,'error':type(exc).__name__+':'+str(exc),'traceback':traceback.format_exc()})
            except RuntimeError as exc:
                if not str(exc).startswith(('normal_health_status_UNKNOWN:','unit_read_UNKNOWN:')):raise
                errors.append({'part':kind,'error':type(exc).__name__+':'+str(exc),'traceback':traceback.format_exc()})
        row={'index':index,'scheduled_mono':scheduled,'start_mono':started,
            'end_mono':time.monotonic(),'original_origin':origin,'original_deadline':deadline,
            'status':sample,'runtime_unit':native,'errors':errors,'all_required_flags':False}
        ins=sample.get('installed') if type(sample) is dict else None
        probe=sample.get('probe') if type(sample) is dict else None
        version=ins.get('version') if type(ins) is dict else None
        positive_identity_error=None
        if type(version) is str and re.fullmatch('[0-9a-f]{7,40}',version) and version!=expected_version:
            positive_identity_error='positive_installed_identity_changed_during_stability_STOP'
        if type(ins) is dict and type(ins.get('release_id')) is str and ins['release_id']!=expected_release:
            positive_identity_error='positive_installed_release_changed_during_stability_STOP'
        if type(probe) is dict and type(probe.get('status_code')) is int and probe['status_code']==200 and type(probe.get('release_id')) is str and probe['release_id']!=expected_release and positive_identity_error is None:
            positive_identity_error='positive_Host_identity_changed_during_stability_STOP'
        if type(probe) is dict and probe.get('runtime_available') is True and type(probe.get('runtime_release_id')) is str and probe['runtime_release_id']!=target:
            positive_identity_error='positive_available_runtime_identity_changed_during_stability_STOP'
        if positive_identity_error:
            row['classification']=positive_identity_error;rows.append(row)
            write(root,'stability-sample-'+str(index)+'.json',row)
            raise RuntimeError(positive_identity_error)
        unit_failed=type(native) is dict and native.get('ActiveState')=='failed'
        try:
            validated_observation(sample,native)
        except ObservationShapeUnknown as exc:
            row.update(UNKNOWN=type(exc).__name__+':'+str(exc),traceback=traceback.format_exc())
        else:
            row['all_required_flags']=bool(target_host(sample,sha) and healthy_target_runtime(sample,target) and native['ActiveState']=='active' and int(native['MainPID'])>0)
        row['classification']='HEALTH_CANDIDATE' if row['all_required_flags'] else 'FAILED_OBSERVATION_RETAINED'
        if unit_failed:row['classification']='RUNTIME_UNIT_FAILED_STOP_NO_ACTION'
        rows.append(row)
        write(root,'stability-sample-'+str(index)+'.json',row)
        # Positive observations are candidates until all persistence and the
        # SAME deadline guard complete. Evidence failures are never swallowed.
        if time.monotonic()>=deadline:return finish('ORIGINAL_DEADLINE_EXHAUSTED_STOP_NO_ACTION')
        if unit_failed:return finish('RUNTIME_UNIT_FAILED_STOP_NO_ACTION')
        if row['all_required_flags']:last_positive=sample;last_unit=native
        failed=sum(not r['all_required_flags'] for r in rows)
        adjacent=len(rows)>1 and not rows[-2]['all_required_flags'] and not row['all_required_flags']
        if failed>1 or adjacent:return finish('STABILITY_RULE_FAILED_STOP_NO_ACTION')
    return finish('POLICY_PASS' if sum(r['all_required_flags'] for r in rows)>=6 else 'STABILITY_RULE_FAILED_STOP_NO_ACTION')

def interruption_metadata(root,u):
    inv=u['InvocationID'];pid=u['MainPID']
    argv=['/usr/bin/journalctl','--user','-u','clarp-runtime.service','_SYSTEMD_INVOCATION_ID='+inv,'--grep','runtimeReady|turnInterruptedByRestart|restartInterruptFail','--output=json','--no-pager']
    rc=command(root,'boot-journal',argv,timeout=20)
    if rc:raise RuntimeError('journal_UNKNOWN')
    raw=(root/'boot-journal.stdout').read_bytes()
    if len(raw)>4*1024*1024:raise RuntimeError('journal_over_cap')
    records={};ready=[];fails=[]
    for line in raw.decode().splitlines():
        row=json.loads(line)
        if row.get('_SYSTEMD_INVOCATION_ID')!=inv or str(row.get('_PID'))!=pid:continue
        m=row.get('MESSAGE','')
        if 'restartInterruptFail' in m:fails.append(row.get('__REALTIME_TIMESTAMP'))
        if 'turnInterruptedByRestart' in m:
            fields=dict(re.findall(r'\b(agent|session|trace|origin)=([^\s]+)',m))
            if not all(fields.get(k) for k in ('agent','session','trace','origin')):raise RuntimeError('interruption_identity_UNKNOWN')
            records[(fields['agent'],fields['trace'])]=dict(fields,ts=int(row['__REALTIME_TIMESTAMP'])//1000)
        if ' runtimeReady ' in m:ready.append(ast.literal_eval(m.split('recovery=',1)[1]))
    if len(ready)!=1 or type(ready[0].get('interrupted')) is not int or ready[0]['interrupted']!=len(records) or fails:raise RuntimeError('boot_interruption_coverage_UNKNOWN')
    db=sqlite3.connect('file:/home/peter/.local/share/clarp/state.sqlite?mode=ro',uri=True,timeout=2)
    db.execute('PRAGMA query_only=ON');db.row_factory=sqlite3.Row
    not_resumed=[];other=[]
    try:
        for record in records.values():
            row=db.execute("SELECT kind,ts,json_extract(detail,'$.source') AS source,json_extract(detail,'$.trace_id') AS trace_id FROM state_log WHERE agent_id=? ORDER BY state_id DESC LIMIT 1",(record['agent'],)).fetchone()
            if row is None:other.append(dict(record,state='LATEST_METADATA_UNAVAILABLE'));continue
            if row['kind']=='interrupted' and row['source']=='server_restart' and (row['trace_id'] or '')==('' if record['trace']=='∅' else record['trace']):
                not_resumed.append(dict(record,state='SAME_INTERRUPTED_WORK_STILL_VISIBLE',latest_ts=row['ts']))
            else:other.append(dict(record,state='DIFFERENT_CURRENT_STATE_NOT_PROOF_OF_SAME_WORK_RESUMPTION',latest_kind=row['kind']))
        counts=[dict(x) for x in db.execute("SELECT session,count(*) AS count FROM queued_turns WHERE status='interrupted' GROUP BY session ORDER BY session")]
    finally:db.close()
    return {'runtimeReady':ready[0],'interrupted_count':len(records),'interrupted_records':list(records.values()),'not_resumed_same_work_count':len(not_resumed),'not_resumed_sessions':not_resumed,'other_current_states':other,'historical_queued_interrupted_counts':counts,'historical_counts_not_boot_attribution':True,'owner_prompts_sent':0,'rc143_attribution':'Only journal source=server_restart in this exact invocation is restart-attributed; no inference for other account-switch rc143.'}

def snapshot_timings(root,anchor_mono,anchor_epoch,host_start_mono):
    # Exactly two GET attempts. No curlrc, redirects, retries or response storage.
    cfg=tomllib.loads(pathlib.Path('/home/peter/.config/clarp/config.toml').read_text())['server']
    host=str(cfg.get('bind_addr','127.0.0.1')).strip()
    if host in ('0.0.0.0','::',''):host='127.0.0.1'
    if ':' in host and not host.startswith('['):host='['+host+']'
    url='http://'+host+':'+str(int(cfg.get('port',7682)))+'/agents/snapshot'
    token=str(cfg.get('auth_token','')).strip()
    if '\r' in token or '\n' in token:raise RuntimeError('auth_header_shape')
    headers=(('Authorization: Bearer '+token+'\n') if token else '').encode()
    rows=[]
    for offset in (120,600):
        requested=anchor_mono+offset
        target=max(requested,host_start_mono+180)
        while time.monotonic()<target:time.sleep(min(10,target-time.monotonic()))
        dispatch=time.monotonic();wall=time.time()
        argv=['/usr/bin/curl','-q','--silent','--show-error','--connect-timeout','5','--max-time','60','--request','GET','--header','@-','--output','/dev/null','--write-out','{"http_code":"%{http_code}","time_total_seconds":%{time_total},"time_starttransfer_seconds":%{time_starttransfer}}\n',url]
        row={'offset_seconds':offset,'anchor_mono':anchor_mono,'anchor_epoch':anchor_epoch,'scheduled_mono':requested,'cold_probe_not_before_mono':host_start_mono+180,'cold_window_deferral_seconds':max(0,target-requested),'actual_dispatch_mono':dispatch,'actual_dispatch_epoch':wall,'lateness_seconds':max(0,dispatch-requested),'GET_attempts':1,'body_destination':'/dev/null','body_retained':False,'auth_not_logged':True}
        try:
            child=subprocess.run(argv,input=headers,capture_output=True,env=dict(os.environ,LC_ALL='C'))
            row.update(actual_OS_rc=child.returncode,end_mono=time.monotonic(),stderr=child.stderr.decode(errors='replace')[:4096])
            try:row['timing']=json.loads(child.stdout)
            except (ValueError,UnicodeDecodeError):row['timing_UNKNOWN']='curl_writeout_not_json';row['writeout_sha256']=hashlib.sha256(child.stdout).hexdigest()
        except OSError as exc:row.update(actual_OS_rc=None,error_type=type(exc).__name__,error_message=str(exc),end_mono=time.monotonic(),GET_attempt_started='UNKNOWN_NO_RETRY')
        write(root,'snapshot-plus'+str(offset)+'.json',row);rows.append(row)
        print(json.dumps({'kind':'SNAPSHOT_TIMING_ONLY_BODY_DISCARDED','observation':row}),flush=True)
    return rows

def announce(root,name,text):
    try:return command(root,name,['/home/peter/.local/bin/clarp-admin','prompt','--to','hobbles-18b0','--from',SESSION,'--text',text],timeout=20)
    except Exception as exc:
        write(root,name+'-UNKNOWN.json',{'error_type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc(),'no_retry':True});return None

def main():
    if len(sys.argv)!=3:raise RuntimeError('usage_launchplan_exactSHA')
    planpath=pathlib.Path(sys.argv[1]);assert digest(planpath)==sys.argv[2]
    b=json.loads(planpath.read_bytes());sha=b['sha'];root=pathlib.Path(b['output'])
    if (not re.fullmatch('[0-9a-f]{40}',sha) or b.get('install_gate')!='LEAD_0859_EXACT_MAIN_CRITICAL_DOCKER_GREEN' or b.get('integration_job_success') is not True):raise RuntimeError('exact_lead_revised_install_gate_required')
    if digest(b['install_authority']['path'])!=b['install_authority']['sha256']:raise RuntimeError('actual_install_order_rebound')
    if b.get('exact_critical_gate_GREEN') is not True or b.get('exact_Docker_integration_GREEN') is not True:raise RuntimeError('exact_head_required_gates_not_GREEN')
    if type(b.get('foreign_focused_required')) is not bool:raise RuntimeError('explicit_foreign_focused_requirement_binding_required')
    if b['foreign_focused_required'] and (b.get('exact_foreign_focused_GREEN') is not True or b.get('foreign_focused_head')!=sha or type(b.get('foreign_focused_run')) is not int or b['foreign_focused_run']<=0):raise RuntimeError('exact_required_foreign_focused_gate_not_GREEN')
    effort_lines=CONFIG_READ=pathlib.Path('/home/peter/.config/clarp/config.toml').read_text().splitlines()
    if len(effort_lines)<56 or not re.fullmatch(r'\s*claude_effort\s*=\s*""\s*(?:#.*)?',effort_lines[55]):raise RuntimeError('config_line56_missing_or_not_explicit_empty_effort_STOP')
    if digest(b['D_review']['path'])!=b['D_review']['sha256']:raise RuntimeError('D_foreign_review_rebound')
    notice=b['actual_lead_park_notice']
    if digest(notice['path'])!=notice['sha256'] or notice.get('actual_notice_confirmed') is not True:raise RuntimeError('actual_lead_park_notice_required')
    if b['not_before_epoch']<notice['issued_epoch']+120 or time.time()<b['not_before_epoch']:raise RuntimeError('planned_minute_or_two_minute_park_not_elapsed')
    if b['worker_sha256']!=digest(__file__) or b['checker_sha256']!=digest(CHECK):raise RuntimeError('worker_or_checker_rebound')
    review=b.get('operator_B_source_review')
    if not isinstance(review,dict) or set(review)!={'path','sha256'} or digest(review['path'])!=review['sha256']:
        raise RuntimeError('actual_operator_B_source_review_binding_required_before_reuse')
    os.umask(0o077);root.mkdir(mode=0o700)
    lock=(P/'release.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    previous=SHARE/'releases'/b['previous_release']
    if (SHARE/'current').resolve()!=previous or digest('/home/peter/.config/clarp/config.toml')!=b['config_sha256']:raise RuntimeError('predecessor_or_config_changed')
    write(root,'BEGIN.json',{'sha':sha,'source':'OFFICIAL_UPDATER_FETCHES_EXACT_REF','previous':str(previous),'Host':unit('clarp.service'),'runtime':unit('clarp-runtime.service'),'status':status(),'authority':b['authority'],'no_PRE_POST_or_quiet_prerequisite':True})
    result={'state':'UNKNOWN','sha':sha,'previous_release':b['previous_release'],'forward_runtime_restart_attempts':0,'owner_prompts_sent':0}
    try:
        announce(root,'lead-start-line','START '+datetime.datetime.now().astimezone().strftime('%H:%M:%S %Z')+' exact '+sha+' official updater; normal readiness, previous kept.')
        rc=command(root,'official-install',['/home/peter/.local/bin/clarp-admin','update','--ref',sha],env=b['installer_env'],cwd='/home/peter/GIT/clarp-worktrees/refactor-release')
        # Use the unchanged normal release health watcher, preserving its
        # readiness/recovery thresholds; do not roll back a single warmup sample.
        installed_runtime=(SHARE/'current/RUNTIME_RELEASE_ID').read_text().strip()
        installed_release=(SHARE/'current/DEPLOYED_RELEASE_ID').read_text().strip()
        installed_version=(SHARE/'current/DEPLOYED_VERSION').read_text().strip()
        if not re.fullmatch('[0-9a-f]{7,40}',installed_version) or not sha.startswith(installed_version) or installed_release!=(SHARE/'current').resolve().name or not installed_release.startswith(installed_version+'-'):
            raise RuntimeError('official_install_target_identity_UNKNOWN')
        resolve_rc=command(root,'installed-commit-resolution',['git','-C',b['official_update_source_repo'],'rev-parse','--verify',installed_version+'^{commit}'])
        if resolve_rc or (root/'installed-commit-resolution.stdout').read_text().strip()!=sha:
            raise RuntimeError('installed_abbreviation_does_not_uniquely_resolve_to_exact_selected_commit')
        bound_install={'canonical_directory':str((SHARE/'current').resolve(strict=True)),'release_id':installed_release,'version':installed_version,'runtime_id':installed_runtime,'resolved_commit':sha}
        if not owns_bound_install(bound_install,sha):raise RuntimeError('official_installed_binding_changed_after_resolution')
        write(root,'BOUND-INSTALL-OWNERSHIP.json',bound_install)
        done=root/'official-install.rc'
        with done.open('x') as f:f.write(str(rc)+'\n');f.flush();os.fsync(f.fileno())
        watch_argv=[str(CHECK),'--observe-runtime','watch','--expect-release-id',installed_release,
            '--expect-runtime-id',installed_runtime,'--previous',previous.name,
            '--previous-runtime-id',(previous/'RUNTIME_RELEASE_ID').read_text().strip(),
            '--install-done-file',str(done),'--minutes','0','--lock-held',
            '--log',str(root/'Host-readiness.jsonl')]
        watch_rc=command(root,'normal-Host-readiness',watch_argv)
        try:s=status()
        except Exception as health_error:
            s={'health_observation_UNKNOWN':type(health_error).__name__+':'+str(health_error),
               'traceback':traceback.format_exc()}
        write(root,'POST-INSTALL-HEALTH-SAMPLE.json',{'installer_rc':rc,'normal_watch_rc':watch_rc,'sample':s})
        if rc or watch_rc or not target_host(s,sha):
            if (SHARE/'current').resolve()==previous:
                result.update(state='NORMAL_WATCH_ROLLED_BACK_TO_PREDECESSOR',install_rc=rc,Host_watch_rc=watch_rc,status=s);return 11
            if not owns_bound_install(bound_install,sha):raise RuntimeError('rollback_ownership_UNKNOWN')
            rollback=command(root,'rollback-host',[(previous/'SERVICE_PYTHON').read_text().strip(),str(previous/'bin/clarp-admin.py'),'rollback',previous.name],env=b['installer_env'],timeout=120)
            result.update(state='HOST_FAILED_ROLLBACK_REQUESTED',install_rc=rc,Host_watch_rc=watch_rc,failed_pre_rollback_sample=s,rollback_rc=rollback,status=status());return 11
        hu=unit('clarp.service');write(root,'HOST-HEALTHY.json',{'status':s,'unit':hu,'actual_install_rc':rc})
        target=(SHARE/'current/RUNTIME_RELEASE_ID').read_text().strip()
        if not re.fullmatch('[0-9a-f]{24}',target):raise RuntimeError('official_installer_runtime_identity_shape_UNKNOWN')
        write(root,'OFFICIAL-INSTALLED-IDENTITY.json',{'release_id':s['installed']['release_id'],'runtime_release_id':target,'service_path':(SHARE/'current/SERVICE_PATH').read_text().strip(),'service_python':(SHARE/'current/SERVICE_PYTHON').read_text().strip(),'toolchain':(SHARE/'current/TOOLCHAIN_DIR').read_text().strip(),'authority':'Lead04:08 officialinstallerPATHauthoritative; no rc5guardbypass'})
        before=unit('clarp-runtime.service');origin=time.monotonic();deadline=origin+300
        if not healthy_target_runtime(s,target):
            result['forward_runtime_restart_attempts']=1
            try:command(root,'runtime-restart',['/usr/bin/systemctl','--user','restart','clarp-runtime.service'],timeout=60)
            except subprocess.TimeoutExpired:result['runtime_command_return']='UNKNOWN_NO_RETRY'
        healthy,after,rows=runtime_wait(root,sha,target,origin,deadline)
        if healthy is None:
            result.update(state='RUNTIME_ACTIVATION_FAILED_OR_UNKNOWN',runtime_deadline=deadline,health_rows=len(rows))
            if not owns_bound_install(bound_install,sha):raise RuntimeError('runtime_failure_rollback_ownership_UNKNOWN')
            rollback_rc=command(root,'runtime-failure-rollback-host',[(previous/'SERVICE_PYTHON').read_text().strip(),str(previous/'bin/clarp-admin.py'),'rollback',previous.name],env=b['installer_env'],timeout=120)
            previous_runtime=(previous/'RUNTIME_RELEASE_ID').read_text().strip();previous_sha=(previous/'DEPLOYED_VERSION').read_text().strip()
            previous_status=status()
            if rollback_rc or not target_host(previous_status,previous_sha):raise RuntimeError('runtime_failure_previous_Host_restore_UNKNOWN')
            recovery_origin=time.monotonic()
            if not healthy_target_runtime(previous_status,previous_runtime):
                command(root,'runtime-failure-rollback-runtime',['/usr/bin/systemctl','--user','restart','clarp-runtime.service'],timeout=60)
            recovered,recovered_unit,recovery_rows=runtime_wait(root,previous_sha,previous_runtime,recovery_origin,recovery_origin+300)
            result.update(state='ROLLED_BACK_AFTER_RUNTIME_FAILURE' if recovered else 'ROLLBACK_RUNTIME_HEALTH_UNKNOWN',rollback_host_rc=rollback_rc,rollback_status=recovered,rollback_runtime_unit=recovered_unit)
            return 11
        stability=stability_window(root,sha,target,origin,deadline,bound_install)
        result['stability']=stability
        if stability['state']!='PASS':
            result.update(state='STABILITY_STOP_NO_ACTION',runtime_deadline=deadline)
            announce(root,'lead-stability-stop','STOP: runtime stability window did not qualify; no rollback/restart/retry. '+str(root/'STABILITY-WINDOW.json'))
            return 11
        healthy=stability['last_positive_status'];after=stability['last_positive_unit']
        health_anchor_mono=time.monotonic();health_anchor_epoch=time.time()
        if digest('/home/peter/.config/clarp/config.toml')!=b['config_sha256']:raise RuntimeError('config_changed_not_authorized')
        recovery=interruption_metadata(root,after)
        if not owns_bound_install(bound_install,sha):raise RuntimeError('final_stability_install_ownership_changed_STOP')
        if time.monotonic()>=deadline:raise RuntimeError('final_stability_original_deadline_exhausted_STOP')
        result.update(state='RELEASE_AND_RUNTIME_HEALTH_PASS',release_id=healthy['installed']['release_id'],runtime_id=target,host_unit=hu,runtime_unit=after,health=healthy['probe'],recovery=recovery,elapsed_runtime_health=time.monotonic()-origin,no_model_or_config_changes=True)
        write(root,'ACTIVATION-HEALTH.json',dict(result,publication='CANDIDATE_PENDING_FINAL_OWNERSHIP_AND_ORIGINAL_DEADLINE',health_anchor_mono=health_anchor_mono,health_anchor_epoch=health_anchor_epoch,performance_observation_state=('PENDING_PLUS2_PLUS10' if b.get('collect_snapshot_timings') is True else 'NOT_REQUESTED_OR_EXPLICITLY_SKIPPED'),outer_worker_still_running_observations_only=(b.get('collect_snapshot_timings') is True)))
        if not owns_bound_install(bound_install,sha):raise RuntimeError('final_health_publication_ownership_changed_STOP')
        if time.monotonic()>=deadline:raise RuntimeError('final_health_publication_original_deadline_exhausted_STOP')
        announce(root,'lead-healthy-line','HEALTHY '+datetime.datetime.now().astimezone().strftime('%H:%M:%S %Z')+' release '+result['release_id']+'; HostPID'+hu['MainPID']+' start '+hu['ExecMainStartTimestamp']+'; runtimePID'+after['MainPID']+' start '+after['ExecMainStartTimestamp']+('; snapshot timing observations pending.' if b.get('collect_snapshot_timings') is True else '; no agent-list timing GETs.'))
        if b.get('collect_snapshot_timings') is True:
            result['snapshot_timings']=snapshot_timings(root,health_anchor_mono,health_anchor_epoch,int(hu['ExecMainStartTimestampMonotonic'])/1000000)
        else:
            result['snapshot_timings']={'state':'NOT_REQUESTED_OR_EXPLICITLY_SKIPPED','owner_GET_attempts':0}
        result['health_anchor_mono']=health_anchor_mono;result['health_anchor_epoch']=health_anchor_epoch
        return 0
    except BaseException as exc:
        result.update(state='UNKNOWN',error=type(exc).__name__+':'+str(exc),traceback=traceback.format_exc());sys.stderr.write(result['traceback']);sys.stderr.flush();return 11
    finally:
        result['terminal_epoch']=time.time();result['terminal_mono']=time.monotonic();write(root,'RESULT.json',result)
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close();print(json.dumps(result),flush=True)
if __name__=='__main__':raise SystemExit(main())
