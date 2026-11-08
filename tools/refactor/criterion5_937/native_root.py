"""Real per-pin entrypoint composition, SOURCE PREPARATION ONLY/UNEXECUTED.

Not an executable/admitted runner. No clock hook or source mutation. Serve entry
is NOT readiness, native return is NOT actor/process drain, and this cooperative
control is NOT a hard supervisor. Parent must qualify the entire source/guard/
client/observer/lifecycle package before invoking the function on the buildbox.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy
import select
import socket
import socketserver
import stat
import sys
import threading
import time

from .native_isolation import Guard
from .proc_samples import bind

PINS = {'4f33cdc9287eafcddd0adfb17b51487c84ace28b': 'server/lib/backend_usage.py',
        '0073254687d22ac5a6c1d01b8af29fa678dcf4a2': 'server/lib/backend_usage_codex.py'}


def inventory(source, files):
    """Actual physical source/cache/link/hash inventory, not Git-HEAD inference."""
    actual = {}
    for path in source.rglob('*'):
        if path.is_symlink() or path.name == '__pycache__' or path.suffix in ('.pyc','.pyo'):
            raise ValueError('source link/cache forbidden')
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('source hardlink forbidden')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while block := stream.read(1024*1024):
                    digest.update(block)
            actual[path.relative_to(source).as_posix()] = digest.hexdigest()
        elif not path.is_dir():
            raise ValueError('special source file forbidden')
    if actual != files:
        raise ValueError('complete actual source inventory mismatch')


def environment(root, source, port):
    expected = {'HOME':str(root/'home'),'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'UTC',
                'XDG_CONFIG_HOME':str(root/'home/.config'),'XDG_CACHE_HOME':str(root/'home/.cache'),
                'XDG_DATA_HOME':str(root/'home/.local/share'),'CLARP_CONFIG_DIR':str(root/'config'),
                'CLARP_CACHE_DIR':str(root/'cache'),'CLARP_DATA_DIR':str(root/'data'),
                'CLARP_SHARE_DIR':str(root/'data'),'CLAUDE_PWA_CONFIG':str(root/'config/config.toml'),
                'CLAUDE_PWA_ROOT':str(source),'CLAUDE_PWA_DB':str(root/'state.sqlite'),
                'CLAUDE_PWA_AUDIO_DIR':str(root/'cache/audio'),'CLAUDE_PWA_BIND':'127.0.0.1',
                'CLAUDE_PWA_PORT':str(port),'PYTHONDONTWRITEBYTECODE':'1',
                'PYTHONNOUSERSITE':'1','PYTHONHASHSEED':'0'}
    if (set(os.environ)-set(expected)-{'PYTHONPATH','PYTHONPYCACHEPREFIX','LC_CTYPE'}
            or any(os.environ.get(k)!=v for k,v in expected.items())
            or Path.home()!=root/'home' or Path.cwd()!=root
            or not sys.dont_write_bytecode or not sys.pycache_prefix
            or os.environ.get('PYTHONPYCACHEPREFIX')!=sys.pycache_prefix):
        raise ValueError('fresh exact private root environment not supplied')
    # Helper PYTHONPATH/venv and effective loaded-code provenance remain an
    # explicit outer-package qualification, not proved merely by this allowlist.
    return expected


def write_new(path, value):
    raw = json.dumps(value,sort_keys=True,separators=(',',':')).encode('ascii')
    if len(raw)>65536:
        raise ValueError('root diagnostic byte cap')
    with path.open('xb',buffering=0) as stream:
        if stream.write(raw)!=len(raw):
            raise OSError('short root diagnostic; no retry')
        os.fsync(stream.fileno())


def run_native_root(*, attempt, pin, role, source, root, output, files, port, absolute_deadline):
    """Invoke exactly one real native root, no provider/work/readiness fabrication.

    A source-bound parent must supply the owned stdin control pipe and absolute
    end. This function cannot create its own compute/captain allowance. Source
    hashes, wrapper cost and source/guard setup are part of raw startup timing;
    no guessed overhead subtraction. No initial_seed_clock import/install.
    """
    if socket.gethostname()=='laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY')
    if (pin not in PINS or role not in ('host','runtime') or type(attempt) is not str
            or not attempt or len(attempt)>128 or type(port) is not int or not 1024<=port<=65535
            or type(absolute_deadline) not in (int,float)
            or not time.monotonic()<absolute_deadline<float('inf')):
        raise ValueError('exact source/role/attempt/endpoint/deadline required')
    source,root,output=map(Path,(source,root,output))
    for path in (source,root,output):
        if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True)!=path or not path.is_dir():
            raise ValueError('exact physical source/private/output directories required')
    for a,b in ((source,root),(source,output),(root,output)):
        if a==b or a.is_relative_to(b) or b.is_relative_to(a):
            raise ValueError('source/private/output overlap')
    if any(output.iterdir()) or any(n=='lib' or n.startswith('lib.') for n in sys.modules):
        raise ValueError('fresh output/interpreter required before product imports')
    if not stat.S_ISFIFO(os.fstat(0).st_mode):
        raise ValueError('owned parent pipe required; no terminal input')
    environment(root,source,port)
    prefix=Path(sys.pycache_prefix)
    if (not prefix.is_absolute() or prefix.is_symlink() or prefix.resolve(strict=True)!=prefix
            or not prefix.is_dir() or any(prefix.iterdir())
            or any(prefix.is_relative_to(p) or p.is_relative_to(prefix) for p in (source,root,output))):
        raise ValueError('unique empty external child prefix required')
    inventory(source,files)
    if not time.monotonic()<absolute_deadline:
        raise TimeoutError('source validation consumed phase; no root start')
    me=bind(os.getpid())
    failure=output/'guard-failures.jsonl'
    guard=Guard(source=source,usage_file=PINS[pin],usage_sha256=files[PINS[pin]],
                failure_file=failure,unix_paths=(str(root/'cache/runtime.sock'),),
                tcp_endpoints=(('127.0.0.1',port),),search_dirs=('/usr/bin','/bin'),expected_cwd=root)
    guard.install()  # before ANY product import; no changed availability result
    sys.path.insert(0,str(source/'server'))
    original_serve=socketserver.BaseServer.serve_forever
    original_exception=threading.excepthook
    lock=threading.Lock(); entered=threading.Event(); done=threading.Event()
    state={'server':None,'stop_requested':False,'partial':[], 'thread_errors':[], 'observation_failed':False}
    started=time.monotonic_ns(); cpu=time.process_time_ns()

    def partial(code):
        try:
            with lock:
                if code not in state['partial'] and len(state['partial'])<32:
                    state['partial'].append(code)
        except BaseException:
            state['observation_failed']=True

    def serving(server,*args,**kwargs):
        expected=('127.0.0.1',port) if role=='host' else str(root/'cache/runtime.sock')
        if server.server_address!=expected or state['server'] is not None:
            partial('native_serve_identity_or_duplicate')
            raise RuntimeError('unexpected actual native server')
        state['server']=server
        try:
            write_new(output/'serve-enter.json',{'attempt':attempt,'pin':pin,'role':role,
                      'pid':me.pid,'start_ticks':me.start_ticks,'boot_id':me.boot_id,
                      'native_class':type(server).__module__+'.'+type(server).__qualname__,
                      'mono_ns':time.monotonic_ns(),'scope':'actual serve entry ONLY, not readiness'})
        except BaseException:
            state['observation_failed']=True
            partial('serve_diagnostic_failed')
        finally:
            try:
                entered.set()
            except BaseException:
                state['observation_failed']=True
        return original_serve(server,*args,**kwargs)

    def thread_error(args):
        try:
            with lock:
                if len(state['thread_errors'])<32:
                    state['thread_errors'].append(args.exc_type.__name__[:128])
            partial('uncaught_native_thread_exception')
        except BaseException:
            state['observation_failed']=True
        finally:
            original_exception(args)  # preserve actual native diagnostic exactly once

    def control():
        # Stop is an OWNER request to the captured server, not a provider prompt,
        # reset, recovery bypass or arbitrary callable execution channel.
        pending=bytearray()
        try:
            os.set_blocking(0,False)
            while not done.is_set():
                remaining=absolute_deadline-time.monotonic()
                if remaining<=0:
                    partial('cooperative_deadline_not_a_hard_stop')
                    break
                readable,_,_=select.select([0],[],[],min(.25,remaining))
                if not readable:
                    continue
                block=os.read(0,1025-len(pending))
                if not block:
                    partial('owner_pipe_closed')
                    break
                pending.extend(block)
                if len(pending)>1024:
                    partial('owner_message_cap')
                    break
                if b'\n' not in pending:
                    continue
                raw,tail=bytes(pending).split(b'\n',1)
                command=json.loads(raw)
                wanted={'action':'stop','attempt':attempt,'pid':me.pid,'start_ticks':me.start_ticks}
                if (tail or type(command) is not dict or command!=wanted
                        or type(command.get('pid')) is not int or type(command.get('start_ticks')) is not int):
                    partial('owner_stop_identity_or_message')
                    break
                state['stop_requested']=True
                break
            if not done.is_set():
                while not entered.is_set() and not done.is_set() and time.monotonic()<absolute_deadline:
                    entered.wait(min(.05,max(0.,absolute_deadline-time.monotonic())))
                if entered.is_set() and not done.is_set():
                    # Actual native shutdown may block. The outer qualified
                    # supervisor owns the absolute timeout/unknown preservation.
                    state['server'].shutdown()
        except BaseException as error:
            partial('owned_control_'+type(error).__name__)

    socketserver.BaseServer.serve_forever=serving
    threading.excepthook=thread_error
    owner=threading.Thread(target=control,name='criterion5-owned-stop',daemon=True)
    primary=None
    old_argv=sys.argv[:]
    try:
        owner.start()
        product=source/'server'/('server.py' if role=='host' else 'runtime.py')
        sys.argv=[str(product)]
        write_new(output/'entrypoint-enter.json',{'attempt':attempt,'pin':pin,'role':role,
                  'pid':me.pid,'start_ticks':me.start_ticks,'boot_id':me.boot_id,
                  'mono_ns':started,'process_cpu_ns':cpu,
                  'entrypoint_sha256':files[product.relative_to(source).as_posix()],
                  'scope':'helper entry-to-root; primary spawn timing belongs to parent'})
        try:
            runpy.run_path(str(product),run_name='__main__')
        except SystemExit as exit_value:
            if exit_value.code not in (None,0):
                raise
    except BaseException as error:
        primary=error
        partial('native_root_'+type(error).__name__)
        raise
    finally:
        done.set(); sys.argv=old_argv
        try:
            if owner.ident is not None:
                owner.join(min(1.0,max(0.,absolute_deadline-time.monotonic())))
        except BaseException:
            partial('owned_control_join_unknown')
        if owner.is_alive():
            partial('owned_control_not_joined')
        if socketserver.BaseServer.serve_forever is not serving or threading.excepthook is not thread_error:
            partial('observer_binding_changed')
        socketserver.BaseServer.serve_forever=original_serve
        threading.excepthook=original_exception
        if state['observation_failed']:
            partial('observer_metadata_unknown')
        try:
            if guard.failed or failure.exists():
                partial('native_guard_refused')
        except BaseException:
            partial('native_guard_result_unknown')
        # This census is diagnostic ONLY. Neither daemon status nor native root
        # return proves worker completion/exit/drain. Parent must qualify actors.
        try:
            alive_count=sum(t is not threading.current_thread() for t in threading.enumerate())
        except BaseException:
            alive_count=None
            partial('thread_census_unknown')
        if not entered.is_set() or not state['stop_requested']:
            partial('no_observed_serve_or_owner_stop')
        try:
            write_new(output/'root-return.json',{'attempt':attempt,'pin':pin,'role':role,
                      'pid':me.pid,'start_ticks':me.start_ticks,'boot_id':me.boot_id,
                      'native_primary':None if primary is None else type(primary).__name__,
                      'mono_ns':time.monotonic_ns(),'process_cpu_ns':time.process_time_ns(),
                      'partial':state['partial'],'thread_errors':state['thread_errors'],
                      'other_live_thread_count':alive_count,'guard_counts':dict(guard.counts),
                      'qualified':False,'scope':'native-return diagnostics ONLY; no work/readiness/actor/exit certificate'})
        except BaseException as error:
            if primary is not None:
                primary.add_note('root diagnostic publication failed: '+type(error).__name__)
            else:
                raise
    if state['partial']:
        raise RuntimeError('root diagnostics partial; no retry or qualification')
