#!/usr/bin/env python3
"""Run the live read-only preview with a cancellable, heartbeating Clarp job."""
import argparse
import os
import pathlib
import signal
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',required=True)
    p.add_argument('--db',required=True)
    p.add_argument('--port',type=int,default=7699)
    p.add_argument('--library')
    p.add_argument('--learn',action='store_true')
    a=p.parse_args()
    if a.learn and not a.library:p.error('--learn requires --library')
    env={**os.environ,'CLARP_BACKGROUND_WORKER_PID':str(os.getpid())}
    def job(*args):
        return subprocess.run(['clarp-agent-bg',a.session,*args],env=env,
                              capture_output=True,text=True,timeout=20)
    registered=job('job-upsert','fleet-map-preview','service','Serving fleet map',
                   ('Live activity with autonomous source development' if a.learn else 'Read-only live activity preview') + '; managed by clarp-fleet-preview.service')
    if registered.returncode:
        print(registered.stderr or registered.stdout,file=sys.stderr);return 1
    handle=registered.stdout.strip()
    child=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).with_name('viz_preview.py')),
                            '--db',a.db,'--port',str(a.port)] +
                           (['--library',a.library] if a.library else []) + (['--learn'] if a.learn else []))
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
        child.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while True:
            try:
                code=child.wait(timeout=60)
                job('job-finish',handle) if stopping or code==0 else job('job-fail',handle,'preview process exited')
                if code<0:print(f'Preview child terminated by signal {-code}',file=sys.stderr,flush=True)
                return 0 if stopping else (128-code if code<0 else code)
            except subprocess.TimeoutExpired:
                if job('job-active',handle).returncode:
                    stop()
    finally:
        if child.poll() is None:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait()


if __name__=='__main__':
    raise SystemExit(main())
