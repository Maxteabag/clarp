"""Optional component-local tool dependencies and real backend validation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import remote


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--ffmpeg',action='store_true');p.add_argument('--cuda',action='store_true');p.add_argument('--verify-cuda-audio',type=Path);a=p.parse_args()
    root=Path.home()/'.local/lib/clarp-fleet/tools';cfg=remote.config()
    if a.ffmpeg:
        target=root/'python'
        if not (target/'imageio_ffmpeg').exists():subprocess.run(['uv','pip','install','--target',str(target),'imageio-ffmpeg==0.6.0'],check=True)
        sys.path.insert(0,str(target));import imageio_ffmpeg
        exe=imageio_ffmpeg.get_ffmpeg_exe();subprocess.run([exe,'-version'],check=True,capture_output=True)
        cfg['ffmpeg_bin']=exe;cfg['ffmpeg_receipt']={'package':'imageio-ffmpeg==0.6.0','sha256':hashlib.sha256(Path(exe).read_bytes()).hexdigest()}
    if a.cuda:
        target=root/'cuda'
        if not (target/'nvidia/cudnn/lib/libcudnn.so.9').exists() or not (target/'nvidia/cublas/lib/libcublas.so.12').exists():
            subprocess.run(['uv','pip','install','--target',str(target),'nvidia-cublas-cu12==12.4.5.8','nvidia-cudnn-cu12==9.1.0.70'],check=True)
        cfg['cuda_libraries']=[str(target/'nvidia/cublas/lib'),str(target/'nvidia/cudnn/lib')]
    remote.write(remote.ROOT/'config.json',cfg)
    if a.verify_cuda_audio:
        env=os.environ.copy();env['LD_LIBRARY_PATH']=':'.join(cfg.get('cuda_libraries',[]))
        script="from faster_whisper import WhisperModel; import sys,json,time; t=time.monotonic(); m=WhisperModel(sys.argv[1],device='cuda',compute_type='int8_float32',local_files_only=True,cpu_threads=2); seg,_=m.transcribe(sys.argv[2],beam_size=1); print(json.dumps({'text':' '.join(s.text.strip() for s in seg),'seconds':time.monotonic()-t}))"
        run=subprocess.run([sys.executable,'-c',script,remote.model_path(),str(a.verify_cuda_audio)],env=env,text=True,capture_output=True,timeout=60)
        if run.returncode:raise RuntimeError(run.stderr[-1500:])
        result=json.loads(run.stdout);cfg['whisper_cuda_verified']=True;cfg['cuda_verification']={'at':time.time(),'model':remote.model_path(),'audio_sha256':hashlib.sha256(a.verify_cuda_audio.read_bytes()).hexdigest(),'result':result};remote.write(remote.ROOT/'config.json',cfg)
    print(json.dumps({k:v for k,v in cfg.items() if k in {'ffmpeg_receipt','cuda_libraries','cuda_verification','whisper_cuda_verified'}},indent=2))


if __name__=='__main__':main()
