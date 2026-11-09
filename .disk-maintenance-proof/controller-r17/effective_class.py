"""Typed independent Stage3 receipt/data join. No business writes or new clocks."""
import hashlib, json, os, stat, time, math
from pathlib import Path
from writer_policy import WriterUnknown
PROFILE_SHA='0c43bcf0e5571f8528f5c64784ad6f57ae38dfcaad476d7ce30487632ff9c5d3'
PRODUCER_SHA='17a50e5838b88f75eb52d10199630118e1a080da59dfa03b81686caef3ed7abe'
PARENT_POLICY_SHA='a63453f42151ed4859b7cb952a6d8d9275a434bc3dee67abfed5f306678916ba'
WRAPPER_SHA='1e53c1623a848069be73c23c88e7fc7a7bf707ca77707e85dd4c41c7c8ec9cec'
NAMES=('controller.py','roster.py','owned.py','child_exec.py','writer_policy.py','post_check.py','effective_class.py')

def v7(s):return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_mode]

def fail(reason):raise WriterUnknown('effective_class:'+str(reason)[:1800])

def document(ref,schema,reader,seals):
    if set(ref)!={'path','sha256'} or not Path(ref['path']).is_absolute() or len(ref['sha256'])!=64:fail('reference_shape')
    raw,seal=reader(ref['path'],limit=1048576,private=True)
    if seal['sha256']!=ref['sha256']:fail('document_hash:'+ref['path'])
    value=json.loads(raw)
    if value.get('schema')!=schema:fail('document_schema:'+ref['path'])
    seals[ref['path']]=seal;return value

def file_versions(profile,tick):
    tick()
    if str(Path(profile['named_root']).resolve(strict=True))!=profile['target_root']:fail('current_root_changed')
    entries={}
    for m in profile['modules']:
        entries[m['source']['path']]=m['source']
        c=m['eligible_cache']
        if c is not None:
            if c.get('absent'):
                if Path(c['path']).exists() or Path(c['path']).is_symlink():fail('cache_appeared:'+c['path'])
            else:entries[c['path']]=c
    for e in profile['loader_files']:entries[e['path']]=e
    for e in entries.values():
        tick();p=Path(e['path']);s=p.lstat()
        if not stat.S_ISREG(s.st_mode) or v7(s)!=e['stat']:fail('source_cache_loader_version:'+str(p))
    for e in profile['required_absences']:
        tick()
        if not e['absent'] or Path(e['path']).exists() or Path(e['path']).is_symlink():fail('customization_appeared:'+e['path'])
    for x in profile['import_selectors']:
        tick();base=Path(x['base'])
        if base.is_symlink():fail('selector_symlink:'+str(base))
        isdir=base.is_dir();s=base.stat() if isdir else None
        identity=[s.st_dev,s.st_ino,s.st_uid,s.st_mode] if s else None
        if {'exists':isdir,'identity':identity}!=x['package_dir']:fail('selector_directory:'+str(base))
        suffixes=x['ordered_suffixes']
        for key,paths in [('package_candidates',[base/('__init__'+s) for s in suffixes]),('module_candidates',[base.parent/(base.name+s) for s in suffixes])]:
            expected={e['path']:e for e in x[key]};found=[]
            for q in paths:
                tick()
                if q.is_symlink():fail('selector_candidate_symlink:'+str(q))
                if q.exists():
                    if str(q) not in expected or not q.is_file() or v7(q.lstat())!=expected[str(q)]['stat']:fail('selector_candidate_changed:'+str(q))
                    found.append(str(q))
            if found!=[e['path'] for e in x[key]]:fail('selector_membership:'+str(base))
    if sorted(p.name for p in Path(profile['site_path']).glob('*.pth'))!=profile['site_pth_names']:fail('pth_membership')
    for row in profile['customization_inventory']:
        root=Path(row['root'])
        for name in ('sitecustomize','usercustomize'):
            if sorted(p.name for p in root.glob(name+'*'))!=row[name+'_matches']:fail('customization_membership:'+str(root))
    for row in profile['stdlib_shadow_inventory']:
        root=Path(row['root']);found=[]
        for name in row['names']:
            for q in [root/(name+'.py'),root/(name+'.pyc'),root/name,*root.glob(name+'.*.so')]:
                if q.exists() or q.is_symlink():found.append(str(q))
        if sorted(found)!=sorted(row['matching_paths']):fail('shadow_membership:'+str(root))
    if Path(profile['stdlib_zip']['path']).exists():fail('stdlib_zip_appeared')
    tick()

def authorize(policy,reader):
    # Source/receipt hashes may be loaded before, NEVER renew fresh capture/HB clocks.
    seals={};ref=policy.get('effective_closure_B_receipt',{})
    b=document(ref,'clarp.effective-loader-class-B/1',reader,seals)
    if (b.get('verdict')!='APPROVED_BOUND_CLASS' or b.get('reviewer')!='rf-review-rest-a79f' or type(b.get('stage')) is not int or b.get('stage')!=3 or not b.get('request_id')):fail('independent_B_receipt_scope')
    if policy.get('effective_closure_B_receipt_sha256')!=ref['sha256']:fail('legacy_receipt_hash_mismatch')
    profile=document(b['profile'],'clarp.readonly-loader-cache-profile/1',reader,seals)
    if b['profile']['sha256']!=PROFILE_SHA or b['producer_source_sha256']!=PRODUCER_SHA or b['parent_policy_sha256']!=PARENT_POLICY_SHA:fail('source_profile_parent_pins')
    parent=document({'path':'/var/tmp/clarp-refactor-program/maintenance/native-qualification-20261109/native-content-policy-r14-20261109.json','sha256':PARENT_POLICY_SHA},'clarp.native-content-policy/1',reader,seals)
    if parent.get('may_consume_as_operative_class_approval') is not False:fail('immutable_parent_policy_changed')
    obs=document(b['observation'],'clarp.readonly-loader-cache-observation/1',reader,seals)
    cap=document(b['capture'],'clarp.loader-native-capture/1',reader,seals)
    if (obs.get('state')!='OBSERVED_SOURCE_CACHE_AND_STARTUP_WITNESSES_FOR_B_STAGE3_ONLY' or obs.get('operative_approval') is not False or obs.get('rc_candidate')!=0 or obs.get('no_app_import') is not True or obs.get('no_app_DB_helper_execution') is not True):fail('observation_not_qualified')
    for key in ('start_mono','end_mono'):
        if type(cap.get(key)) not in (int,float) or not math.isfinite(cap[key]) or cap[key]<=0:fail('typed_capture_clock')
    if type(cap.get('actual_OS_rc')) is not int:fail('typed_OS_returncode')
    if (cap.get('actual_OS_rc')!=0 or cap.get('complete_capture') is not True or cap.get('producer_source_sha256')!=PRODUCER_SHA or cap.get('profile_sha256')!=PROFILE_SHA or not 0<=cap['end_mono']-cap['start_mono']<=90):fail('actual_OS_capture_or_bound')
    if obs.get('profile_sha256')!=PROFILE_SHA or obs['compiler']['exe']!=profile['compiler']['exe'] or obs['compiler']['cache_tag']!=profile['compiler']['cache_tag']:fail('observation_input_ABI')
    if obs['boot']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip() or b['approved_root']!=profile['target_root'] or b['approved_venv']!=profile['compiler']['venv'] or b['library_class']!='split-live-f52' or b['managed_exec_hop_sha256']!=WRAPPER_SHA:fail('root_class_boot_applicability')
    if policy['managed_exec_hop']['sha256']!=WRAPPER_SHA:fail('operative_wrapper_policy')
    rows=obs['compiled_members']
    if len(rows)!=len(profile['modules']):fail('missing_compiled_member')
    for expected,actual in zip(profile['modules'],rows):
        if expected['source']!=actual.get('source'):fail('compiled_source_member')
        cache=expected['eligible_cache']
        state='ENTRYPOINT_SOURCE_ONLY_NO_IMPORT_CACHE' if cache is None else 'NO_ELIGIBLE_NAMED_CACHE_SOURCE_ROUTE' if cache.get('absent') else 'CACHE_CODE_EQUALS_IN_MEMORY_SOURCE_COMPILE'
        if actual['state']!=state or (cache is not None and actual.get('cache')!=cache):fail('cache_comparison_member')
    n=obs['native']
    for key in ('capture_origin_mono','capture_end_mono','capture_deadline_mono'):
        if type(n.get(key)) not in (int,float) or not math.isfinite(n[key]) or n[key]<=0:fail('typed_native_capture_clock')
    if n.get('initial_scan_included') is not True or not 0<=n['capture_end_mono']-n['capture_origin_mono']<15 or n['capture_end_mono']>=n['capture_deadline_mono']:fail('native_capture_deadline')
    base=Path(__file__).parent;mf=base/'SHA256SUMS';raw,seal=reader(mf,limit=65536,private=True)
    if seal['sha256']!=b['controller_MF_sha256']:fail('controller_MF_pin')
    names=[]
    for line in raw.decode().splitlines():
        sha,name=line.split(None,1);name=name.lstrip('*')
        if name not in NAMES:fail('controller_member_name')
        body,s=reader(base/name,limit=1048576,private=True)
        if s['sha256']!=sha:fail('controller_member_hash:'+name)
        seals[str(base/name)]=s;names.append(name)
    if sorted(names)!=sorted(NAMES) or sorted(q.name for q in base.glob('*.py'))!=sorted(NAMES):fail('exact_seven_controller_members')
    if (base/'__pycache__').exists():fail('unqualified_controller_import_cache')
    seals[str(mf)]=seal
    return {'B_receipt':ref,'B_schema':b['schema'],'profile':profile,'profile_ref':b['profile'],'seals':seals,'producer_source_sha256':PRODUCER_SHA,'approved_root':profile['target_root'],'approved_venv':profile['compiler']['venv'],'library_class':'split-live-f52','floor_claim_unchanged':True,'trust_limits':profile['compiler']['trusted_startup_model']}

def check(info,reader,tick):
    for path,seal in info['seals'].items():
        tick();p=Path(path);s=p.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077 or [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]!=seal['version']:fail('receipt_or_controller_source_version:'+path)
    file_versions(info['profile'],tick)

def join_helper(info,code_root,library_class,env,argv,native,tick):
    if code_root!=info['approved_root'] or library_class!=info['library_class']:fail('helper_root_or_content_class:'+code_root)
    p=info['profile']
    if argv[0] not in p['helper_argv0_allowed'] or str(Path(argv[0]).resolve())!=p['compiler']['exe']:fail('helper_interpreter_prefix')
    for k,v in env.items():
        if k.startswith(b'PYTHON') or k.startswith(b'_PYTHON'):
            allowed=p['python_environment_allowed'].get(k.decode())
            if allowed is None or v.decode() not in allowed:fail('helper_loader_env:'+k.decode())
    if any(env.get(k) for k in (b'LD_PRELOAD',b'LD_LIBRARY_PATH',b'LD_AUDIT')):fail('helper_native_loader')
    for key,value in p['helper_context_required'].items():
        if env.get(key.encode(),b'').decode()!=value:fail('helper_context:'+key)
    if env.get(b'SETUPTOOLS_USE_DISTUTILS',b'').decode() not in p['SETUPTOOLS_USE_DISTUTILS_allowed'] or env.get(b'CLARP_TELEMETRY_DB',b'').decode() not in p['telemetry_environment_allowed']:fail('helper_site_or_telemetry_inputs')
    if env.get(b'CLARP_BACKGROUND_WORKER_PID')!=str(native['pid']).encode():fail('helper_worker_selector')
    check(info,None,tick)
    return {'B_receipt':info['B_receipt'],'profile_ref':info['profile_ref'],'method':'INDEPENDENT_STAGE3_CLASS_JOIN_NOT_FLOOR_RELABELLING'}
