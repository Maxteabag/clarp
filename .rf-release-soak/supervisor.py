#!/usr/bin/env python3
"""Buildbox-only, single-admission synthetic D_F canary/soak/compat supervisor."""
import argparse, datetime, hashlib, json, os, shutil, signal, subprocess, sys, time, tarfile, secrets, selectors, math
from pathlib import Path

OWNER = 'rf-releasec-29c5'
TOOL_DIGEST = 'cad642ea27167e355dd570c4ab3c6f11bc83b636b845c983967aaf1cd4f56870'
ROOT = Path(__file__).resolve().parent

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    with temporary.open('x') as out:
        json.dump(data, out, sort_keys=True); out.write('\n'); out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)

def verify_files(root, checks):
    for name, expected in checks.items():
        path = root / name
        if root.resolve() not in path.resolve().parents: raise RuntimeError('capsule path escapes root')
        if digest(path) != expected: raise RuntimeError('capsule bytes changed: ' + name)

class Native:
    def __init__(self, manifest, output):
        self.m, self.output = manifest, output
        now = time.monotonic()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if manifest.get('rehearsal'):
            self.binding = {'nonce':'rehearsal-'+secrets.token_hex(16), 'remote_job_id':'rehearsal-'+str(os.getpid()), 'boot_id':boot}
            remaining = manifest['hard_total_seconds']; absolute = time.time()+remaining
        else:
            if os.environ.get('HEAVY_SOAK_ALLOWANCE') != 'bigmerge1-800':
                raise ValueError('real soak requires reviewed narrow allowance admission')
            required = ('HEAVY_SOAK_NONCE','HEAVY_SOAK_REMOTE_JOB_ID','HEAVY_SOAK_BOOT_ID','HEAVY_SOAK_DEADLINE_UTC','HEAVY_SOAK_REMAINING_OWNED_SECONDS')
            if any(not os.environ.get(k) for k in required):raise ValueError('wrapper admission identity/deadline missing')
            self.binding = {'nonce':os.environ['HEAVY_SOAK_NONCE'],'remote_job_id':os.environ['HEAVY_SOAK_REMOTE_JOB_ID'],'boot_id':os.environ['HEAVY_SOAK_BOOT_ID']}
            if self.binding['boot_id'] != boot:raise ValueError('VM boot changed; no resume/retry')
            remaining = float(os.environ['HEAVY_SOAK_REMAINING_OWNED_SECONDS'])
            stamp = datetime.datetime.fromisoformat(os.environ['HEAVY_SOAK_DEADLINE_UTC'].replace('Z','+00:00'))
            if stamp.tzinfo is None:raise ValueError('absolute deadline needs timezone')
            absolute = stamp.timestamp()
        raw_budgets=(float(manifest['hard_total_seconds']),remaining,absolute-time.time())
        if any(not math.isfinite(v) or v<=0 for v in raw_budgets):raise ValueError('invalid admission budget/deadline')
        budget = min(raw_budgets)
        if budget <= 120:raise ValueError('no owned envelope left after cleanup reserve')
        self.started_monotonic = now; self.hard_deadline = now+budget
        self.deadline = self.hard_deadline-120
        self.container = manifest['container']
        self.env = dict(os.environ, CLARP_SOAK_CONTAINER=self.container,
                        HEAVY_SOAK_NONCE=self.binding['nonce'], HEAVY_SOAK_REMOTE_JOB_ID=self.binding['remote_job_id'], HEAVY_SOAK_BOOT_ID=self.binding['boot_id'])
        self.seq = 0
    def command(self, argv, *, cwd=None, timeout=600, expected=(0,)):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise RuntimeError('owned job envelope exhausted')
        self.seq += 1
        stem = self.output / ('step-%04d' % self.seq)
        end = time.monotonic()+min(timeout,remaining)
        proc = subprocess.Popen(argv,cwd=cwd,env=self.env,stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,start_new_session=True,bufsize=0)
        selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ,'out');selector.register(proc.stderr,selectors.EVENT_READ,'err')
        total=0;limit=8*1024*1024;chunks=[];failure=None
        try:
            with stem.with_suffix('.out').open('wb') as out,stem.with_suffix('.err').open('wb') as err:
                files={'out':out,'err':err}
                while selector.get_map():
                    left=end-time.monotonic()
                    if left<=0:raise RuntimeError('owned step deadline exhausted')
                    for key,_mask in selector.select(min(left,.2)):
                        data=os.read(key.fileobj.fileno(),min(16384,limit-total+1))
                        if not data:selector.unregister(key.fileobj);continue
                        total+=len(data)
                        if total>limit:raise RuntimeError('owned step output exceeds8MiB combined budget')
                        files[key.data].write(data)
                        if key.data=='out':chunks.append(data)
                left=end-time.monotonic()
                if left<=0:raise RuntimeError('owned step deadline exhausted')
                proc.wait(timeout=left)
            if proc.returncode not in expected:raise RuntimeError('step exit %s at %04d' % (proc.returncode,self.seq))
            return b''.join(chunks).decode('utf-8')
        except Exception as error:
            failure=type(error).__name__;print(json.dumps({'step':self.seq,'error_type':failure}),flush=True);raise
        finally:
            selector.close()
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            proc.stdout.close();proc.stderr.close();proc.wait(timeout=2)
            atomic(stem.with_suffix('.json'),{'argv':argv,'exit':proc.returncode,'error_type':failure,
                   't':datetime.datetime.now().astimezone().isoformat(),'binding':self.binding})
    def inside(self, *argv, timeout=600):
        return self.command(['docker','exec','-u','canary','-e','HOME=/home/canary',
            '-e','XDG_RUNTIME_DIR=/run/user/1000','-e','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
            '-w','/home/canary',self.container,*argv], timeout=timeout)
    def adapter(self, role, mode, *flags, expected=(0,)):
        verify_files(ROOT, self.m['harness_sha256'])
        source = Path(self.m['payloads'][role]['path']).resolve()
        verify_files(source, self.m['payloads'][role]['sha256'])
        started = time.monotonic()
        result = self.command(['bash',str(ROOT/'native-vm.sh'),mode,self.m['payloads'][role]['sha'],*flags],
                              cwd=source, timeout=1800, expected=expected)
        atomic(self.output / ('phase-%04d.json' % self.seq), {'role':role,'mode':mode,'flags':flags,
               'input_sha':self.m['payloads'][role]['sha'],'started_monotonic':started,'finished_monotonic':time.monotonic(),'elapsed_seconds':time.monotonic()-started,'binding':self.binding})
        return result
    def status(self):
        return json.loads(self.inside('python3','/home/canary/release-check','--ignore-stt','status',timeout=30))
    def runtime_restart(self, target):
        before = self.inside('systemctl','--user','show','clarp-runtime.service','-p','MainPID','--value').strip()
        self.inside('systemctl','--user','restart','clarp-runtime.service',timeout=60)
        until = min(self.deadline, time.monotonic()+300)
        while time.monotonic() < until:
            state = self.status()
            if time.monotonic() >= until: break
            pid = self.inside('systemctl','--user','show','clarp-runtime.service','-p','MainPID','--value').strip()
            if (state['probe'].get('health_ready') is True and state['probe'].get('runtime_available') is True
                and state['probe'].get('runtime_release_id') == target and state['runtime_on_installed']
                and pid.isdigit() and pid != '0' and pid != before):
                return {'target':target,'before_pid':before,'after_pid':pid,'healthy':True}
            time.sleep(2)
        raise RuntimeError('synthetic runtime target health not proven in300s; no repeat')
    def cleanup(self):
        # Only this harness-labelled container, never a shared image/volume/VM.
        self.deadline = self.hard_deadline
        ids = self.command(['docker','container','ls','-aq','--filter','name=^/'+self.container+'$'],timeout=15).strip()
        if not ids: return
        if len(ids.splitlines()) != 1:raise RuntimeError('cleanup identity ambiguous')
        label = self.command(['docker','inspect','--format','{{index .Config.Labels "clarp.soak-owner"}}',ids],timeout=15).strip()
        if label != OWNER: raise RuntimeError('cleanup ownership unknown; container retained')
        for key,expected in (('clarp.soak-job',self.binding['remote_job_id']),('clarp.soak-nonce',self.binding['nonce'])):
            actual=self.command(['docker','inspect','--format','{{index .Config.Labels \"'+key+'\"}}',ids],timeout=15).strip()
            if actual!=expected:raise RuntimeError('cleanup nonce/job changed; replacement retained')
        self.command(['docker','rm','-f',ids],timeout=30)

def unpack_payloads(manifest):
    for role in ('base','candidate'):
        payload = manifest['payloads'][role]
        archive = Path(payload['archive']).resolve()
        destination = Path(payload['path']).resolve()
        if ROOT.resolve() not in destination.parents or ROOT.resolve() not in archive.parents:
            raise ValueError('payload archive/destination escapes harness capsule')
        if digest(archive) != payload['archive_sha256']:
            raise RuntimeError('frozen source archive bytes changed')
        if destination.exists():
            raise RuntimeError('payload already unpacked; reconcile earlier admission')
        destination.mkdir(parents=True)
        with tarfile.open(archive) as source:
            source.extractall(destination, filter='data')


def validate(manifest):
    if manifest['owner_session'] != OWNER or manifest.get('synthetic_data_only') is not True: raise ValueError('owner/data scope mismatch')
    rehearsal = manifest.get('rehearsal') is True
    if manifest['soak_seconds'] != 21600 and not (rehearsal and 1 <= manifest['soak_seconds'] <= 60): raise ValueError('six hours cannot compress')
    if not 21600 <= manifest['hard_total_seconds'] <= 30000 and not rehearsal: raise ValueError('invalid owned envelope')
    if manifest['tool_manifest_sha256'] != TOOL_DIGEST: raise ValueError('803 tool pin differs')
    tool_manifest=Path(manifest['tool_manifest_payload']).resolve()
    if ROOT.resolve() not in tool_manifest.parents or digest(tool_manifest)!=TOOL_DIGEST:raise ValueError('copied exact tool manifest missing/different')
    for line in tool_manifest.read_text().splitlines():
        h,name=line.split('  ',1)
        if digest(tool_manifest.parent/name)!=h:raise ValueError('copied tool payload bytes changed')
    if not manifest['container'].startswith('clarp-soak-rf-releasec-'): raise ValueError('container not owned')
    for role in ('base','candidate'):
        sha = manifest['payloads'][role]['sha']
        if len(sha)!=40 or any(c not in '0123456789abcdef' for c in sha): raise ValueError('full source pin required')
        verify_files(Path(manifest['payloads'][role]['path']).resolve(),manifest['payloads'][role]['sha256'])
    verify_files(ROOT,manifest['harness_sha256'])
    if not rehearsal and manifest.get('phase')!='full':raise ValueError('70NOK admission only full soak, not canary-only')
    if not rehearsal and not all(manifest['gate_receipts'].get(k) for k in ('G1','G2','CI','G3_G4','freeze585','schema_contract','compute_admission')): raise ValueError('ordered admission receipts incomplete')

def workload(native, mode, seconds=21600):
    args=['python3','/home/canary/soak-workload.py',mode,'--state','/home/canary/soak-state.json']
    if mode=='run':
        args += ['--seconds',str(seconds)]
        if native.m.get('rehearsal'): args += ['--rehearsal']
    return native.inside(*args,timeout=max(240,seconds+30))

def paused_idle(native):
    native.inside('touch','/home/canary/soak-pause-sends')
    until = min(native.deadline,time.monotonic()+180)
    while time.monotonic()<until:
        # Count only the isolated synthetic runtime's workload.
        state=native.status()
        if state['probe'].get('runtime_available') is True and state['probe'].get('active_turns')==0:return
        time.sleep(1)
    raise RuntimeError('synthetic idle boundary not proven; no forced in-flight replay')

def soak(native):
    seconds=native.m['soak_seconds']
    soaked_db=native.inside('stat','-c','%d:%i','/home/canary/.local/share/clarp/state.sqlite').strip()
    native.command(['docker','cp',str(ROOT/'workload.py'),native.container+':/home/canary/soak-workload.py'])
    native.command(['docker','exec',native.container,'chown','canary','/home/canary/soak-workload.py'])
    args=['docker','exec','-d','-u','canary','-e','HOME=/home/canary',native.container,'python3','/home/canary/soak-workload.py','run','--state','/home/canary/soak-state.json','--seconds',str(seconds)]
    if native.m.get('rehearsal'):args+=['--rehearsal']
    native.command(args)
    start=time.monotonic();next_restart=start+7200;next_maintenance=start+3600;next_status=start
    target=native.status()['installed']['runtime_release_id'];boots=[];maintenance=[]
    while True:
        exists=native.inside('bash','-c','if test -f /home/canary/soak-state.result.json; then cat /home/canary/soak-state.result.json; else echo null; fi',timeout=30)
        result=json.loads(exists)
        if result is not None:
            if result.get('result')!='PASS' or result.get('elapsed_seconds',0)<seconds: raise RuntimeError('soak incomplete/failed; no compressed receipt')
            after=native.inside('stat','-c','%d:%i','/home/canary/.local/share/clarp/state.sqlite').strip()
            if result.get('same_db_device_inode')!=soaked_db or after!=soaked_db:raise RuntimeError('same DB before/after soak not proven')
            break
        # A vanished driver without terminal receipt is unknown, never six-hour proof.
        native.inside('bash','-c','if test -f /home/canary/soak-state.started.json; then pid=$(python3 -c '"'"'import json;print(json.load(open("/home/canary/soak-state.started.json"))["pid"])'"'"'); test -f /home/canary/soak-state.result.json || kill -0 "$pid"; fi',timeout=15)
        now=time.monotonic()
        if native.deadline-now < 1800: raise RuntimeError('post-soak compatibility reserve exhausted; no compression')
        if now>=next_status:
            state=native.status()
            if state['probe'].get('status_code')!=200 or state['probe'].get('health_ready') is not True:raise RuntimeError('soak Host health failure')
            with (native.output/'soak-status.jsonl').open('a') as out:out.write(json.dumps({'t':datetime.datetime.now().astimezone().isoformat(),'elapsed':now-start,'probe':state['probe']})+'\n')
            next_status=now+60
        if now>=next_maintenance:
            # Observe the existing Host hourly worker; do not double its writes.
            raw=native.inside('journalctl','--user','-u','clarp.service','--since','-1hour','-n','32','--grep','maintenancePhases|maintenanceFail|maintenanceCheckpointFail','--output=json','--no-pager',timeout=30)
            lines=[json.loads(line) for line in raw.splitlines() if line.strip()]
            phase_count=sum('maintenancePhases' in row.get('MESSAGE','') for row in lines)
            failures=sum('maintenanceFail' in row.get('MESSAGE','') or 'maintenanceCheckpointFail' in row.get('MESSAGE','') for row in lines)
            if not phase_count or failures:raise RuntimeError('ordinary hourly maintenance receipt missing/failed')
            maintenance.append({'elapsed':now-start,'phase_count':phase_count,'failures':failures});next_maintenance+=3600
        if now>=next_restart:
            paused_idle(native);boots.append({'elapsed':now-start,**native.runtime_restart(target)})
            native.inside('rm','/home/canary/soak-pause-sends');next_restart+=7200
        time.sleep(5)
    atomic(native.output/'soak-drills.json',{'runtime_boots':boots,'maintenance':maintenance,'seconds_required':seconds})
    if not native.m.get('rehearsal') and (len(boots)<2 or len(maintenance)<5):
        raise RuntimeError('required two-hour runtime/five hourly maintenance receipts missing')
    atomic(native.output/'soak-result.json',result)
    return result

def compatibility(native, base_release, base_runtime):
    started=time.monotonic();started_up_ns=time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    before=native.inside('stat','-c','%d:%i','/home/canary/.local/share/clarp/state.sqlite').strip()
    soaked=json.loads((native.output/'soak-result.json').read_text())
    if before!=soaked.get('same_db_device_inode'):raise RuntimeError('compatibility DB differs from actual soaked DB')
    checks=[{'phase':'D_F_soaked_read','result':json.loads(workload(native,'verify'))}]
    native.inside('bash','-c','old="$HOME/.local/share/clarp/releases/$1"; py=$(cat "$old/SERVICE_PYTHON"); CLARP_CODE_ROOT="$old" "$py" "$old/bin/clarp-admin.py" rollback "$1"','bash',base_release,timeout=120)
    native.runtime_restart(base_runtime)
    checks.append({'phase':'base_reads_soaked_D_F_and_writes','result':json.loads(workload(native,'compat-send'))})
    native.adapter('candidate','release')
    checks.append({'phase':'D_F_reads_base_and_writes','result':json.loads(workload(native,'compat-send'))})
    after=native.inside('stat','-c','%d:%i','/home/canary/.local/share/clarp/state.sqlite').strip()
    if before!=after or not all(c['result'].get('ok') is True for c in checks): raise RuntimeError('same-soaked-DB compatibility not proven')
    atomic(native.output/'forward-back-D_F.json',{'same_db_device_inode':before,'checks':checks,'result':'PASS','start_up_ns':started_up_ns,'end_up_ns':time.clock_gettime_ns(time.CLOCK_BOOTTIME),'started_monotonic':started,'finished_monotonic':time.monotonic(),'binding':native.binding})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',type=Path,required=True);args=parser.parse_args();manifest=json.loads(args.manifest.read_text())
    output=Path(manifest['output']).resolve()
    if ROOT.resolve() not in output.parents:raise ValueError('output escapes harness-owned directory')
    native=Native(manifest,output)  # admission/deadline before capsule setup
    unpack_payloads(manifest);validate(manifest)
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (output/'admission-started.json').open('x') as out:json.dump({'manifest_sha256':digest(args.manifest),'job_id':manifest['job_id'],'started':datetime.datetime.now().astimezone().isoformat(),'started_monotonic':native.started_monotonic,'binding':native.binding},out)
    status='FAIL';failure=None
    def interrupted(signum,_frame):raise RuntimeError('owned job interrupted signal '+str(signum)+'; no retry/resume')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    try:
        if shutil.disk_usage('/').free<25_000_000_000:raise RuntimeError('fresh capacity below25GB')
        existing=native.command(['docker','container','ls','-aq','--filter','name=^/'+native.container+'$']).strip()
        if existing:raise RuntimeError('preexisting container: reconcile uncertain prior admission')
        canary_started=time.monotonic();canary_started_up_ns=time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        for mode,flags,expected in [('release',[],(0,)),('handover-turns',[],(0,)),('release',['--plant-late-fail-lib'],(10,)),('release',['--plant-fail','--no-safety-net'],(12,))]:
            native.adapter('base','base');native.adapter('candidate',mode,*flags,expected=expected)
        atomic(output/'canary-four-cases.json',{'result':'PASS','source':manifest['payloads']['candidate']['sha'],'all_four_cases':True,'start_up_ns':canary_started_up_ns,'end_up_ns':time.clock_gettime_ns(time.CLOCK_BOOTTIME),'started_monotonic':canary_started,'finished_monotonic':time.monotonic(),'binding':native.binding})
        native.adapter('base','base');base=native.status()['installed'];native.adapter('candidate','release')
        # Preserve enough envelope for all21600seconds, compatibility and cleanup.
        if not manifest.get('rehearsal') and native.deadline-time.monotonic()<21600+1800+660:raise RuntimeError('setup left insufficient full-soak/postcheck budget')
        soak(native);compatibility(native,base['release_id'],base['runtime_release_id']);status='PASS'
    except Exception as error:
        failure={'error_type':type(error).__name__,'reason':str(error)};print(json.dumps({'result':'FAIL',**failure}),flush=True)
    finally:
        cleanup_status='PASS';cleanup_started_up_ns=time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        try:native.cleanup()
        except Exception as error:
            cleanup_status='FAIL';status='FAIL';failure={'error_type':type(error).__name__,'reason':'owned cleanup unresolved: '+str(error)};print(json.dumps(failure),flush=True)
        cleanup_ended_up_ns=time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        try:
            canary=json.loads((output/'canary-four-cases.json').read_text())
            soaked=json.loads((output/'soak-result.json').read_text())
            compat=json.loads((output/'forward-back-D_F.json').read_text())
            proof={'nonce':native.binding['nonce'],'remote_job':native.binding['remote_job_id'],'boot':native.binding['boot_id'],'cleanup':cleanup_status,'same_db_device_inode':compat['same_db_device_inode'],'phases':[
                {'phase':'canary','start_up_ns':canary['start_up_ns'],'end_up_ns':canary['end_up_ns']},
                {'phase':'soak','start_up_ns':soaked['start_up_ns'],'end_up_ns':soaked['end_up_ns']},
                {'phase':'compatibility','start_up_ns':compat['start_up_ns'],'end_up_ns':compat['end_up_ns']},
                {'phase':'cleanup','start_up_ns':cleanup_started_up_ns,'end_up_ns':cleanup_ended_up_ns}]}
            for previous,current in zip(proof['phases'],proof['phases'][1:]):
                if previous['end_up_ns']>current['start_up_ns']:raise RuntimeError('phase ordering violated')
            if any(p['end_up_ns']<p['start_up_ns'] for p in proof['phases']):raise RuntimeError('phase clock moved backwards')
            if soaked['end_up_ns']-soaked['start_up_ns']<manifest['soak_seconds']*10**9:raise RuntimeError('short soak phase proof')
            atomic(output/'compute-phase-proof.json',proof)
        except Exception as error:
            status='FAIL';failure={'error_type':type(error).__name__,'reason':'complete ordered phase proof unavailable: '+str(error)};print(json.dumps(failure),flush=True)
        atomic(output/'terminal.json',{'result':status,'candidate':manifest['payloads']['candidate']['sha'],'base':manifest['payloads']['base']['sha'],'soak_seconds':manifest['soak_seconds'],'rehearsal':manifest.get('rehearsal',False),'failure':failure,'cleanup_status':cleanup_status,'finished_monotonic':time.monotonic(),'owned_started_monotonic':native.started_monotonic,'binding':native.binding})
    return 0 if status=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
