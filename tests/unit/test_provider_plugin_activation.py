"""Release-pointer activation without restarting a running runtime or using a model."""
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[2]


def test_already_loaded_runtime_resolves_next_plugin_and_fresh_hook_from_current(tmp_path):
    share=tmp_path/'share';old=share/'releases'/'old';new=share/'releases'/'new'
    for release in (old,new):
        shutil.copytree(ROOT/'server/lib',release/'lib',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT/'plugin',release/'plugin',ignore=shutil.ignore_patterns('__pycache__'))
    (old/'plugin/hooks/tool_activity.py').write_text('# old pinned hook fixture\n')
    (share/'current').symlink_to(old,target_is_directory=True)
    (share/'plugin').symlink_to(share/'current/plugin',target_is_directory=True)
    env={**os.environ,'PYTHONPATH':str(old),'CLARP_SHARE_DIR':str(share)}
    # Import once, wait across activation, and resolve again from the SAME process.
    code='''import json,sys
from lib import deployment
for _ in range(2):
 p=deployment.plugin_dir()
 print(json.dumps({'module':deployment.__file__,'path':str(p),'resolved':str(p.resolve())}),flush=True)
 sys.stdin.readline()
'''
    process=subprocess.Popen([sys.executable,'-u','-c',code],env=env,
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        before=json.loads(process.stdout.readline())
        assert before['resolved']==str(old/'plugin')
        next_link=share/'next';next_link.symlink_to(new,target_is_directory=True)
        next_link.replace(share/'current')
        process.stdin.write('\n');process.stdin.flush()
        after=json.loads(process.stdout.readline())
        assert after['module']==before['module'] and str(old) in after['module']
        assert after['path']==before['path'] and after['resolved']==str(new/'plugin')
        # A new hook process must import its colocated new lib despite a stale
        # inherited PYTHONPATH, exactly as _clarp_lib resolves __file__.
        hook=pathlib.Path(after['path'])/'hooks/_clarp_lib.py'
        resolution=subprocess.check_output([sys.executable,'-c',
            'import runpy,sys;runpy.run_path(sys.argv[1]);import lib.background_jobs as j;print(j.__file__)',str(hook)],
            env=env,text=True,timeout=10).strip()
        assert resolution==str(new/'lib/background_jobs.py')
        assert 'tool_input_with_origin' in (pathlib.Path(after['path'])/'hooks/tool_activity.py').read_text()
    finally:
        process.stdin.close();process.wait(timeout=5)
