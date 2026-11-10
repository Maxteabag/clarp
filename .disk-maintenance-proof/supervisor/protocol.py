"""SOURCE ONLY: unprivileged manager/effect-gate protocol; no app imports."""
import hashlib,json,math,os,re,secrets,signal,stat,subprocess,sys,time,traceback
from pathlib import Path
BASE=Path('/var/tmp/clarp-refactor-program/maintenance/e26-loader-cache-producer-r1')
PROGRAM=BASE.parents[1]
ROOT=BASE/'manager-residual-source-r4/supervisor'
CAPTURE=Path('/var/tmp/clarp-refactor-program/maintenance/controller-external-capture-20261110-r6')
PREFIX='clarp-disk-maintenance-rf-releasec-20261110-'
SCALAR={'s':str,'b':bool,'u':int,'t':int}
class Refused(RuntimeError):pass

def version(s):return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_mode]
def canonical(path):
    p=Path(path)
    if not p.is_absolute() or p.resolve(strict=True)!=p or not p.is_relative_to(PROGRAM.resolve(strict=True)):raise Refused('noncanonical_program_path')
    return p

def read_private(path,sha=None,cap=1048576):
    p=Path(path);fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        a=os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_uid!=os.getuid() or a.st_mode&0o077 or not 0<a.st_size<=cap:raise Refused('private_input_type_mode_size:'+str(p))
        with os.fdopen(fd,'rb',closefd=False) as f:raw=f.read(cap+1)
        z=os.fstat(fd);digest=hashlib.sha256(raw).hexdigest()
        if len(raw)!=a.st_size or version(a)!=version(z) or version(z)!=version(p.lstat()) or (sha is not None and sha!=digest):raise Refused('private_input_rebound:'+str(p))
        return raw,{'path':str(p),'sha256':digest,'version':version(z)}
    finally:os.close(fd)

def write_new(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,sort_keys=True,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())

def present(path):
    try:Path(path).lstat();return True
    except FileNotFoundError:return False

def boot():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
def native(pid):
    p=Path('/proc')/str(pid);raw=(p/'stat').read_bytes();fields=raw.decode().rsplit(')',1)[1].split()
    cg=(p/'cgroup').read_text().strip()
    if not cg.startswith('0::') or '\n' in cg:raise Refused('native_cgroup_shape')
    argv=(p/'cmdline').read_bytes()
    if len(raw)>8192 or len(argv)>65536:raise Refused('native_metadata_size')
    return {'pid':pid,'start':fields[19],'uid':p.stat().st_uid,'boot':boot(),'cgroup':cg[3:],
            'exe':str((p/'exe').resolve(strict=True)),'argv_sha256':hashlib.sha256(argv).hexdigest()}

def binding_check(raw):
    b=json.loads(raw);now=time.time()
    residual=b.get('manager_fd_residual_policy')
    peter_card='decision-0795e2c8e5ebd591f6c242a8'
    if residual is not None:
        if (not isinstance(residual,dict) or set(residual)!={'path','sha256'}
            or type(residual['path']) is not str or not Path(residual['path']).is_absolute()
            or type(residual['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',residual['sha256'])
            or b.get('writer_inventory_reconciled') is not False
            or b.get('writer_inventory_accounting')!='ACCOUNTED_EXCEPT_OWNER_ACCEPTED_MANAGER_RESIDUAL'):
            raise Refused('exact_manager_residual_binding_required')
        peter_card='decision-c147190cebf96302dfe418d4'
    if (b['schema']!='clarp.disk-maintenance-binding/1' or b['peter_decision']!=peter_card
        or b['peter_yes'] is not True or not b['lead_go_receipt'] or b['pre_state']!='ZERO'
        or not b['window_start_epoch']<=now<=b['window_end_epoch'] or not 0<=now-b['pre_end_epoch']<=120):raise Refused('actual_current_approved_binding_required')
    return b

def tick(e,admission=False):
    observed_boot=boot();now=time.monotonic();limit=e['admit_deadline'] if admission else e['hard_deadline']
    if observed_boot!=e['boot'] or now>=limit:raise Refused('boot_or_absolute_admission_deadline' if admission else 'boot_or_absolute_hard_deadline')
    return now

def envelope_read(path,sha):
    raw,seal=read_private(path,sha);e=json.loads(raw)
    if e['schema']!='clarp.controller-effect-envelope/1' or not re.fullmatch('[0-9a-f]{24}',e['nonce']) or e['unit']!=PREFIX+e['nonce']+'.service':raise Refused('effect_envelope_identity')
    for k in ('origin','admit_deadline','soft_deadline','hard_deadline'):
        if type(e[k]) not in (int,float) or not math.isfinite(e[k]) or e[k]<=0:raise Refused('typed_absolute_clock')
    if (e['admit_deadline']!=e['origin']+80 or e['soft_deadline']!=e['origin']+780 or e['hard_deadline']!=e['origin']+900
        or e['unit_runtime_us']!=700000000 or e['unit_stop_us']!=120000000):raise Refused('exact_common_budget')
    for key in ('binding','loader','gate','protocol','supervisor','B_final'):
        ref=e[key];_,actual=read_private(canonical(ref['path']),ref['sha256'])
        if actual!=ref:raise Refused('envelope_source_or_input_version:'+key)
    if e['origin']>time.monotonic():raise Refused('future_monitor_origin')
    e['envelope_sha256']=seal['sha256'] # runtime-only metadata; not encoded into hashed envelope
    return e,seal

def object_path(unit):return '/org/freedesktop/systemd1/unit/'+''.join(c if c.isascii() and c.isalnum() else '_'+format(ord(c),'02x') for c in unit)
def decode_bus(raw,sig):
    obj=json.loads(raw)
    if obj.get('type')!=sig:raise Refused('manager_reply_signature')
    d=obj['data']
    if sig=='ay':
        if isinstance(d,list) and len(d)==1 and isinstance(d[0],list):d=d[0]
        if not isinstance(d,list) or len(d)!=16 or any(type(v) is not int or not 0<=v<=255 for v in d):raise Refused('manager_invocation_type')
        return bytes(d).hex()
    if isinstance(d,list) and len(d)==1:d=d[0]
    if type(d) is not SCALAR[sig]:raise Refused('manager_scalar_type')
    return d

def run_read(argv,e,capture,label):
    tick(e);r=subprocess.run(argv,capture_output=True,text=True,timeout=min(3,max(.001,e['hard_deadline']-time.monotonic())))
    if len(r.stdout)>65536 or len(r.stderr)>65536:raise Refused('manager_output_overcap')
    write_new(capture/label,{'argv':argv,'rc':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'mono':time.monotonic()})
    tick(e)
    if r.returncode:raise Refused('manager_query_unknown:'+label)
    return r.stdout

def manager(unit,e,capture,serial):
    specs=(('Unit','ActiveState','s'),('Unit','SubState','s'),('Unit','InvocationID','ay'),('Unit','Description','s'),
           ('Unit','ActiveEnterTimestampMonotonic','t'),('Unit','Transient','b'),('Service','MainPID','u'),('Service','ControlGroup','s'),
           ('Service','Type','s'),('Service','KillMode','s'),('Service','RuntimeMaxUSec','t'),('Service','TimeoutStopUSec','t'),
           ('Service','ExecMainStartTimestampMonotonic','t'))
    result={}
    for i,(interface,key,sig) in enumerate(specs):
        argv=['/usr/bin/busctl','--user','--json=short','get-property','org.freedesktop.systemd1',object_path(unit),'org.freedesktop.systemd1.'+interface,key]
        result[key]=decode_bus(run_read(argv,e,capture,'manager-%03d-%02d.json'%(serial,i)),sig)
    return result

def attribution(e,ready,m,now_native):
    if (ready['schema']!='clarp.controller-gate-ready/1' or ready['nonce']!=e['nonce'] or ready['unit']!=e['unit']
        or ready['envelope_sha256']!=e['envelope_sha256'] or ready['boot']!=e['boot']
        or type(ready.get('mono')) not in (int,float) or not math.isfinite(ready['mono']) or not e['origin']<=ready['mono']<=time.monotonic()
        or ready['native']!=now_native or now_native['uid']!=os.getuid() or now_native['boot']!=e['boot']
        or not now_native['cgroup'].endswith('/'+e['unit']) or ready['source_pins']!={k:e[k]['sha256'] for k in ('binding','loader','gate','protocol','supervisor')}
        or m['MainPID']!=now_native['pid'] or m['ControlGroup']!=now_native['cgroup']
        or m['InvocationID']!=ready['invocation'] or not re.fullmatch('[0-9a-f]{32}',ready['invocation'])
        or m['Description']!='Clarp maintenance gate '+e['nonce'] or m['Transient'] is not True
        or m['Type']!='exec' or m['KillMode']!='mixed' or m['RuntimeMaxUSec']!=700000000 or m['TimeoutStopUSec']!=120000000):raise Refused('new_unit_not_positively_attributed')
    return {'unit':e['unit'],'invocation':ready['invocation'],'native':now_native,'nonce':e['nonce']}

def effect_admission(e,ready,m,current):
    tick(e,True);owned=attribution(e,ready,m,current)
    now=time.monotonic()
    if m['ActiveState']!='active' or m['SubState']!='running':raise Refused('attributed_unit_not_running')
    for key in ('ActiveEnterTimestampMonotonic','ExecMainStartTimestampMonotonic'):
        value=m[key]/1e6
        if not e['origin']<=value<=now or value>=e['admit_deadline']:raise Refused('activation_exceeds_common_admission')
    # Accepted activation <=origin80 +unit700 +stop120 <=origin900.
    binding_raw,_=read_private(e['binding']['path'],e['binding']['sha256']);binding_check(binding_raw)
    return owned

class Coordinator:
    def __init__(self,e,capture):
        self.e=e;self.capture=capture;self.abort=False;self.abort_reason=None;self.cancel_attempted=False;self.cancel_sent=False
        self.owned=None;self.client=None;self.dispatched=False;self.granted=False;self.errors=[];self.serial=0
        self.client_out=None;self.client_err=None
    def abort_latch(self,reason):
        self.abort=True;self.abort_reason=self.abort_reason or reason
        try:
            if not present(self.capture/'ABORT.json'):write_new(self.capture/'ABORT.json',{'nonce':self.e['nonce'],'reason':self.abort_reason,'mono':time.monotonic()})
        except BaseException:self.errors.append('abort_marker:'+traceback.format_exc())
    def cancel_owned(self):
        if self.owned is None or self.cancel_attempted:return
        self.cancel_attempted=True
        self.serial+=1;m=manager(self.e['unit'],self.e,self.capture,self.serial)
        n=native(self.owned['native']['pid'])
        if m['InvocationID']!=self.owned['invocation'] or m['MainPID']!=n['pid'] or n!=self.owned['native'] or m['ControlGroup']!=n['cgroup']:raise Refused('owned_cancel_identity_unknown')
        self.cancel_sent=True
        r=subprocess.run(['/usr/bin/systemctl','--user','stop','--no-block',self.e['unit']],capture_output=True,text=True,timeout=3)
        write_new(self.capture/'OWNED-CANCEL.json',{'rc':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'attributed':self.owned,'mono':time.monotonic()})
        if r.returncode:raise Refused('owned_cancel_request_unknown')
    def signal_handler(self,sig,frame):self.abort_latch('monitor_signal_'+str(sig))
    def dispatch(self,argv):
        old=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM,signal.SIGINT})
        try:
            tick(self.e,True)
            if self.abort or signal.sigpending()&{signal.SIGTERM,signal.SIGINT}:self.abort_latch('abort_before_client_dispatch');return
            self.client=subprocess.Popen(argv,stdout=self.client_out,stderr=self.client_err);self.dispatched=True
        finally:signal.pthread_sigmask(signal.SIG_SETMASK,old)
        # A pending signal delivered on unmask latches ABORT before any grant.
        if self.abort:self.abort_latch('abort_during_creation')
    def observe_ready(self):
        p=self.capture/'UNIT-READY.json'
        if not present(p):return None
        raw,seal=read_private(p);ready=json.loads(raw);self.serial+=1
        m=manager(self.e['unit'],self.e,self.capture,self.serial);now_native=native(ready['native']['pid'])
        self.owned=attribution(self.e,ready,m,now_native)
        return ready,seal,m,now_native
    def grant(self,observation):
        ready,seal,m,n=observation;old=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM,signal.SIGINT})
        try:
            tick(self.e,True)
            if self.abort or signal.sigpending()&{signal.SIGTERM,signal.SIGINT}:self.abort_latch('abort_before_effect_grant');return
            owned=effect_admission(self.e,ready,m,n)
            write_new(self.capture/'EFFECT-GRANT.json',{'schema':'clarp.controller-effect-grant/1','nonce':self.e['nonce'],
                      'envelope_sha256':self.e['envelope_sha256'],'ready_sha256':seal['sha256'],'manager':m,'owned':owned,'mono':time.monotonic()})
            self.granted=True
        finally:signal.pthread_sigmask(signal.SIG_SETMASK,old)
    def keep_error(self,label):
        trace=label+':'+traceback.format_exc();self.errors.append(trace)
        try:sys.stderr.write(trace+'\n');sys.stderr.flush()
        except BaseException as exc:self.errors.append('raw_stderr_fault:'+type(exc).__name__+':'+str(exc))
    def run(self,argv):
        signal.signal(signal.SIGTERM,self.signal_handler);signal.signal(signal.SIGINT,self.signal_handler)
        primary=11
        try:
            self.client_out=(self.capture/'systemd-client.stdout.raw').open('xb')
            self.client_err=(self.capture/'systemd-client.stderr.raw').open('xb')
            self.dispatch(argv)
            if self.client is not None:
                while self.client.poll() is None:
                    now=tick(self.e)
                    if now>=self.e['soft_deadline']:self.abort_latch('external_soft780')
                    if not self.granted and now>=self.e['admit_deadline']:self.abort_latch('external_admission80')
                    if self.owned is None and present(self.capture/'UNIT-READY.json'):
                        observation=self.observe_ready()
                        if not self.abort:self.grant(observation)
                    if self.abort and self.owned is not None:self.cancel_owned()
                    time.sleep(.05)
                rc=self.client.returncode
                if self.granted and not self.abort and not self.errors and rc==0:
                    # Actual --wait OS outcome is necessary; a target JSON label
                    # alone never grants successful completion.
                    raw,_=read_private(self.capture/'TARGET-RETURN.json');target=json.loads(raw)
                    if (target['nonce']!=self.e['nonce'] or target['envelope_sha256']!=self.e['envelope_sha256']
                        or type(target['controller_rc_candidate']) is not int or target['controller_rc_candidate']!=0
                        or target['traceback'] is not None):raise Refused('target_return_join_unknown')
                    tick(self.e);primary=0
        except BaseException:
            self.keep_error('monitor_exception');self.abort_latch('monitor_exception')
            if self.owned is not None and not self.cancel_attempted:
                try:self.cancel_owned()
                except BaseException:self.keep_error('owned_cancel_unknown')
        finally:
            for stream in (self.client_out,self.client_err):
                if stream is not None:
                    try:stream.flush();os.fsync(stream.fileno());stream.close()
                    except BaseException:self.keep_error('client_capture_fault')
            result={'schema':'clarp.controller-monitor-terminal/1','abort':self.abort,
                    'abort_reason':self.abort_reason,'dispatched':self.dispatched,'granted':self.granted,'owned':self.owned,
                    'cancel_attempted':self.cancel_attempted,'cancel_sent':self.cancel_sent,
                    'client_rc':None if self.client is None else self.client.returncode,'errors':self.errors,'mono':time.monotonic(),
                    'unit_terminal_only_if_client_wait_returned':self.client is not None and self.client.returncode is not None,
                    'no_health_balance_or_universal_closure_claim':True}
            try:write_new(self.capture/'MONITOR-TERMINAL.json',result)
            except BaseException:self.keep_error('monitor_terminal_publication_fault')
        return 0 if primary==0 and not self.errors and not self.abort and time.monotonic()<self.e['hard_deadline'] else 11

class Gate:
    def __init__(self,e,seal,capture):
        self.e=e;self.seal=seal;self.capture=capture;tick(e,True)
        self.n=native(os.getpid());inv=os.environ.get('INVOCATION_ID','')
        if not re.fullmatch('[0-9a-f]{32}',inv) or not self.n['cgroup'].endswith('/'+e['unit']) or self.n['boot']!=e['boot']:raise Refused('gate_native_unit_context')
        self.ready={'schema':'clarp.controller-gate-ready/1','nonce':e['nonce'],'unit':e['unit'],'boot':e['boot'],
                    'envelope_sha256':seal['sha256'],'native':self.n,'invocation':inv,
                    'source_pins':{k:e[k]['sha256'] for k in ('binding','loader','gate','protocol','supervisor')},'mono':time.monotonic()}
        write_new(capture/'UNIT-READY.json',self.ready)
        self.ready_sha=hashlib.sha256((capture/'UNIT-READY.json').read_bytes()).hexdigest()
    def step(self):
        tick(self.e,True)
        if present(self.capture/'ABORT.json'):raise Refused('gate_ABORT_no_controller_effect')
        path=self.capture/'EFFECT-GRANT.json'
        if not present(path):return None
        raw,_=read_private(path);g=json.loads(raw)
        if (g['schema']!='clarp.controller-effect-grant/1' or g['nonce']!=self.e['nonce'] or g['envelope_sha256']!=self.seal['sha256']
            or g['ready_sha256']!=self.ready_sha or type(g['mono']) not in (int,float) or not math.isfinite(g['mono'])
            or not self.ready['mono']<=g['mono']<=time.monotonic() or g['mono']>=self.e['admit_deadline']):raise Refused('effect_grant_provenance_or_clock')
        current=native(os.getpid());owned=effect_admission(self.e,self.ready,g['manager'],current)
        if g['owned']!=owned:raise Refused('effect_grant_owned_join')
        if present(self.capture/'ABORT.json'):raise Refused('gate_ABORT_at_effect_boundary')
        _,bound=envelope_read(self.seal['path'],self.seal['sha256'])
        tick(self.e,True);return g

def native_file(path,expected):
    p=Path(path);fd=os.open(p,os.O_RDONLY|os.O_CLOEXEC)
    try:
        a=os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_uid!=0 or a.st_mode&0o022 or not 0<a.st_size<=64*1024**2:raise Refused('native_file_type_mode')
        with os.fdopen(fd,'rb',closefd=False) as f:raw=f.read(64*1024**2+1)
        actual={'path':str(p),'resolved':str(p.resolve(strict=True)),'version':version(a),'named_lstat':version(p.lstat()),'sha256':hashlib.sha256(raw).hexdigest()}
        if version(os.fstat(fd))!=version(a) or version(p.stat())!=version(a) or actual!=expected:raise Refused('native_file_pin_or_named_chain')
        return actual
    finally:os.close(fd)

def check_native_files(e):
    if set(e['native_files'])!={'/usr/bin/python3','/usr/bin/systemd-run','/usr/bin/systemctl','/usr/bin/busctl'}:raise Refused('native_file_set')
    for name,expected in e['native_files'].items():native_file(name,expected)

def unit_argv(e,capture):
    fixed_env={'HOME':'/home/peter','XDG_RUNTIME_DIR':'/run/user/1000','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus','PATH':'/usr/bin:/bin','LC_ALL':'C','LANG':'C','PYTHONDONTWRITEBYTECODE':'1','CLAUDE_PWA_DB':'/home/peter/.local/share/clarp/state.sqlite','CLARP_SHARE_DIR':'/home/peter/.local/share/clarp'}
    argv=['/usr/bin/systemd-run','--user','--wait','--collect','--unit='+e['unit'],
          '--description=Clarp maintenance gate '+e['nonce'],'--property=Type=exec','--property=Restart=no',
          '--property=KillMode=mixed','--property=KillSignal=SIGTERM','--property=RuntimeMaxSec=700',
          '--property=TimeoutStopSec=120','--property=SendSIGKILL=yes','--property=WorkingDirectory='+str(BASE),
          '--property=UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE _PYTHON_SYSCONFIGDATA_NAME CLARP_BACKGROUND_WORKER_PID',
          '--property=StandardOutput=append:'+str(capture/'controller.stdout.raw'),
          '--property=StandardError=append:'+str(capture/'controller.stderr.raw')]
    argv+=['--setenv='+k+'='+v for k,v in sorted(fixed_env.items())]
    argv+=['--','/usr/bin/python3','-I','-S','-B',e['gate']['path'],str(capture/'ENVELOPE.json'),e['envelope_sha256']]
    return argv

def main_monitor(binding_path,binding_sha,B_path,B_sha,entry_path):
    origin=time.monotonic();os.umask(0o077)
    if os.getuid()!=1000 or any(os.environ.get(k) for k in ('LD_PRELOAD','LD_LIBRARY_PATH','LD_AUDIT')):raise Refused('monitor_uid_or_native_loader')
    binding_raw,bref=read_private(canonical(binding_path),binding_sha,65536);b=binding_check(binding_raw)
    _,B_ref=read_private(canonical(B_path),B_sha)
    source_refs={}
    for key in ('loader','gate','protocol','supervisor'):
        ref=b['external_sources'][key];_,actual=read_private(canonical(ref['path']),ref['sha256'],65536)

        if not Path(actual['path']).is_relative_to(BASE.resolve(strict=True)):raise Refused('source_outside_owned_source_root')
        source_refs[key]=actual
    if source_refs['supervisor']['path']!=str(canonical(entry_path)) or source_refs['protocol']['path']!=str(canonical(__file__)):raise Refused('selected_entry_protocol')
    nonce=secrets.token_hex(12);e={'schema':'clarp.controller-effect-envelope/1','nonce':nonce,'unit':PREFIX+nonce+'.service',
        'origin':origin,'admit_deadline':origin+80,'soft_deadline':origin+780,'hard_deadline':origin+900,
        'unit_runtime_us':700000000,'unit_stop_us':120000000,'boot':boot(),'binding':bref,'B_final':B_ref,
        'native_files':b['external_native_files'],**source_refs}
    check_native_files(e);tick(e,True);CAPTURE.mkdir(mode=0o700)
    for name in ('controller.stdout.raw','controller.stderr.raw'):
        with (CAPTURE/name).open('xb') as f:f.flush();os.fsync(f.fileno())
    write_new(CAPTURE/'ENVELOPE.json',e)
    e['envelope_sha256']=hashlib.sha256((CAPTURE/'ENVELOPE.json').read_bytes()).hexdigest()
    # Before any create, refuse an already loadable unit under this fresh name.
    fresh=run_read(['/usr/bin/systemctl','--user','show',e['unit'],'--property=LoadState'],e,CAPTURE,'PRECREATE-LOADSTATE.json')
    if fresh!='LoadState=not-found\n':raise Refused('unit_name_collision_or_unknown')
    argv=unit_argv(e,CAPTURE);write_new(CAPTURE/'MONITOR-LAUNCH.json',{'argv':argv,'envelope_sha256':e['envelope_sha256'],'source':source_refs,'B':B_ref,'no_effect_until_attributed_gate':True})
    check_native_files(e);tick(e,True)
    return Coordinator(e,CAPTURE).run(argv)

def main_gate(envelope_path,envelope_sha,entry_path):
    os.umask(0o077);e,seal=envelope_read(envelope_path,envelope_sha);capture=Path(envelope_path).parent
    if str(canonical(entry_path))!=e['gate']['path'] or str(canonical(__file__))!=e['protocol']['path']:raise Refused('gate_source_entry_mismatch')
    check_native_files(e);gate=Gate(e,seal,capture)
    while gate.step() is None:time.sleep(min(.05,max(0,e['admit_deadline']-time.monotonic())))
    check_native_files(e);tick(e,True)
    if present(capture/'ABORT.json'):raise Refused('gate_final_ABORT_no_effect')
    binding_raw,_=read_private(e['binding']['path'],e['binding']['sha256'],65536);b=binding_check(binding_raw)
    loader_raw,_=read_private(e['loader']['path'],e['loader']['sha256'],65536)
    loader_code=compile(loader_raw,e['loader']['path'],'exec',dont_inherit=True)
    sys.argv=[e['loader']['path'],'controller',e['binding']['path'],e['binding']['sha256'],b['controller_evidence_path']]
    actual_rc=11;failure=None
    try:
        # Actual approved loader then owns all controller before-import/member
        # checks and only that controller's existing effect algorithms.
        space={'__name__':'__main__','__file__':e['loader']['path'],'__package__':None}
        # Effect-admission commit: all potentially blocking input work and
        # compilation precede this final immutable clock/boot/abort guard.
        tick(e,True)
        if present(capture/'ABORT.json'):raise Refused('gate_final_ABORT_at_exec_commit')
        # Pure clock sample AFTER every guard read; no IO follows before exec.
        if time.monotonic()>=e['admit_deadline']:raise Refused('gate_exec_commit_absolute_admission_deadline')
        exec(loader_code,space)
        actual_rc=0
    except SystemExit as exc:
        actual_rc=exc.code if type(exc.code) is int else 0 if exc.code is None else 11
    except BaseException:
        failure=traceback.format_exc();sys.stderr.write(failure);sys.stderr.flush()
    finally:
        write_new(capture/'TARGET-RETURN.json',{'schema':'clarp.controller-target-return/1','nonce':e['nonce'],
                  'envelope_sha256':seal['sha256'],'controller_rc_candidate':actual_rc,'traceback':failure,
                  'native_before_effect':gate.n,'mono':time.monotonic(),'needs_actual_unit_OS_and_cgroup_confirmation':True,
                  'not_runtime_health_or_balance_success':True})
    return actual_rc
