"""A sampled context survives bounded error tails without widening every read."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from lib import claude_transcript as transcript
from tests.unit.test_transcript_usage_cache import _usage


def _errors(count):
    record={'type':'assistant','uuid':'synthetic-error','timestamp':'2026-11-17T09:00:00Z',
            'message':{'role':'assistant','model':'<synthetic>',
                       'content':[{'type':'text','text':'Synthetic provider failure '*12}],
                       'usage':{'input_tokens':0,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}}}
    return (json.dumps(record)+'\n') * count


def _initial(path, tokens=160):
    path.write_text(json.dumps({'type':'progress','data':'x'*200_000})+'\n'+_usage(tokens))


def _clock(monkeypatch, path, mode):
    monkeypatch.setattr(transcript.time,'time_ns',
        lambda:path.stat().st_mtime_ns+(transcript._RACY_NS+1 if mode=='settled' else 1))


def _count_reads(monkeypatch, path):
    reads=[]; original=Path.open
    class Counted:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):return self
        def __exit__(self,*args):self.handle.close()
        def __getattr__(self,name):return getattr(self.handle,name)
        def read(self,*args):
            data=self.handle.read(*args);reads.append(len(data));return data
    def open_counted(self,*args,**kwargs):
        handle=original(self,*args,**kwargs)
        return Counted(handle) if self==path and args and args[0]=='rb' else handle
    monkeypatch.setattr(Path,'open',open_counted)
    return reads


@pytest.mark.parametrize('tokens',[0,160])
@pytest.mark.parametrize('mode',['settled','racy'])
def test_growing_5000_error_storm_retains_real_usage_at_one_window(tmp_path,monkeypatch,mode,tokens):
    path=tmp_path/'storm.jsonl';_initial(path,tokens);_clock(monkeypatch,path,mode)
    reads=_count_reads(monkeypatch,path)
    assert transcript.context_tokens_from_jsonl(path)==tokens
    for _ in range(5):
        with path.open('a') as stream:stream.write(_errors(1000))
        reads.clear()
        assert transcript.context_tokens_from_jsonl(path)==tokens
        assert sum(reads)<=65_536
        reads.clear()
        assert transcript.context_tokens_from_jsonl(path)==tokens
        assert sum(reads)==(0 if mode=='settled' else 65_536)
    assert path.stat().st_size>1_000_000
    # A genuinely cold process cannot recover a real sample outside its budget.
    script='''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]);from lib import claude_transcript as c
p=pathlib.Path(sys.argv[2]);real=pathlib.Path.open;reads=[]
class Counted:
 def __init__(self,h):self.h=h
 def __enter__(self):return self
 def __exit__(self,*a):self.h.close()
 def __getattr__(self,n):return getattr(self.h,n)
 def read(self,*a):v=self.h.read(*a);reads.append(len(v));return v
def opened(self,*a,**k):
 h=real(self,*a,**k);return Counted(h) if self==p else h
pathlib.Path.open=opened
print(json.dumps({'value':c.context_tokens_from_jsonl(p),'bytes':sum(reads)}))
'''
    result=subprocess.run([sys.executable,'-c',script,str(Path(__file__).resolve().parents[2]/'server'),str(path)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'value':None,'bytes':1_327_680}


@pytest.mark.parametrize('mode',['settled','racy'])
@pytest.mark.parametrize('operation',['truncate','replace'])
def test_reset_file_cannot_reuse_an_old_context_sample(tmp_path,monkeypatch,mode,operation):
    path=tmp_path/'reset.jsonl';_initial(path);_clock(monkeypatch,path,mode)
    assert transcript.context_tokens_from_jsonl(path)==160
    found_size=path.stat().st_size
    with path.open('a') as stream:stream.write(_errors(1000))
    assert transcript.context_tokens_from_jsonl(path)==160
    replacement=_errors(500)
    # The shrink remains ABOVE the size where the real answer was sampled.
    assert found_size<len(replacement.encode())<path.stat().st_size
    if operation=='truncate':
        inode=path.stat().st_ino;path.write_text(replacement);assert path.stat().st_ino==inode
    else:
        newer=tmp_path/'new.jsonl';newer.write_text(replacement);newer.replace(path)
    assert transcript.context_tokens_from_jsonl(path) is None


@pytest.mark.parametrize('mode',['settled','racy'])
@pytest.mark.parametrize('compact',[False,True],ids=['new-real','compact'])
def test_new_real_sample_wins_and_survives_the_next_storm(tmp_path,monkeypatch,mode,compact):
    path=tmp_path/'new-real.jsonl';_initial(path);_clock(monkeypatch,path,mode)
    assert transcript.context_tokens_from_jsonl(path)==160
    with path.open('a') as stream:
        if compact:stream.write(json.dumps({'type':'system','subtype':'compact_boundary'})+'\n')
        stream.write(_errors(5000))
    assert transcript.context_tokens_from_jsonl(path)==160  # last measured pre-compact usage
    with path.open('a') as stream:stream.write(_usage(23))
    assert transcript.context_tokens_from_jsonl(path)==23
    with path.open('a') as stream:stream.write(_errors(5000))
    assert transcript.context_tokens_from_jsonl(path)==23


def test_context_memory_is_per_transcript_identity(tmp_path):
    first=tmp_path/'first.jsonl';second=tmp_path/'second.jsonl';_initial(first)
    assert transcript.context_tokens_from_jsonl(first)==160
    second.write_text(_errors(5000))
    assert transcript.context_tokens_from_jsonl(second) is None
    with second.open('a') as stream:stream.write(_usage(23))
    assert transcript.context_tokens_from_jsonl(second)==23
    with first.open('a') as stream:stream.write(_errors(5000))
    assert transcript.context_tokens_from_jsonl(first)==160


def test_context_memory_is_bounded_to_8192_file_identities(tmp_path):
    paths=[]
    for n in range(8193):
        path=tmp_path/f'{n}.jsonl';path.write_text(_usage(n+1));paths.append(path)
        assert transcript.context_tokens_from_jsonl(path)==n+1
    for path,expected in ((paths[0],None),(paths[-1],8193)):
        with path.open('a') as stream:stream.write(_errors(5000))
        assert transcript.context_tokens_from_jsonl(path)==expected


def test_delayed_older_read_cannot_replace_a_newer_observed_real_usage(tmp_path,monkeypatch):
    path=tmp_path/'racing.jsonl';_initial(path);_clock(monkeypatch,path,'racy')
    assert transcript.context_tokens_from_jsonl(path)==160
    observed=threading.Event();release=threading.Event();paused=[];usage=transcript._usage_tokens
    def gated(line):
        value=usage(line)
        if threading.current_thread().name=='old-context' and value==160 and not paused:
            paused.append(True);observed.set();assert release.wait(5)
        return value
    monkeypatch.setattr(transcript,'_usage_tokens',gated)
    def older():
        threading.current_thread().name='old-context'
        return transcript.context_tokens_from_jsonl(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old=pool.submit(older)
        try:
            assert observed.wait(3)
            with path.open('a') as stream:stream.write(_usage(23))
            assert pool.submit(transcript.context_tokens_from_jsonl,path).result(timeout=1)==23
            with path.open('a') as stream:stream.write(_errors(5000))
        finally:release.set()
        assert old.result(timeout=5)==23
    assert transcript.context_tokens_from_jsonl(path)==23
