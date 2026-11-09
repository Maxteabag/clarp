"""Private pre-stop producer. Reads metadata only; never starts/signals a job.

Policy file/image bindings require final native B review. Unknown effective helper
lineage refuses; a caller class label or a cached registry row is not evidence.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from owned import DispatchPermit
from writer_policy import (WriterUnknown, validate_workers, validate_heartbeat_age,
                           require_same_worker, classify_fd, version_of, context_of)

def proc_bytes(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        value=os.read(fd,limit+1)
        if len(value)>limit:raise WriterUnknown('proc_metadata_over_cap')
        return value
    finally:os.close(fd)


MAX_WORKERS = 64
CAPTURE_SECONDS = 15
ADMISSION_AGE = 2



def heartbeat_guard(workers, clock):
    now_mono=time.monotonic();now_wall_ms=int(time.time()*1000)
    # Bracketed wall/mono anchor plus100ms conservative margin; a discontinuity
    # observed at either final capture or dispatch refuses rather than renewing.
    if abs((now_wall_ms-clock['wall_ms'])/1000-(now_mono-clock['monotonic']))>.05:
        raise WriterUnknown('heartbeat_clock_mapping_changed')
    try:validate_heartbeat_age(workers,now_wall_ms)
    except WriterUnknown as exc:raise WriterUnknown('heartbeat_expired_or_unqualified_before_stop_admission') from exc
    if now_mono>=clock['expiry']:
        raise WriterUnknown('heartbeat_expired_before_stop_admission')


class FirstStopPermit(DispatchPermit):
    def check(self):
        check_admission(self.owner) # actual Owned parent check AFTER setup too
        super().check()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_bound(path, expected=None, limit=1024*1024, private=False):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        a=os.fstat(fd)
        if (not stat.S_ISREG(a.st_mode) or not 0<=a.st_size<=limit
                or a.st_uid not in (0,os.getuid()) or a.st_mode&0o022
                or (private and (a.st_uid!=os.getuid() or a.st_mode&0o077))):
            raise WriterUnknown('bound_file_type_size_owner_mode')
        raw=os.read(fd,limit+1);b=os.fstat(fd);named=path.stat(follow_symlinks=False)
        if len(raw)>limit or len(raw)!=a.st_size or version_of(a)!=version_of(b) or version_of(b)!=version_of(named):
            raise WriterUnknown('bound_file_rebound')
        seal={'path':str(path),'version':version_of(b),'sha256':hashlib.sha256(raw).hexdigest()}
        if expected is not None and (expected['version']!=seal['version'] or expected['sha256']!=seal['sha256']):
            raise WriterUnknown('bound_file_version_hash')
        return raw,seal
    finally:os.close(fd)


def load_receipt(path, sha256):
    raw,_=read_bound(path,limit=1024*1024,private=True)
    if hashlib.sha256(raw).hexdigest()!=sha256:raise WriterUnknown('roster_receipt_hash')
    value=json.loads(raw)
    if value.get('schema')!='clarp.current-active-roster/1':raise WriterUnknown('roster_receipt_schema')
    b=value['controller_binding']
    if b['tolerated_workers']!=value['active_workers']:raise WriterUnknown('roster_handoff_changed')
    validate_workers(value['active_workers'],b['auxiliary_units']+b['timers']+['clarp.service','clarp-runtime.service'])
    return value


class Producer:
    def __init__(self,owner):
        self.o=owner;self.started=time.monotonic();self.deadline=self.started+CAPTURE_SECONDS
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.policy=owner.b['roster_policy']
        if digest(self.policy)!=owner.b['roster_policy_sha256'] or not owner.b['roster_policy_review']:
            raise WriterUnknown('native_source_policy_unqualified')

    def tick(self):
        if (self.o.abort_requested or time.monotonic()>=self.deadline
                or Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=self.boot):
            raise WriterUnknown('roster_capture_expired_aborted_or_boot_changed')

    def names(self):
        self.tick()
        raw=self.o.require(['/usr/bin/systemctl','--user','list-units','--type=service',
                            '--state=active,activating','--no-legend','--plain','--no-pager'])
        names=[line.split()[0] for line in raw.splitlines() if line.split() and line.split()[0].startswith('clarp-job-')]
        if not 0<len(names)<=MAX_WORKERS or len(set(names))!=len(names) or any(not n.endswith('.service') for n in names):
            raise WriterUnknown('current_roster_missing_duplicate_or_over_cap')
        return sorted(names)

    def native(self,name):
        self.tick();first=self.o.unit(name);pid=int(first.get('MainPID') or 0)
        if first.get('ActiveState')!='active' or pid<=0 or not first.get('InvocationID') or not first.get('ControlGroup'):
            raise WriterUnknown('current_worker_not_positive_active')
        p=Path('/proc')/str(pid);a=self.o.process_observation(p);b=self.o.process_observation(p);last=self.o.unit(name)
        if a!=b or first!=last or a['uid']!=os.getuid() or a['boot']!=self.boot or a['cgroup']!=first['ControlGroup']:
            raise WriterUnknown('current_native_rebound')
        return dict(a,unit=name,invocation=first['InvocationID'],state='active')

    def registry(self, natives):
        self.tick();db=self.o.protected_root/'state.sqlite';a=db.stat()
        expected=self.o.b['protected_db_files']['state.sqlite']
        if (a.st_dev,a.st_ino)!=(expected['dev'],expected['inode']):raise WriterUnknown('roster_db_changed')
        pids=','.join(str(int(w['pid'])) for w in natives.values())
        sql=('PRAGMA query_only=ON;BEGIN;SELECT job_id,session,generation,status,heartbeat_at,'
             'worker_pid,worker_start_token,terminal_reason,log_path FROM background_jobs '
             'WHERE worker_pid IN ('+pids+');COMMIT;')
        raw=self.o.require(['/usr/bin/sqlite3','-batch','-bail','-readonly','-json','-cmd','.timeout 2000',str(db),sql],10)
        z=db.stat()
        if (z.st_dev,z.st_ino)!=(a.st_dev,a.st_ino):raise WriterUnknown('roster_db_rebound')
        rows=json.loads(raw)
        if not isinstance(rows,list) or len(rows)!=len(natives):raise WriterUnknown('registry_join_incomplete_or_duplicate')
        result={}
        for name,n in natives.items():
            found=[r for r in rows if r.get('worker_pid')==n['pid'] and r.get('worker_start_token')==n['boot']+':'+n['start']]
            if len(found)!=1 or not found[0].get('session') or not found[0].get('job_id'):
                raise WriterUnknown('registry_worker_identity_unknown')
            result[name]=found[0]
        return result,hashlib.sha256(raw.encode()).hexdigest()

    def pids(self,native):
        directory=Path('/sys/fs/cgroup'+native['cgroup'])
        result=[]
        count=0
        for p in directory.rglob('cgroup.procs'):
            self.tick();count+=1
            if count>128:raise WriterUnknown('unit_subgroup_cap')
            if p.is_symlink():raise WriterUnknown('cgroup_alias')
            values=p.read_text().split();result.extend(int(x) for x in values)
            if len(result)>512:raise WriterUnknown('unit_process_cap')
        return sorted(set(result))

    def worker_image(self,native):
        self.tick();p=Path('/proc')/str(native['pid'])
        before=self.o.process_observation(p)
        argv=proc_bytes(p/'cmdline',16384)
        if len(argv)>16384:raise WriterUnknown('worker_argv_cap')
        parts=[x.decode() for x in argv.rstrip(b'\0').split(b'\0')]
        if len(parts)!=5 or parts[0] not in ('/bin/bash','/usr/bin/bash') or parts[2]!='__worker':
            raise WriterUnknown('not_source_qualified_worker_argv')
        exe=str((p/'exe').resolve(strict=True))
        if exe!=self.policy['worker_exe']['path']:raise WriterUnknown('worker_bash_executable_unknown')
        _,exe_seal=read_bound(exe,self.policy['worker_exe'],limit=32*1024**2)
        image=str(Path(parts[1]).resolve(strict=True));expected=self.policy['worker_images'].get(image)
        if expected is None:raise WriterUnknown('worker_source_version_unqualified')
        _,seal=read_bound(image,expected)
        loaded=p/'fd'/'255';v=loaded.stat();raw=proc_bytes(loaded,1024*1024);after=loaded.stat()
        if (version_of(v)!=version_of(after) or version_of(v)!=seal['version']
                or hashlib.sha256(raw).hexdigest()!=seal['sha256']):
            raise WriterUnknown('loaded_worker_script_not_bound_image')
        if before!=self.o.process_observation(p) or any(before[k]!=native[k] for k in ('pid','start','uid','boot','cgroup')):
            raise WriterUnknown('loaded_worker_identity_changed')
        if str((p/'exe').resolve(strict=True))!=exe:raise WriterUnknown('worker_executable_changed')
        return {'image':seal,'exe':exe_seal,'argv_sha256':hashlib.sha256(argv).hexdigest(),'loaded_fd':255}

    def helper(self,pid,native,row):
        self.tick();p=Path('/proc')/str(pid);before=self.o.process_observation(p)
        if before['uid']!=native['uid'] or before['cgroup']!=native['cgroup']:return None
        fields=(p/'stat').read_text().rsplit(')',1)[1].split()
        if int(fields[1])!=native['pid']:return None
        raw=proc_bytes(p/'cmdline',16384)
        if len(raw)>16384:raise WriterUnknown('helper_argv_cap')
        argv=[x.decode() for x in raw.rstrip(b'\0').split(b'\0')]
        if len(argv)!=5 or argv[3]!='job-active':return None
        script=str(Path(argv[1]).resolve(strict=True));policy=self.policy['helper_images'].get(script)
        if policy is None:raise WriterUnknown('effective_agent_bg_unqualified')
        handle='bg1:'+str(row['generation'])+':'+row['job_id']
        if argv[2]!=row['session'] or argv[4]!=handle:raise WriterUnknown('helper_context_mismatch')
        exe=str((p/'exe').resolve(strict=True))
        if str(Path(argv[0]).resolve(strict=True))!=exe or exe!=policy['exe']['path']:
            raise WriterUnknown('helper_python_unqualified')
        _,exe_seal=read_bound(exe,policy['exe'],limit=32*1024**2)
        env_raw=proc_bytes(p/'environ',131072)
        if len(env_raw)>131072:raise WriterUnknown('helper_environment_cap')
        env=dict(x.split(b'=',1) for x in env_raw.split(b'\0') if b'=' in x)
        if env.get(b'CLARP_BACKGROUND_WORKER_PID')!=str(native['pid']).encode('ascii'):
            raise WriterUnknown('helper_effective_worker_pid_unknown_or_changed')
        if any(env.get(k) for k in (b'PYTHONPATH',b'PYTHONHOME',b'PYTHONSTARTUP')):
            raise WriterUnknown('helper_python_override_unknown')
        code_root=str(Path(env[b'CLARP_CODE_ROOT'].decode()).resolve(strict=True))
        if code_root!=policy['code_root'] or str(Path(env[b'CLAUDE_PWA_DB'].decode()).resolve(strict=True))!=str((self.o.protected_root/'state.sqlite').resolve(strict=True)):
            raise WriterUnknown('helper_effective_root_or_db_unknown')
        required={script,str(Path(code_root)/'lib/background_jobs.py'),str(Path(code_root)/'lib/db.py'),str(Path(code_root)/'lib/timing.py'),str(Path(code_root)/'lib/background_job_process.py')}
        if not required<=set(policy['files']):raise WriterUnknown('helper_dependency_closure_incomplete')
        seals={name:read_bound(name,binding)[1] for name,binding in policy['files'].items()}
        if (before!=self.o.process_observation(p)
                or int((p/'stat').read_text().rsplit(')',1)[1].split()[1])!=native['pid']
                or proc_bytes(p/'cmdline',16384)!=raw or proc_bytes(p/'environ',131072)!=env_raw
                or str((p/'exe').resolve(strict=True))!=exe):
            raise WriterUnknown('helper_incarnation_or_selector_changed')
        return {'native':before,'ppid':native['pid'],'context':{'job_id':row['job_id'],'session':row['session'],'generation':row['generation']},
                'argv_sha256':hashlib.sha256(raw).hexdigest(),'exe':exe_seal,'source_files':seals,
                'environment_sha256':hashlib.sha256(env_raw).hexdigest(),'effective_code_root':code_root,
                'effective_worker_pid':native['pid'],'effective_worker_start_token':native['boot']+':'+native['start'],
                'PATH_sha256':hashlib.sha256(env.get(b'PATH',b'')).hexdigest(),
                'CLARP_AGENT_BG_sha256':hashlib.sha256(env.get(b'CLARP_AGENT_BG',b'')).hexdigest()}

    def collect(self):
        names=self.names();native={name:self.native(name) for name in names}
        initial,initial_hash=self.registry(native)
        workers=[dict(native[n],phase='ACTIVE',job_id=initial[n]['job_id'],owner=initial[n]['session'],generation=initial[n]['generation'],pre_registry=initial[n]) for n in names]
        validate_workers(workers,self.o.b['auxiliary_units']+self.o.b['timers']+['clarp.service','clarp-runtime.service'])
        images={n:self.worker_image(native[n]) for n in names};witnesses={}
        # One finite passive capture of natural helper executions; no helper launch,
        # heartbeat mutation, retry of failed production action or rebind loop.
        for _ in range(600):
            self.tick()
            for name in names:
                if name in witnesses:continue
                for pid in self.pids(native[name]):
                    try:value=self.helper(pid,native[name],initial[name])
                    except FileNotFoundError:continue # a short-lived non-witness ended
                    if value is not None:witnesses[name]=value;break
            if len(witnesses)==len(names):break
            time.sleep(.02)
        if len(witnesses)!=len(names):raise WriterUnknown('effective_helper_witness_UNKNOWN')
        fds=[];byname={w['unit']:w for w in workers};protected=self.o.b['protected_db_files']
        for role,expected in protected.items():
            s=(self.o.protected_root/role).stat()
            if (s.st_dev,s.st_ino)!=(expected['dev'],expected['inode']):raise WriterUnknown('roster_protected_db_changed')
        for name in names:
            for pid in self.pids(native[name]):
                self.tick();p=Path('/proc')/str(pid)
                try:
                    for fd in (p/'fd').iterdir():
                        try:s=fd.stat()
                        except FileNotFoundError:continue
                        roles=[role for role,v in protected.items() if (s.st_dev,s.st_ino)==(v['dev'],v['inode'])]
                        if len(roles)>1:raise WriterUnknown('roster_ambiguous_fd')
                        if roles:
                            record=self.o.capture_fd(p,fd,roles[0]);matched=classify_fd(record,byname,native,protected)
                            if matched!=name:raise WriterUnknown('roster_fd_unit_mismatch')
                            fds.append(dict(record,unit=matched))
                except FileNotFoundError:continue
        anchor_mono=time.monotonic();anchor_wall_ms=int(time.time()*1000)
        fresh,fresh_hash=self.registry(native);now_ms=int(time.time()*1000)
        for w in workers:
            w['pre_registry']=fresh[w['unit']]
            if (w['job_id'],w['owner'],w['generation'])!=(w['pre_registry']['job_id'],w['pre_registry']['session'],w['pre_registry']['generation']):
                raise WriterUnknown('registry_generation_changed')
            log=Path(w['pre_registry']['log_path']+'.exit')
            if not log.is_absolute():raise WriterUnknown('log_path_unbound')
            try:_,seal=read_bound(log,limit=32);w['log_exit']={'path':str(log),'pre_state':'HISTORICAL','historical':dict(seal,context=context_of(w))}
            except FileNotFoundError:w['log_exit']={'path':str(log),'pre_state':'ABSENT'}
        validate_heartbeat_age(workers,now_ms)
        clock={'monotonic':anchor_mono,'wall_ms':anchor_wall_ms,
               'expiry':min(anchor_mono+(w['pre_registry']['heartbeat_at']+60000-anchor_wall_ms)/1000-.1 for w in workers),
               'reserve_seconds':.1,'mapping_max_skew_seconds':.05}
        for name in names:require_same_worker(byname[name],self.native(name))
        if self.names()!=names:raise WriterUnknown('current_roster_changed')
        for name in names:
            if self.worker_image(native[name])!=images[name]:raise WriterUnknown('worker_image_changed')
            witness=witnesses[name]
            read_bound(witness['exe']['path'],witness['exe'],limit=32*1024**2)
            for path,seal in witness['source_files'].items():read_bound(path,seal)
        self.tick();heartbeat_guard(workers,clock)
        return workers,{'native':native,'worker_images':images,'effective_helper_witnesses':witnesses,'db_fds':fds,
                        'initial_registry_sha256':initial_hash,'fresh_registry_sha256':fresh_hash,'fresh_registry_at_ms':now_ms,'heartbeat_clock':clock}


def admit(owner):
    original=copy.deepcopy(owner.b);producer=Producer(owner);previous=owner.command_deadline
    owner.command_deadline=producer.deadline
    try:workers,evidence=producer.collect()
    finally:owner.command_deadline=previous
    frozen=copy.deepcopy(owner.b);frozen['tolerated_workers']=workers
    ended=time.monotonic();epoch=int(time.time()*1000)
    value={'schema':'clarp.current-active-roster/1','boot':producer.boot,'start':producer.started,'end':ended,'end_epoch_ms':epoch,
           'policy_sha256':digest(producer.policy),'original_binding_sha256':digest(original),
           'controller_binding':frozen,'active_workers':workers,'capture':evidence,
           'retired_history':original['retired_worker_history'],'source_hashes':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ('controller.py','roster.py','owned.py','child_exec.py','writer_policy.py','post_check.py')}}
    raw=(json.dumps(value,sort_keys=True)+'\n').encode()
    if len(raw)>1024*1024:raise WriterUnknown('roster_receipt_over_cap')
    path=owner.e/'current-active-roster.json';fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    producer.tick();heartbeat_guard(workers,evidence['heartbeat_clock'])
    if time.monotonic()-ended>ADMISSION_AGE:raise WriterUnknown('roster_receipt_age_before_stop')
    owner.b=frozen;owner.roster_admission={'boot':producer.boot,'end':ended,
                                        'deadline':min(ended+ADMISSION_AGE,evidence['heartbeat_clock']['expiry']),
                                        'heartbeat_clock':evidence['heartbeat_clock'],
                                        'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'binding_sha256':digest(frozen)}
    return owner.roster_admission


def check_admission(owner):
    value=owner.roster_admission
    if value:heartbeat_guard(owner.b['tolerated_workers'],value['heartbeat_clock'])
    if (not value or value['boot']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or owner.abort_requested or digest(owner.b)!=value['binding_sha256']
            or not 0<=time.monotonic()-value['end']<ADMISSION_AGE):
        raise WriterUnknown('current_roster_stop_admission_stale_or_unbound')
