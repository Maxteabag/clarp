#!/usr/bin/env python3
"""Release the dedicated fleet preview; default is a read-only deployment check.

--base identifies the checked-in Flow source the live library is expected to
match. Refuse learned drift, archive HEAD into permanent storage, publish Flow
with a backup, retain other views, and switch only clarp-fleet-preview.service.
"""
import argparse,io,json,pathlib,subprocess,tarfile

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base',required=True,help='Git commit whose Flow source must still match live')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args();home=pathlib.Path.home()
    def run(*args,**kw):return subprocess.run(args,check=True,**kw)
    def git(*args):return subprocess.check_output(['git',*args])
    sha=git('rev-parse','HEAD').decode().strip()
    if git('status','--porcelain').strip():raise RuntimeError('Commit or isolate changes before release')
    manifest=json.loads(git('show',a.base+':static/viz-flow/program.json'))
    base={n:git('show',a.base+':static/viz-flow/'+n).decode() for n in manifest['files']}
    data=home/'.local/share/clarp-fleet-preview';library=data/'library.json'
    unit=home/'.config/systemd/user/clarp-fleet-preview.service';original_unit=unit.read_text()
    old=next(l for l in original_unit.splitlines() if l.startswith('WorkingDirectory='))
    assert old.startswith('WorkingDirectory='+str(data/'releases')+'/')
    def check_live():
        value=json.loads(library.read_text())
        if value.get('program',{}).get('view')!='flow' or value['program']['files']!=base:
            raise RuntimeError('Live Flow has changed; reconcile learned source before release')
        return value
    current=check_live();print(json.dumps({'head':sha,'base':a.base,'library_revision':current['revision'],'apply':a.apply}),flush=True)
    if not a.apply:return
    release=data/'releases'/sha;release.mkdir(parents=True,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(git('archive',sha))) as archive:archive.extractall(release,filter='data')
    run('uv','sync','--frozen','--group','dev',cwd=release,capture_output=True)
    run('systemctl','--user','stop','clarp-fleet-preview')
    original_library=None
    try:
        before=check_live();original_library=library.read_bytes()
        run(str(release/'.venv/bin/python'),str(release/'scripts/viz_publish_flow.py'),'--library',str(library),'--allow-live',cwd=release)
        after=json.loads(library.read_text())
        assert after.get('view_programs',{}).get('world')==before.get('view_programs',{}).get('world')
        unit.write_text(original_unit.replace(old,'WorkingDirectory='+str(release)))
        run('systemctl','--user','daemon-reload')
    except Exception:
        if original_library is not None:library.write_bytes(original_library)
        unit.write_text(original_unit);run('systemctl','--user','daemon-reload');raise
    finally:run('systemctl','--user','start','clarp-fleet-preview')
    print('Released '+sha+'; verify live frames and service state before reporting success.')

if __name__=='__main__':main()
