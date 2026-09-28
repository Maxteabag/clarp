"""Disposable provider backlog/SQLite contention probe. Run in a fresh DB path."""
import json
import os
import pathlib
import statistics
import sys
import threading
import time
from datetime import datetime, timezone

assert os.environ.get('CLAUDE_PWA_DB'), 'set CLAUDE_PWA_DB to a new disposable file'
assert not pathlib.Path(os.environ['CLAUDE_PWA_DB']).exists(), 'refusing an existing database'
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'server'))
from lib import agents, backends, background_jobs, db, provider_background_jobs as provider
from lib.background_job_watcher import BackgroundJobWatcher

root = pathlib.Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
assert str(db.DB_PATH).startswith(str(root)), 'requires disposable database under probe root'
n = int(os.environ.get('PROBE_AGENTS','16'))
paths = {}
stamp = datetime.now(timezone.utc).isoformat()
for i in range(n):
    native = f'probe-native-{i:03d}'
    owner = agents.create_agent(persona=native,voice_id='',cwd=str(root),session=native)
    db.conn().execute('INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES(?,?,?,?)',
                     (owner,native,native,db.now_ms()-1000))
    path = root / f'{native}.jsonl'
    line = json.dumps({'type':'assistant','sessionId':native,'timestamp':stamp,
                       'message':{'content':[{'type':'text','text':'diagnostic output ' * 10}]}})+'\n'
    path.write_text(line * (8*1024*1024//len(line)+1))
    paths[native] = path
backends.by_id('claude').find_transcript = lambda sid: paths.get(sid)
managed = agents.create_agent(persona='managed',voice_id='',cwd=str(root),session='managed')
background_jobs.upsert(session="managed",job_id="managed-job",title="Managed probe")
latencies=[]
ready=threading.Event(); stop=threading.Event()
def writer():
    ready.set()
    while not stop.is_set():
        start=time.monotonic()
        background_jobs.upsert(session='managed',job_id='managed-job',title='Managed probe')
        latencies.append((time.monotonic()-start)*1000)
        stop.wait(.01)
worker=threading.Thread(target=writer);worker.start();ready.wait()
locks=[];begin=[None]
def trace(sql):
    if sql == 'BEGIN IMMEDIATE': begin[0]=time.monotonic()
    elif sql in ('COMMIT','ROLLBACK') and begin[0] is not None:
        locks.append((time.monotonic()-begin[0])*1000);begin[0]=None
db.conn().set_trace_callback(trace)
events=[]
class Stream:
    def broadcast(self,event):
        if event.get('type')=='background-job-updated': events.append(time.monotonic())
watcher=BackgroundJobWatcher(Stream())
start=time.monotonic()
if hasattr(watcher, '_tick'):
    watcher._tick()
else:
    provider.poll_once()
    watcher._poll_once()
elapsed=(time.monotonic()-start)*1000
stop.set();worker.join()
print(json.dumps({'agents':n,'input_bytes':sum(p.stat().st_size for p in paths.values()),
 'processed_bytes':sum(r[0] for r in db.conn().execute('SELECT position FROM provider_job_cursors')),
 'poll_and_managed_event_ms':round(elapsed,3),
 'first_managed_event_ms':round((events[0]-start)*1000,3) if events else None,
 'writer_transactions':len(locks),'max_writer_lock_ms':round(max(locks,default=0),3),
 'managed_registrations':len(latencies),'max_managed_registration_ms':round(max(latencies,default=0),3)},indent=2))
