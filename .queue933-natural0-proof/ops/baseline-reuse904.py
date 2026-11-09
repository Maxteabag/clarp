"""904/905 conservative AS-IS b7 reuse; original run/source and pre-invocation freshness only."""
from pathlib import Path
import hashlib,importlib.util,json,math,os,sqlite3,subprocess,time

OLD_SOURCE='b7a4a36ce4ebf43ae8ef87eb9eb50be4bb37ecd1'
OLD_MANIFEST='af35104eae683e48d13707177b586a1f14048bc915bedbb45434ab1e824d3f8d'
OLD_REPORT='ba60510c9856deb22ac00f88108dd5d2317de04ced8a91115f63cde1e7434e98'
FIELDS=('db_locked_records','http_5xx','runtime_writer_giveups','genuine_failed_turns')
TURN_KEYS={'not_failed','excluded_account','excluded_restart','excluded_stop','genuine','unknown'}

class BaselineRefused(RuntimeError):
    pass

def number(value):
    return type(value) in (int,float) and math.isfinite(value)

def read_rows(path):
    with Path(path).open('rb') as f:raw=f.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise BaselineRefused('baseline_log_bound')
    rows=[json.loads(line) for line in raw.splitlines() if line.strip()]
    if not rows or len(rows)>2000:raise BaselineRefused('baseline_row_bound')
    return rows

def window(rows,metrics,now,*,reuse,original_metrics=None):
    """Return exact old embedded eligibility, never repair a counter or pick older-good."""
    if not rows:raise BaselineRefused('baseline_empty')
    if any(not isinstance(r,dict) or not number(r.get('epoch')) or not number(r.get('mono')) for r in rows):
        raise BaselineRefused('baseline_clock_schema')
    if any(b['epoch']<=a['epoch'] or b['mono']<=a['mono'] for a,b in zip(rows,rows[1:])):
        raise BaselineRefused('baseline_clock_order')
    selected=rows
    upper=now
    latest=selected[-1]
    if not number(upper) or not 0<=upper-latest['epoch']<=120:
        raise BaselineRefused('baseline_not_fresh_at_boundary')
    result=metrics.eligibility(selected)
    original_result=(original_metrics.eligibility(selected) if original_metrics else result)
    if result.get('qualifies') is not True or latest.get('eligibility')!=original_result:
        raise BaselineRefused('latest_window_not_qualified_or_not_asis')
    start=result.get('start_epoch');end=result.get('end_epoch')
    if not number(start) or end!=latest['epoch']:
        raise BaselineRefused('window_identity')
    samples=[r for r in selected if r['epoch']>=start]
    if len(samples)!=result.get('samples'):raise BaselineRefused('window_sample_count')
    excluded=0;unknown_total=0
    policy922=(not reuse and result.get('eligibility_policy')=='917_922'
               and result.get('classifier_unknown_budget')==6)
    for r in samples:
        errors=r.get('error_counts');counts=r.get('failed_turn_counts');faults=r.get('unknown',{})
        if (r.get('probe_errors') or r.get('result')!='OBSERVED'
            or not isinstance(faults,dict) or not isinstance(errors,dict) or not isinstance(counts,dict)
            or set(counts)-TURN_KEYS or any(type(v) is not int or v<0 for v in counts.values())
            or any(type(errors.get(k)) is not int or errors[k]<0 for k in FIELDS if k!='genuine_failed_turns')):
            raise BaselineRefused('raw_count_schema')
        n=counts.get('unknown',0);known=counts.get('genuine',0);raw=errors.get('genuine_failed_turns')
        if n:
            if (not policy922 or faults!={'failed_turn_classifier':'UNKNOWN_CAUSE'}
                or not (raw is None or (type(raw) is int and raw==known))):
                raise BaselineRefused('window_unknown')
            unknown_total+=n
        elif faults or type(raw) is not int or raw!=known:
            raise BaselineRefused('window_unknown_or_count_mismatch')
        excluded+=counts.get('excluded_account',0)
    if unknown_total>6 or (policy922 and result.get('classifier_unknown_rows')!=unknown_total):
        raise BaselineRefused('classifier_unknown_budget')
    if reuse:
        for key in ('last10_error_counts','prior10_error_counts','baseline_error_counts'):
            group=result.get(key)
            if not isinstance(group,dict) or type(group.get('genuine_failed_turns')) is not int or group['genuine_failed_turns']!=0:
                raise BaselineRefused('old_genuine_nonzero_or_unknown')
        # Retain B904 conservative condition until exact-source superset release.
        if excluded!=0:raise BaselineRefused('old_account_exclusion_equivalence_unproven')
    return {'baseline':dict(result),'identity':(latest['pid'],latest['process_start'],latest['boot']),
            'boundary_epoch':upper,'age_seconds':upper-latest['epoch'],
            'mode':'B7_AS_IS_904_905' if reuse else'FRESH_FINAL_R1',
            'no_relabel_no_backfill':True,
            'original_embedded_eligibility':original_result}

def native_host_identity():
    """Fresh clarp.service incarnation, never the recorder/worker identity."""
    def unit():
        result=subprocess.run(['systemctl','--user','show','clarp.service',
            '--property=ActiveState,MainPID'],capture_output=True,text=True,timeout=10)
        props=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or props.get('ActiveState')!='active':
            raise BaselineRefused('Host_unit_unknown_or_inactive')
        pid=int(props['MainPID'])
        if pid<=0:raise BaselineRefused('Host_pid_unknown')
        return pid
    try:
        pid=unit()
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        def process():
            fields=Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
            start=int(fields[19])
            if fields[0] in ('Z','X','x') or start<=0:
                raise BaselineRefused('Host_process_not_live')
            return start
        start=process()
        if (not boot or unit()!=pid or process()!=start
                or Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=boot):
            raise BaselineRefused('Host_identity_changed_during_read')
        return (pid,start,boot)
    except (OSError,ValueError,KeyError,IndexError,subprocess.SubprocessError) as error:
        raise BaselineRefused('Host_native_identity_unknown') from error

def admitted_watch(program,job_id,prefix,root,expected_source,expected_manifest):
    """Read-only actual owner/worker/unit+source binding, not a supplied label."""
    program=Path(program);root=Path(root)
    if (root/'SOURCE_COMMIT').read_text().strip()!=expected_source:raise BaselineRefused('watch_source')
    digest=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    if digest(root/'SHA256SUMS')!=expected_manifest:raise BaselineRefused('watch_manifest')
    for line in(root/'SHA256SUMS').read_text().splitlines():
        h,n=line.split(maxsplit=1)
        if digest(root/n.lstrip('*'))!=h:raise BaselineRefused('watch_source_bytes')
    share=Path('/home/peter/.local/share/clarp')
    with sqlite3.connect('file:'+str(share/'state.sqlite')+'?mode=ro',uri=True,timeout=.25) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT session,status,generation,worker_pid,worker_start_token,worker_cwd,log_path FROM background_jobs WHERE job_id=?',(job_id,)).fetchone()
    log=program/'logs/releases'/(prefix+'.log')
    if (row is None or row['session']!='rf-releasec-29c5' or row['status']!='running'
        or row['generation']!=1 or row['worker_cwd']!='/home/peter/GIT/clarp-worktrees/refactor-release'
        or row['log_path']!=str(log)):
        raise BaselineRefused('watch_admission')
    pid=row['worker_pid'];boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    stat=Path('/proc/'+str(pid)+'/stat')
    token=boot+':'+stat.read_text().rsplit(')',1)[1].split()[19]
    if token!=row['worker_start_token']:raise BaselineRefused('watch_worker_changed')
    unit=Path(str(log)+'.unit').read_text().strip()
    if not unit.startswith('clarp-job-'+job_id+'-'):raise BaselineRefused('watch_unit')
    r=subprocess.run(['systemctl','--user','show',unit,'--property=ActiveState,MainPID,ControlGroup'],capture_output=True,text=True,timeout=10)
    props=dict(x.split('=',1) for x in r.stdout.splitlines() if '=' in x)
    if r.returncode or props.get('ActiveState')!='active' or props.get('MainPID')!=str(pid):
        raise BaselineRefused('watch_unit_not_owned')
    group=props.get('ControlGroup','')
    if not group.startswith('/user.slice/'):raise BaselineRefused('watch_cgroup')
    procs=Path('/sys/fs/cgroup'+group+'/cgroup.procs').read_text().splitlines()
    if len(procs)>64:raise BaselineRefused('watch_group_bound')
    expected=[str(root/'ops/eligibility-watch.py'),'--prefix',prefix];found=0
    for process in procs:
        try:args=[x.decode() for x in Path('/proc/'+process+'/cmdline').read_bytes().split(b'\0') if x]
        except FileNotFoundError:continue
        if len(args)>=4 and args[-3:]==expected:found+=1
    if found!=1:raise BaselineRefused('watch_command_binding')
    return {'source':expected_source,'manifest':expected_manifest,'run':job_id,
            'generation':1,'worker_pid':pid,'worker_start_token':token,
            'sample_path':str(program/'logs/releases'/(prefix+'.jsonl')),'unit':unit}

def old_window(program,now):
    program=Path(program);root=program/'release-tooling/reship883-b7a4a36'
    report=program/'reviews/reship883-b7a4a36-B-final.md'
    if hashlib.sha256(report.read_bytes()).hexdigest()!=OLD_REPORT:raise BaselineRefused('old_final_report')
    provenance=admitted_watch(program,'rf-releasec-eligibility911-b7','reship911-b7-eligibility',root,OLD_SOURCE,OLD_MANIFEST)
    spec=importlib.util.spec_from_file_location('old_b7_baseline904',root/'ops/release-metrics.py')
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    choice=window(read_rows(provenance['sample_path']),old,time.time(),reuse=True)
    choice['provenance']=dict(provenance,B_report=str(report),B_report_sha256=OLD_REPORT)
    return choice


R1_SOURCE='8cbe1646769014db660874d445f77b9b9c28c35b'
R1_MANIFEST='6d79797283f98d68abf5bef467b3a6d08feffee1508a6b7f43c9e9e3d27948f7'
R1_REPORT='d0837ca5332f69dc2d24c41fde20ae59f4ad40f7f0a8fed9abcb365171ba789c'

def derived_r1_window(program,metrics):
    """917 policy derivation from actual admittedR1 bytes, never an ASIS relabel."""
    program=Path(program);root=program/'release-tooling/reship883-r1-8cbe164'
    report=program/'reviews/reship883-r1-8cbe164-B-final.md'
    if hashlib.sha256(report.read_bytes()).hexdigest()!=R1_REPORT:
        raise BaselineRefused('R1_final_report')
    provenance=admitted_watch(program,'rf-releasec-eligibility911-r1',
                'reship911-r1-eligibility',root,R1_SOURCE,R1_MANIFEST)
    if provenance.get('source')!=R1_SOURCE or provenance.get('manifest')!=R1_MANIFEST:
        raise BaselineRefused('R1_derived_source_identity')
    policy=Path(metrics.__file__).resolve()
    policyroot=policy.parent.parent
    register=json.loads((program/'registers/reship883-proof-acceptance.json').read_text())
    digest=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    if (register.get('B_final') is not True
        or register.get('source')!=(policyroot/'SOURCE_COMMIT').read_text().strip()
        or register.get('manifest')!=digest(policyroot/'SHA256SUMS')
        or register.get('files',{}).get('release-metrics.py')!=digest(policy)
        or digest(register['B_report'])!=register.get('B_report_sha256')):
        raise BaselineRefused('R3_policy_proof_binding')
    spec=importlib.util.spec_from_file_location('original_R1_for_917',root/'ops/release-metrics.py')
    original=importlib.util.module_from_spec(spec);spec.loader.exec_module(original)
    sample=Path(provenance['sample_path'])
    with sample.open('rb') as f:raw=f.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise BaselineRefused('baseline_log_bound')
    lines=raw.splitlines(keepends=True)
    rows=[json.loads(line) for line in lines if line.strip()]
    if not rows or len(rows)>2000 or not raw.endswith(b'\n'):
        raise BaselineRefused('derived_rows_incomplete_or_bound')
    choice=window(rows,metrics,time.time(),reuse=False,original_metrics=original)
    start=choice['baseline']['start_epoch']
    window_lines=[line for line in lines if line.strip() and json.loads(line)['epoch']>=start]
    choice['mode']='R3_DERIVED_ACTUAL_R1_917'
    choice['provenance']=dict(provenance,original_B_report=str(report),
        original_B_report_sha256=R1_REPORT,raw_prefix_bytes=len(raw),
        raw_prefix_sha256=hashlib.sha256(raw).hexdigest(),
        raw_selected_window_sha256=hashlib.sha256(b''.join(window_lines)).hexdigest(),
        raw_selected_samples=len(window_lines),policy_source=register['source'],
        policy_manifest=register['manifest'],policy_file_sha256=digest(policy),
        policy_B_report=register['B_report'],policy_B_report_sha256=register['B_report_sha256'],
        derivation='917 separatecomputedpolicy; originalR1raw/embeddedeligibility unchanged')
    return choice
