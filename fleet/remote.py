#!/usr/bin/env python3
"""SSH-invoked peer protocol. No network listener, screen access or credentials in jobs."""
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import subprocess
import socket
import socketserver
import sys
import tempfile
import time

ROOT=Path(os.environ.get('CLARP_FLEET_WORKER_STATE',str(Path.home()/'.local/state/clarp-fleet-worker')))
ROOT.mkdir(parents=True,exist_ok=True);ROOT.chmod(0o700)
MAX_REQUEST=64*1024*1024
PROFILE_NAMES={'diagnostic','compile-cpp','cmake','render-cpu','render-gpu','whisper','whisper-gpu','agent'}


def write(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    temporary.write_text(json.dumps(value));temporary.chmod(0o600);os.replace(temporary,path)


def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {} if default is None else default


def command(args,timeout=4):
    try:
        r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        return r.stdout.strip() if r.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):return None


def environment():
    env=os.environ.copy()
    env['PATH']=str(Path.home()/'.local/bin')+':/opt/homebrew/bin:/usr/local/bin:'+env.get('PATH','/usr/bin:/bin')
    return env


os.environ['PATH']=environment()['PATH']


def config():return read(ROOT/'config.json')


def model_path():
    explicit=config().get('whisper_model')
    if explicit:return str(Path(explicit).expanduser()) if Path(explicit).expanduser().is_dir() else None
    base=Path.home()/'.cache/huggingface/hub/models--Systran--faster-whisper-small.en/snapshots'
    candidates=sorted(base.glob('*/model.bin'))
    return str(candidates[-1].parent) if candidates else None


def probe():
    x={'protocol':1,'reachable':True,'observed_at':time.time(),'os':platform.system(),'arch':platform.machine(),
       'logical_cpus':os.cpu_count(),'load':list(os.getloadavg()),'cpu_busy_pct':None,'ram_available_mb':None,
       'gpu_free_mb':None,'gpu_total_mb':0,'gpu_name':None,'unified_memory':platform.system()=='Darwin',
       'profiles':['diagnostic'],'on_battery':False,'limits':'watchdog','worker_digest':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if x['os']=='Linux':
        m={k:int(v.strip().split()[0])/1024 for k,v in (l.split(':',1) for l in Path('/proc/meminfo').read_text().splitlines())}
        x.update(ram_total_mb=m['MemTotal'],ram_available_mb=m['MemAvailable'])
        def ticks():return [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        a=ticks();time.sleep(.15);b=ticks();d=[v-u for u,v in zip(a,b)];total=sum(d)
        x['cpu_busy_pct']=round(100*(total-d[3]-d[4])/total,1) if total else None
        x['pressure']={n:Path('/proc/pressure/'+n).read_text().strip() for n in ('cpu','memory','io') if Path('/proc/pressure/'+n).exists()}
        online=[p.read_text().strip() for p in Path('/sys/class/power_supply').glob('*/online')]
        x['on_battery']=bool(online and '1' not in online)
        if shutil.which('nvidia-smi'):
            raw=command(['nvidia-smi','--query-gpu=name,memory.total,memory.free,utilization.gpu','--format=csv,noheader,nounits'])
            if raw:
                fields=raw.splitlines()[0].split(',');x.update(gpu_name=fields[0].strip(),gpu_total_mb=float(fields[1]),gpu_free_mb=float(fields[2]),gpu_utilization_pct=float(fields[3]))
        if shutil.which('systemd-run') and Path('/run/user/'+str(os.getuid())+'/bus').exists():x['limits']='cgroup'
    elif x['os']=='Darwin':
        x['ram_total_mb']=int(command(['sysctl','-n','hw.memsize']) or '0')/1048576
        raw=command(['vm_stat']) or '';size=int(re.search(r'page size of (\d+)',raw).group(1)) if 'page size of' in raw else 16384
        pages={k:int(v.strip().strip('.')) for k,v in (l.split(':',1) for l in raw.splitlines()[1:] if ':' in l) if v.strip().strip('.').isdigit()}
        x['ram_available_mb']=sum(pages.get(k,0) for k in ['Pages free','Pages inactive','Pages speculative'])*size/1048576
        x['ram_availability_kind']='free+inactive+speculative estimate'
        x['on_battery']='Battery Power' in (command(['pmset','-g','batt']) or '')
        x['gpu_name']=command(['sysctl','-n','machdep.cpu.brand_string'])
    x['tools']={n:shutil.which(n) for n in ['c++','cmake','ctest','ffmpeg','bwrap','codex']}
    if x['tools']['c++']:x['profiles'].append('compile-cpp')
    if x['tools']['cmake'] and x['tools']['ctest']:x['profiles'].append('cmake')
    if config().get('ffmpeg_bin'):x['tools']['ffmpeg']=config()['ffmpeg_bin']
    x['profile_errors']={}
    if x['tools']['ffmpeg']:
        if command([x['tools']['ffmpeg'],'-version']):x['profiles'].append('render-cpu')
        else:x['profile_errors']['render-cpu']='FFmpeg executable failed its runtime check'
    if x['tools']['ffmpeg'] and config().get('nvenc_verified'):x['profiles'].append('render-gpu')
    if importlib.util.find_spec('faster_whisper') and model_path():x['profiles'].append('whisper')
    if x['tools']['codex'] and config().get('agent_enabled'):x['profiles'].append('agent')
    if 'whisper' in x['profiles'] and config().get('whisper_cuda_verified'):x['profiles'].append('whisper-gpu')
    warm=read(ROOT/'warm.json')
    x['whisper_warm']=bool(warm.get('ready') and alive(warm.get('pid'),warm.get('token')))
    x['whisper_warm_device']=warm.get('device') if x['whisper_warm'] else None
    tail=command(['tailscale','status','--json'])
    try:x['tailscale_dns']=json.loads(tail or '{}').get('Self',{}).get('DNSName','').rstrip('.')
    except ValueError:x['tailscale_dns']=''
    x['whisper_model']=Path(model_path()).name if model_path() else None
    return x


def process_token(pid):
    try:
        if platform.system()=='Linux':return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]
        return command(['ps','-p',str(pid),'-o','lstart='])
    except (OSError,IndexError):return None


def alive(pid,token):return bool(pid and token and process_token(pid)==token)


def job_dir(broker,attempt):
    if not re.fullmatch('[a-f0-9]{32}',broker or '') or not re.fullmatch('[a-f0-9]{32}',attempt or ''):raise ValueError('Invalid execution identity')
    return ROOT/'jobs'/broker/attempt


def safe_file(name):
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts or '\\' in name or '\x00' in name:raise ValueError('Unsafe file path')
    return p


def unpack(directory,files):
    total=0
    if len(files)>5000:raise ValueError('Too many input files')
    for f in files:
        name=safe_file(f['path']);data=base64.b64decode(f['data'],validate=True);total+=len(data)
        if total>32*1024*1024:raise ValueError('Input bundle exceeds32MiB')
        if hashlib.sha256(data).hexdigest()!=f['sha256']:raise ValueError('Input checksum mismatch')
        target=directory/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(0o755 if f.get('executable') else 0o644)


def start(request):
    cfg=config();broker=request['broker'];attempt=request['attempt'];job=request['job'];directory=job_dir(broker,attempt)
    if cfg.get('broker_id')!=broker:raise ValueError('Broker is not enrolled on this executor')
    telemetry=probe()
    if job.get('profile') not in telemetry['profiles']:raise ValueError('Profile unavailable on executor')
    digest=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    directory.parent.mkdir(parents=True,exist_ok=True)
    with (ROOT/'admission.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if directory.exists():
            old=read(directory/'request.json')
            if old.get('request_digest')!=digest:raise ValueError('Existing attempt differs or has uncertain staging')
            return read(directory/'state.json',{'status':'staging'})
        active=[]
        for p in (ROOT/'jobs').glob('*/*/state.json'):
            s=read(p)
            if s.get('status') in ('staging','running','cancelling','unknown'):active.append(read(p.parent/'request.json').get('job',{}))
        interactive=sum(j.get('priority')=='interactive' for j in active)
        if job.get('priority')=='interactive':
            if interactive>=1 or len(active)>=cfg.get('max_jobs',2)+1:raise ValueError('Executor interactive slot is occupied')
        elif len(active)-interactive>=cfg.get('max_jobs',2):raise ValueError('Executor background concurrency reservation is full')
        for resource in ('cpu','ram_mb','gpu_mb'):
            wanted=float(job['resources'].get(resource,0));used=sum(float(j.get('resources',{}).get(resource,0)) for j in active)
            if wanted<0 or used+wanted>cfg.get('capacity',{}).get(resource,0):raise ValueError('Executor resource reservation rejected: '+resource)
        if telemetry.get('ram_available_mb') is None or job['resources']['ram_mb']>telemetry['ram_available_mb']-cfg.get('ram_headroom_mb',512):raise ValueError('Physical RAM changed before executor admission')
        if job['resources'].get('gpu_mb',0) and (telemetry.get('gpu_free_mb') is None or job['resources']['gpu_mb']>telemetry['gpu_free_mb']-256):raise ValueError('Physical GPU memory changed before executor admission')
        directory.mkdir();(directory/'source').mkdir();(directory/'result').mkdir()
        write(directory/'request.json',{**request,'request_digest':digest})
        write(directory/'state.json',{'status':'staging','attempt':attempt,'updated':time.time()})
        write(directory/'lease.json',{'expires':time.time()+60})
        try:unpack(directory/'source',job.get('files',[]))
        except Exception as e:
            write(directory/'state.json',{'status':'failed','attempt':attempt,'error':str(e),'updated':time.time()});raise
        with (directory/'supervisor.log').open('ab') as log:
            child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'run',str(directory)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        return {'status':'staging','attempt':attempt,'supervisor_pid':child.pid}


def argv_for(job,directory):
    src=directory/'source';out=directory/'result';profile=job['profile'];cpu=max(1,int(job['resources']['cpu']))
    if profile=='diagnostic':return [sys.executable,'-c',"import time,json,platform; time.sleep(float(__import__('sys').argv[1])); print(json.dumps({'host':platform.node(),'ok':True}))",str(min(30,max(0,float(job.get('seconds',0)))))]
    if profile=='compile-cpp':return ['c++','-std=c++20','-O2',str(src/'main.cpp'),'-o',str(out/'program')]
    if profile=='cmake':
        script="import subprocess,sys; s,o,n=sys.argv[1:]; subprocess.run(['cmake','-S',s,'-B',o,'-DCMAKE_BUILD_TYPE=Release'],check=True); subprocess.run(['cmake','--build',o,'--parallel',n],check=True); subprocess.run(['ctest','--test-dir',o,'--output-on-failure'],check=True)"
        return [sys.executable,'-c',script,str(src),str(out/'build'),str(cpu)]
    if profile.startswith('render-'):
        return [config().get('ffmpeg_bin') or 'ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=640x360:rate=24','-t',str(min(30,max(1,int(job.get('seconds',2))))),'-c:v','h264_nvenc' if profile=='render-gpu' else 'mpeg4','-threads',str(cpu),'-y',str(out/'render.mp4')]
    if profile in {'whisper','whisper-gpu'}:
        return [sys.executable,str(Path(__file__).resolve()),'transcribe',str(directory)]
    if profile=='agent':
        task=(src/'task.md').read_text()
        task=('Work only in this isolated job directory. Do not publish, send external messages, access visible UI, or modify system/user configuration. '
              'Return the requested result and evidence. Write ALL deliverables (report.md, result.json, fix.patch, logs) to the absolute output directory: '+str(out)+'. '
              'This absolute path overrides any ambiguous relative output path in the task.\n\n'+task)
        return ['codex','exec','--ephemeral','--skip-git-repo-check','--sandbox','workspace-write','--model',job['model'],'-c','model_reasoning_effort='+json.dumps(job['effort']),'--output-last-message',str(out/'answer.md'),task]
    raise ValueError('Unknown profile')


def run(directory):
    directory=Path(directory);request=read(directory/'request.json');job=request['job'];attempt=request['attempt'];started=time.time();cfg=config()
    state={'status':'running','attempt':attempt,'job_id':request['job_id'],'started':started,'updated':started,'supervisor_pid':os.getpid(),'supervisor_token':process_token(os.getpid()),'limits':'watchdog'}
    write(directory/'state.json',state)
    child=None;unit=None
    try:
        argv=argv_for(job,directory);env=environment()
        for key in ['DISPLAY','WAYLAND_DISPLAY','HYPRLAND_INSTANCE_SIGNATURE','XDG_ACTIVATION_TOKEN','DESKTOP_STARTUP_ID']:env.pop(key,None)
        env.update(QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software',OMP_NUM_THREADS=str(max(1,int(job['resources']['cpu']))))
        if platform.system()=='Linux' and shutil.which('bwrap') and job['profile'] in {'diagnostic','compile-cpp','cmake','render-cpu'}:
            mounts=[]
            for path in ('/usr','/bin','/sbin','/lib','/lib64','/etc'):
                if Path(path).exists():mounts+=['--ro-bind',path,path]
            if job['profile']=='render-cpu' and config().get('ffmpeg_bin'):
                tool=str(Path(config()['ffmpeg_bin']).resolve());mounts+=['--ro-bind',tool,tool]
            argv=['bwrap','--unshare-pid','--unshare-ipc','--unshare-net',*mounts,'--dev','/dev','--proc','/proc','--tmpfs','/tmp','--tmpfs','/run','--dir','/work','--ro-bind',str(directory/'source'),'/work/source','--bind',str(directory/'result'),'/work/result','--chdir','/work/source',*[a.replace(str(directory),'/work') for a in argv]]
            state['filesystem_isolation']='private workdir, read-only system, no user home or display sockets'
        if platform.system()=='Linux' and shutil.which('systemd-run') and Path('/run/user/'+str(os.getuid())+'/bus').exists():
            unit='clarp-fleet-'+attempt
            argv=['systemd-run','--user','--scope','--quiet','--collect','--unit',unit,'-p','MemoryMax='+str(int((128 if job['profile'].startswith('whisper') else job['resources']['ram_mb'])*1048576)),'-p','CPUQuota='+str(int(job['resources']['cpu']*100))+'%','-p','TasksMax=256','-p','CPUWeight='+('10000' if job.get('priority')=='interactive' else '50'),*argv]
            env['XDG_RUNTIME_DIR']='/run/user/'+str(os.getuid());state['limits']='cgroup';state['unit']=unit;write(directory/'state.json',state)
        with (directory/'output.log').open('wb') as log:
            child=subprocess.Popen(argv,cwd=directory/'source',env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            state.update(child_pid=child.pid,child_token=process_token(child.pid));write(directory/'state.json',state)
            reason=None
            while child.poll() is None:
                now=time.time()
                if (directory/'cancel').exists():reason='cancelled'
                elif now-started>job['timeout']:reason='timeout'
                elif read(directory/'lease.json').get('expires',0)<now:reason='lease expired'
                elif (directory/'output.log').stat().st_size>8*1024*1024:reason='output limit'
                if reason:
                    if job['profile'].startswith('whisper'):
                        subprocess.run(['systemctl','--user','stop','clarp-fleet-whisper-'+request['broker']+'.service'],capture_output=True,timeout=8)
                    if unit:subprocess.run(['systemctl','--user','stop',unit+'.scope'],env=env,capture_output=True,timeout=8)
                    with contextlib.suppress(ProcessLookupError):os.killpg(child.pid,signal.SIGTERM)
                    try:child.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(ProcessLookupError):os.killpg(child.pid,signal.SIGKILL)
                        child.wait()
                    break
                time.sleep(.2)
        output=(directory/'output.log').read_text(errors='replace')[-65536:]
        artifacts=[]
        for p in (directory/'result').rglob('*'):
            if p.is_file() and not p.is_symlink() and p.stat().st_size<=32*1024*1024:
                artifacts.append({'path':str(p.relative_to(directory/'result')),'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
        status='cancelled' if reason=='cancelled' else 'failed' if reason or child.returncode else 'succeeded'
        state.update(status=status,exit_code=child.returncode,error=reason or '',output=output,artifacts=artifacts,elapsed_seconds=time.time()-started,updated=time.time(),source=job.get('source',{}),parent=job['parent'])
    except Exception as error:state.update(status='failed',error=str(error),updated=time.time())
    write(directory/'state.json',state)



def warm_server():
    import gc
    from faster_whisper import WhisperModel
    sock=ROOT/'whisper.sock';sock.unlink(missing_ok=True)
    cache={'model':None,'device':None,'used':time.monotonic()}
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                q=json.loads(self.rfile.readline(8192));directory=job_dir(q['broker'],q['attempt']);req=read(directory/'request.json');job=req['job']
                if q['broker']!=config().get('broker_id') or read(directory/'lease.json').get('expires',0)<time.time():raise ValueError('Inference lease is not active')
                audio=directory/'source/audio.wav'
                if not audio.resolve().is_relative_to((directory/'source').resolve()):raise ValueError('Invalid audio path')
                import wave
                with wave.open(str(audio)) as wav:
                    if wav.getnframes()/wav.getframerate()>120:raise ValueError('Interactive audio limit is120 seconds')
                device='cuda' if job['profile']=='whisper-gpu' else 'cpu';warm=cache['model'] is not None and cache['device']==device;t=time.monotonic()
                if not warm:
                    cache['model']=None;gc.collect()
                    cache['model']=WhisperModel(model_path(),device=device,compute_type='int8_float32' if device=='cuda' else 'int8',local_files_only=True,cpu_threads=2)
                    cache['device']=device
                remote_state={'pid':os.getpid(),'token':process_token(os.getpid()),'ready':True,'device':device,'last_used':time.time()};write(ROOT/'warm.json',remote_state)
                segments,info=cache['model'].transcribe(str(audio),beam_size=1);texts=[]
                for seg in segments:
                    if (directory/'cancel').exists() or read(directory/'lease.json').get('expires',0)<time.time():raise ValueError('Inference cancelled')
                    texts.append(seg.text.strip())
                result={'text':' '.join(texts),'language':info.language,'device':device,'warm':warm,'elapsed_seconds':time.monotonic()-t}
                self.wfile.write((json.dumps({'ok':True,'result':result})+'\n').encode());cache['used']=time.monotonic()
            except Exception as e:
                with contextlib.suppress(BrokenPipeError):self.wfile.write((json.dumps({'ok':False,'error':str(e)})+'\n').encode())
    server=socketserver.UnixStreamServer(str(sock),Handler);sock.chmod(0o600);server.timeout=1
    try:
        while time.monotonic()-cache['used']<120:server.handle_request()
    finally:server.server_close();sock.unlink(missing_ok=True);write(ROOT/'warm.json',{'ready':False})


def transcribe(directory):
    directory=Path(directory);req=read(directory/'request.json');sock=ROOT/'whisper.sock';unit='clarp-fleet-whisper-'+req['broker']
    with (ROOT/'warm-start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            test=socket.socket(socket.AF_UNIX);test.connect(str(sock));test.close();healthy=True
        except OSError:healthy=False
        if not healthy:
            sock.unlink(missing_ok=True)
            cfg=config();libraries=':'.join(cfg.get('cuda_libraries',[]));argv=[sys.executable,str(Path(__file__).resolve()),'warm-server']
            if platform.system()=='Linux' and Path('/run/user/'+str(os.getuid())+'/bus').exists():
                subprocess.run(['systemctl','--user','reset-failed',unit+'.service'],capture_output=True)
                argv=['systemd-run','--user','--quiet','--collect','--unit',unit,'-p','MemoryMax=1920M','-p','CPUQuota=200%','-p','CPUWeight=10000','--setenv=LD_LIBRARY_PATH='+libraries,'--setenv=CLARP_FLEET_WORKER_STATE='+str(ROOT),*argv]
                subprocess.run(argv,check=True,capture_output=True)
            else:
                env=environment();env['LD_LIBRARY_PATH']=libraries
                with (ROOT/'warm.log').open('ab') as log:subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            deadline=time.monotonic()+20
            while not sock.exists():
                if time.monotonic()>deadline:raise TimeoutError('Warm inference service did not start')
                time.sleep(.1)
    conn=socket.socket(socket.AF_UNIX);conn.settimeout(req['job']['timeout'])
    try:
        conn.connect(str(sock));conn.sendall((json.dumps({'broker':req['broker'],'attempt':req['attempt']})+'\n').encode())
        data=conn.makefile('rb').readline(1024*1024);result=json.loads(data)
        if not result.get('ok'):raise RuntimeError(result.get('error','Inference failed'))
        print(json.dumps(result['result']))
    finally:conn.close()

def rpc(request):
    action=request.get('action')
    if action=='probe':return probe()
    if action=='start':return start(request)
    directory=job_dir(request.get('broker'),request.get('attempt'))
    if config().get('broker_id')!=request.get('broker'):raise ValueError('Broker is not enrolled')
    if action=='status':
        if not directory.exists():return {'status':'absent'}
        if request.get('renew'):write(directory/'lease.json',{'expires':time.time()+60})
        state=read(directory/'state.json',{'status':'staging'})
        if state.get('status') in {'running','unknown'} and not alive(state.get('supervisor_pid'),state.get('supervisor_token')):
            child_alive=alive(state.get('child_pid'),state.get('child_token'))
            unit_live=bool(state.get('unit') and command(['systemctl','--user','show',state['unit']+'.scope','-p','ActiveState','--value'])=='active')
            state.update(status='unknown' if child_alive or unit_live else 'failed',error='Supervisor lost; owned child still exists' if child_alive or unit_live else 'Supervisor lost; no owned process remains',updated=time.time());write(directory/'state.json',state)
        return state
    if action=='cancel':
        if not directory.exists():return {'status':'absent'}
        (directory/'cancel').touch();state=read(directory/'state.json')
        if not alive(state.get('supervisor_pid'),state.get('supervisor_token')) and alive(state.get('child_pid'),state.get('child_token')):
            if state.get('unit')=='clarp-fleet-'+request['attempt']:
                subprocess.run(['systemctl','--user','stop',state['unit']+'.scope'],capture_output=True,timeout=8)
            with contextlib.suppress(ProcessLookupError):os.killpg(state['child_pid'],signal.SIGTERM)
        return state
    if action=='artifact':
        path=directory/'result'/safe_file(request['path'])
        if not path.resolve().is_relative_to((directory/'result').resolve()) or path.is_symlink() or not path.is_file() or path.stat().st_size>32*1024*1024:raise ValueError('Artifact unavailable')
        data=path.read_bytes();return {'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}
    raise ValueError('Unknown RPC action')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='run':run(sys.argv[2])
    elif len(sys.argv)>1 and sys.argv[1]=='warm-server':warm_server()
    elif len(sys.argv)>1 and sys.argv[1]=='transcribe':transcribe(sys.argv[2])
    else:
        try:
            raw=sys.stdin.buffer.readline(MAX_REQUEST+1)
            if len(raw)>MAX_REQUEST:raise ValueError('Request too large')
            print(json.dumps({'ok':True,'result':rpc(json.loads(raw))}))
        except Exception as error:
            print(json.dumps({'ok':False,'error':str(error)}));sys.exit(1)
