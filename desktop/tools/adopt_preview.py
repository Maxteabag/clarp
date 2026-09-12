#!/usr/bin/env python3
"""Open a preserved copy of an older preview window; never close the original."""
import argparse
import configparser
import tomllib
import urllib.request
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def process_identity(pid, proc_root=Path('/proc')):
    root=proc_root/str(pid)
    if root.stat().st_uid != os.getuid():
        raise ValueError('The source process is not owned by this user')
    start=(root/'stat').read_text().rsplit(')',1)[1].split()[19]
    executable=Path(os.readlink(root/'exe').removesuffix(' (deleted)'))
    if executable.name != 'clarp-desktop':
        raise ValueError('The source process is not a Clarp desktop')
    return {'pid':pid,'start_time':start,'binary_sha256':digest(root/'exe')}


def process_environment(pid):
    return dict(v.split('=',1) for v in (Path('/proc')/str(pid)/'environ').read_bytes().decode().split('\0') if '=' in v)


def source_window(pid, title):
    clients=json.loads(subprocess.check_output(['hyprctl','clients','-j'],timeout=3))
    windows=[c for c in clients if c.get('pid')==pid and 'clarp' in c.get('class','').lower()]
    if len(windows)!=1 or windows[0].get('title')!=title:
        raise ValueError('Source window identity changed or is ambiguous; inspect it again')
    return windows[0]


def verify_session(pid, session, host, title, config, namespace):
    settings=configparser.ConfigParser(interpolation=None)
    settings.read(config/'MaxTeaBag'/(namespace+'.conf'))
    env=process_environment(pid)
    configured=env.get('CLARP_BASE_URL', settings.get('connection','baseUrl',fallback='http://127.0.0.1:7682')).rstrip('/')
    if host.rstrip('/') != configured:
        raise ValueError('Host is not the source process configured Host')
    token=env.get('CLARP_TOKEN')
    if not token:
        try: token=tomllib.loads((config/'clarp'/'config.toml').read_text()).get('auth_token')
        except (OSError,ValueError): pass
    if not token:
        result=subprocess.run(['secret-tool','lookup','application','com.maxteabag.Clarp','server',host],capture_output=True,text=True,timeout=3)
        token=result.stdout.strip() if result.returncode==0 else ''
    if not token:
        raise ValueError('Cannot verify source session without configured Host credentials')
    request=urllib.request.Request(host.rstrip('/')+'/agents/snapshot',headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(request,timeout=8) as response:
        agents=json.load(response).get('agents',[])
    matches=[agent for agent in agents if (agent.get('persona') or agent.get('session',''))+' — Clarp'==title]
    if len(matches)!=1 or matches[0].get('session')!=session:
        raise ValueError('Window title does not uniquely identify the requested session on its Host')
    return {'session':session,'agent_id':matches[0].get('agent_id',''),'title':title,'host':configured}


def copy_settings(source, destination):
    """Byte-preserving clone; a changing source is rejected, never overwritten."""
    before=source.read_bytes()
    time.sleep(.1)
    if source.read_bytes()!=before:
        raise ValueError('Draft/settings changed while preparing; try again when typing is paused')
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    destination.write_bytes(before);destination.chmod(0o600)
    if source.read_bytes()!=before:
        destination.unlink()
        raise ValueError('Draft/settings changed while copying; no adoption launched')
    return hashlib.sha256(before).hexdigest()


def prepare(pid, session, host, title, binary, root):
    if not session or not host.startswith(('http://','https://')):
        raise ValueError('An exact session and originating Host URL are required')
    identity=process_identity(pid)
    window=source_window(pid,title)
    env=process_environment(pid)
    if env.get('CLARP_BASE_URL') and env['CLARP_BASE_URL'].rstrip('/')!=host.rstrip('/'):
        raise ValueError('The supplied Host differs from the source process Host')
    config=Path(env.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))
    namespace='ClarpScreenshot' if env.get('CLARP_SCREENSHOT_PATH') else 'Clarp'
    source=config/'MaxTeaBag'/(namespace+'.conf')
    if not source.is_file():
        raise ValueError('Source settings are unavailable; original window kept')
    verified=verify_session(pid,session,host,title,config,namespace)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    folder=Path(tempfile.mkdtemp(prefix=str(pid)+'-',dir=root));folder.chmod(0o700)
    target=folder/'config'/'MaxTeaBag'/'Clarp.conf'
    snapshot=digest(source)
    if copy_settings(source,target)!=snapshot:
        raise ValueError('Settings changed during capture; original window kept')
    # Credentials remain in their existing configured store. No tokens in receipts.
    if (config/'clarp').exists():
        (folder/'config'/'clarp').symlink_to((config/'clarp').resolve())
    if process_identity(pid)!=identity or source_window(pid,title)['address']!=window['address']:
        raise ValueError('Source window changed during capture; original window kept')
    binary=Path(binary).resolve(strict=True)
    receipt={'source':identity,'source_title':title,'source_address':window['address'],
             'workspace':window['workspace']['id'],'session':session,'host':host.rstrip('/'),
             'verified_identity':verified,'source_settings':str(source),'settings_sha256':snapshot,
             'config_home':str(folder/'config'),'namespace':namespace,'target_namespace':'Clarp',
             'binary':str(binary),'binary_sha256':digest(binary),
             'original_window_closed':False,'mode':'side-by-side-preserved-copy'}
    manifest=folder/'adoption.json';manifest.write_text(json.dumps(receipt,indent=2));manifest.chmod(0o600)
    return manifest,receipt


def launch_environment(receipt, environment):
    env=dict(environment)
    # One-shot screenshot/launch flags from an old process must not leak into
    # normal adoption. Screenshot tests add their own isolated flags explicitly.
    for key in list(env):
        if key.startswith('CLARP_SCREENSHOT_'):
            env.pop(key)
    env.pop('CLARP_INSTANCE_NAME',None)
    env.update(XDG_CONFIG_HOME=receipt['config_home'],CLARP_BASE_URL=receipt['host'],
               CLARP_RESTORE_DESKTOP='1',CLARP_RESTORE_SESSION=receipt['session'])
    return env


def validate(receipt):
    if process_identity(receipt['source']['pid'])!=receipt['source']:
        raise ValueError('Source process changed; original window kept')
    window=source_window(receipt['source']['pid'],receipt['source_title'])
    if window['address']!=receipt['source_address'] or window['workspace']['id']!=receipt['workspace']:
        raise ValueError('Source window moved or changed; prepare adoption again')
    config=Path(receipt['source_settings']).parent.parent
    if verify_session(receipt['source']['pid'],receipt['session'],receipt['host'],receipt['source_title'],config,receipt['namespace'])!=receipt['verified_identity']:
        raise ValueError('Host session identity changed; prepare adoption again')
    if digest(receipt['source_settings'])!=receipt['settings_sha256']:
        raise ValueError('Draft/settings changed after capture; prepare adoption again')
    target=Path(receipt['config_home'])/'MaxTeaBag'/(receipt.get('target_namespace',receipt['namespace'])+'.conf')
    if digest(target)!=receipt['settings_sha256'] or digest(receipt['binary'])!=receipt['binary_sha256']:
        raise ValueError('Prepared settings or selected preview changed; prepare adoption again')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-pid',type=int)
    parser.add_argument('--session')
    parser.add_argument('--host')
    parser.add_argument('--expect-title')
    parser.add_argument('--binary',type=Path,default=Path.home()/'.local/lib/clarp-desktop-preview/clarp-desktop')
    parser.add_argument('--root',type=Path,default=Path.home()/'.local/state/clarp/preview-adoptions')
    parser.add_argument('--launch',action='store_true',help='Open the preserved copy on the same workspace without taking focus')
    parser.add_argument('--execute',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:
        if args.execute:
            if args.execute.stat().st_uid!=os.getuid() or args.execute.stat().st_mode & 0o077:
                raise ValueError('Adoption manifest must be private and owned by this user')
            receipt=json.loads(args.execute.read_text());validate(receipt)
            env=launch_environment(receipt,os.environ)
            # Read any environment-only auth at execution time, never into argv
            # or the saved receipt. The original remains alive throughout.
            source_env=process_environment(receipt['source']['pid'])
            for name in ('CLARP_TOKEN','CLARP_SHARED_FILESYSTEM_HOST'):
                if name in source_env:env[name]=source_env[name]
            os.execve(receipt['binary'],[receipt['binary'],'--no-new-agent'],env)
        if not all((args.from_pid,args.session,args.host,args.expect_title)):
            parser.error('--from-pid, --session, --host and --expect-title are required')
        manifest,receipt=prepare(args.from_pid,args.session,args.host,args.expect_title,args.binary,args.root)
        if args.launch:
            validate(receipt)
            command=shlex.join([sys.executable,str(Path(__file__).resolve()),'--execute',str(manifest)])
            subprocess.run(['hyprctl','dispatch','exec',f"[workspace {receipt['workspace']} silent; noinitialfocus] "+command],check=True,capture_output=True,timeout=3)
        print(json.dumps({'manifest':str(manifest),'launched':args.launch,**receipt},indent=2))
        return 0
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        if args.execute:
            status=args.execute.with_name('launch-error.txt')
            status.write_text(str(error));status.chmod(0o600)
        print(str(error),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
