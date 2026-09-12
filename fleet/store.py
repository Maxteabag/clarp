"""Transactional placement and durable ownership; no SSH or subprocess work here."""
import contextlib
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

ACTIVE = ('reserved', 'staging', 'running', 'cancelling', 'unknown')
TERMINAL = ('succeeded', 'failed', 'cancelled')
PROFILES = {'diagnostic', 'compile-cpp', 'cmake', 'render-cpu', 'render-gpu', 'whisper', 'whisper-gpu', 'agent'}


def validate(request):
    if request.get('profile') not in PROFILES:
        raise ValueError('Unknown execution profile')
    resources = request.setdefault('resources', {})
    for name, default, maximum in [('cpu', 1, 256), ('ram_mb', 512, 1048576), ('gpu_mb', 0, 1048576)]:
        value = resources.setdefault(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < (0 if name == 'gpu_mb' else .1) or value > maximum or not math.isfinite(value):
            raise ValueError('Invalid resource request: '+name)
    timeout = request.setdefault('timeout', 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int,float)) or not math.isfinite(timeout) or not 1 <= timeout <= 86400:
        raise ValueError('Timeout must be between 1 and 86400 seconds')
    parent = request.setdefault('parent', {})
    if not all(isinstance(parent.get(k),str) and parent[k] for k in ('host','agent','task')):
        raise ValueError('Parent host, agent and task are required')
    request.setdefault('priority', 'background')
    queue_timeout=request.setdefault('queue_timeout',10 if request['priority']=='interactive' else 3600)
    if isinstance(queue_timeout,bool) or not isinstance(queue_timeout,(int,float)) or not math.isfinite(queue_timeout) or not 1<=queue_timeout<=86400:raise ValueError('Invalid queue timeout')
    if request['priority'] not in ('interactive','background'):
        raise ValueError('Invalid priority')
    if request['profile'] == 'agent' and (not request.get('model') or not request.get('effort')):
        raise ValueError('Agent jobs need explicit model and effort')
    if request['priority']=='interactive' and request['profile'] not in {'whisper','whisper-gpu','diagnostic'}:
        raise ValueError('Interactive reservations are reserved for dictation')
    if request['profile']=='render-gpu' and resources['gpu_mb']<256:
        raise ValueError('GPU rendering needs an explicit GPU reservation of at least256MiB')
    if request['profile'].startswith('whisper') and (resources['cpu']<2 or resources['ram_mb']<2048):
        raise ValueError('Whisper reserves at least2 CPUs and2048MiB RAM')
    if request['profile']=='whisper-gpu' and resources['gpu_mb']<1024:
        raise ValueError('GPU Whisper reserves at least1024MiB VRAM')
    return request


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS peers(id TEXT PRIMARY KEY,config TEXT NOT NULL,telemetry TEXT NOT NULL DEFAULT '{}',observed REAL NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,draining INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,peer TEXT,attempt TEXT,created REAL NOT NULL,updated REAL NOT NULL,result TEXT,error TEXT NOT NULL DEFAULT '',acknowledged REAL,cancel_requested INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inputs(digest TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,at REAL NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL);
            ''')
            if 'overrides' not in [r[1] for r in c.execute('PRAGMA table_info(peers)')]:c.execute("ALTER TABLE peers ADD COLUMN overrides TEXT NOT NULL DEFAULT '{}'")
            c.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('broker_id',uuid.uuid4().hex))
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=10)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        try:
            with c: yield c
        finally:c.close()

    def broker_id(self):
        with self.db() as c:return c.execute("SELECT value FROM meta WHERE key='broker_id'").fetchone()[0]

    def peers_configure(self, peers):
        with self.db() as c:
            for supplied in peers:
                p=json.loads(json.dumps(supplied));p['maximum_capacity']=dict(p['capacity'])
                old=c.execute('SELECT overrides FROM peers WHERE id=?',(p['id'],)).fetchone();overrides=json.loads(old[0]) if old else {}
                for key,value in overrides.items():
                    if key in {'cpu','ram_mb'}:p['capacity'][key]=min(value,p['maximum_capacity'][key])
                    else:p[key]=min(value,p['maximum_capacity']['cpu' if key=='interactive_cpu' else 'ram_mb'])
                c.execute('INSERT INTO peers(id,config,enabled) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,enabled=excluded.enabled',
                          (p['id'],json.dumps(p),int(p.get('enabled',True))))

    def observe(self, peer, telemetry):
        valid=isinstance(telemetry,dict)
        if valid:
            valid=isinstance(telemetry.get('profiles',[]),list) and all(isinstance(p,str) for p in telemetry.get('profiles',[]))
            for k in ['ram_available_mb','ram_total_mb','gpu_free_mb','gpu_total_mb','cpu_busy_pct','logical_cpus']:
                v=telemetry.get(k)
                if v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0):valid=False
        if not valid:telemetry={'reachable':False,'error':'Invalid peer telemetry'}
        with self.db() as c:
            c.execute('UPDATE peers SET telemetry=?,observed=? WHERE id=?',(json.dumps(telemetry),time.time(),peer))

    def expire_queued(self):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT id FROM jobs WHERE status='queued' AND created + coalesce(json_extract(request,'$.queue_timeout'),3600) < ?",(time.time(),)).fetchall()
            for row in rows:
                c.execute("UPDATE jobs SET status='failed',error='Queue deadline exceeded',updated=? WHERE id=?",(time.time(),row[0]));self.event(c,row[0],'failed','Queue deadline exceeded')

    def set_limits(self,peer,changes):
        allowed={'cpu','ram_mb','interactive_cpu','interactive_ram_mb'}
        if not changes or set(changes)-allowed:raise ValueError('Invalid limit fields')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT config,overrides FROM peers WHERE id=?',(peer,)).fetchone()
            if row is None:raise ValueError('Unknown peer')
            p=json.loads(row[0]);maximum=p.setdefault('maximum_capacity',dict(p['capacity']))
            for key,value in changes.items():
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise ValueError('Invalid limit')
                bound=maximum['cpu'] if key in {'cpu','interactive_cpu'} else maximum['ram_mb']
                if value>bound:raise ValueError('Limit exceeds enrolled capacity')
                if key in {'cpu','ram_mb'}:p['capacity'][key]=value
                else:p[key]=value
            overrides=json.loads(row[1]);overrides.update(changes)
            c.execute('UPDATE peers SET config=?,overrides=? WHERE id=?',(json.dumps(p),json.dumps(overrides),peer))
            return p

    def drain(self, peer, value):
        with self.db() as c:
            if c.execute('UPDATE peers SET draining=? WHERE id=?',(int(value),peer)).rowcount != 1:raise ValueError('Unknown peer')

    def submit(self, request, key=None):
        request=validate(dict(request))
        encoded=json.dumps(request,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        files=request.pop('files',[])
        bundle=json.dumps(files,separators=(',',':'))
        input_ref=hashlib.sha256(bundle.encode()).hexdigest()
        request['input_ref']=input_ref
        encoded=json.dumps(request,sort_keys=True,separators=(',',':'))
        job=key or uuid.uuid4().hex
        if not job or len(job)>128 or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for ch in job):raise ValueError('Invalid job id')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT request_hash FROM jobs WHERE id=?',(job,)).fetchone()
            if old:
                if old[0]!=digest:raise ValueError('Job id already has a different request')
                return job
            if c.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]>=1000:
                raise ValueError('Queue full; retry the same job id after backpressure clears')
            if not c.execute('SELECT 1 FROM inputs WHERE digest=?',(input_ref,)).fetchone():
                used=c.execute('SELECT coalesce(sum(length(payload)),0) FROM inputs').fetchone()[0]
                if used+len(bundle)>512*1024*1024:raise ValueError('Input cache full; retain results and prune explicitly before submitting new source')
                c.execute('INSERT INTO inputs VALUES(?,?)',(input_ref,bundle))
            now=time.time()
            c.execute('INSERT INTO jobs(id,request_hash,request,status,created,updated) VALUES(?,?,?,?,?,?)',(job,digest,encoded,'queued',now,now))
            self.event(c,job,'queued','Submitted by '+request['parent']['host']+'/'+request['parent']['agent'])
        return job

    @staticmethod
    def event(c, job, status, detail):
        c.execute('INSERT INTO events(job,at,status,detail) VALUES(?,?,?,?)',(job,time.time(),status,detail))

    def _choices(self,c,request,now):
        choices=[]
        for row in c.execute('SELECT * FROM peers ORDER BY id'):
            p=json.loads(row['config']);t=json.loads(row['telemetry']);reasons=[]
            used={'cpu':0.,'ram_mb':0.,'gpu_mb':0.};count=0;interactive=0
            for j in c.execute("SELECT request FROM jobs WHERE peer=? AND status IN ('reserved','staging','running','cancelling','unknown')",(row['id'],)):
                count+=1
                if json.loads(j[0]).get('priority')=='interactive':interactive+=1
                for k,v in json.loads(j[0])['resources'].items():
                    if k in used:used[k]+=v
            if not row['enabled']:reasons.append('not enrolled for execution')
            if row['draining']:reasons.append('draining')
            if not t.get('reachable'):reasons.append('unreachable')
            if now-row['observed']>p.get('stale_seconds',30):reasons.append('stale telemetry')
            if request.get('peer') and request['peer']!=row['id']:reasons.append('different requested peer')
            if request['profile'] not in t.get('profiles',[]):reasons.append('profile unavailable')
            if request.get('os') and request['os']!=t.get('os'):reasons.append('OS mismatch')
            if request.get('arch') and request['arch']!=t.get('arch'):reasons.append('architecture mismatch')
            if t.get('on_battery') and not p.get('allow_battery',False):reasons.append('battery policy')
            if request['priority']=='background' and ((t.get('cpu_busy_pct') or 0)>=90 or (t.get('load',[0])[0]/max(1,t.get('logical_cpus') or 1)>1.5)):
                reasons.append('host CPU pressure protects interactive work')
            if request['priority']=='interactive':
                if interactive>=1 or count>=p.get('max_jobs',2)+1:reasons.append('interactive execution slot occupied')
            elif count-interactive>=p.get('max_jobs',2):reasons.append('background job concurrency limit')
            pool=p.get('capacity',{})
            available={k:float(pool.get(k,0))-used[k] for k in used}
            # Physical available memory already includes current usage; take the
            # minimum instead of subtracting the same allocation twice.
            if t.get('ram_available_mb') is None:reasons.append('RAM availability unknown')
            else:available['ram_mb']=min(available['ram_mb'],t['ram_available_mb']-p.get('ram_headroom_mb',2048))
            if request['priority']=='background':
                available['cpu']-=p.get('interactive_cpu',1)
                available['ram_mb']-=p.get('interactive_ram_mb',512)
            if request['resources']['gpu_mb']:
                if used['gpu_mb']>0:reasons.append('GPU execution slot occupied')
                if t.get('gpu_free_mb') is None:reasons.append('GPU memory unknown')
                else:available['gpu_mb']=min(available['gpu_mb'],t['gpu_free_mb']-p.get('gpu_headroom_mb',256))
                if t.get('unified_memory'):
                    available['ram_mb']-=request['resources']['gpu_mb']
            for k,v in request['resources'].items():
                if k in available and v>available[k]:reasons.append('insufficient '+k)
            # Explicit estimate, not a measured completion-time prediction.
            score=count*10+(float(t['cpu_busy_pct'])/10 if t.get('cpu_busy_pct') is not None else 15)+float(p.get('placement_penalty',0))
            choices.append({'peer':row['id'],'eligible':not reasons,'reasons':reasons,'available':available,'reserved':used,'active_jobs':count,'score':round(score,2)})
        return sorted(choices,key=lambda x:(not x['eligible'],x['score'],x['peer']))

    def choices(self, request):
        request=validate(dict(request))
        with self.db() as c:return self._choices(c,request,time.time())

    def reserve_next(self):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY CASE json_extract(request,'$.priority') WHEN 'interactive' THEN 0 ELSE 1 END,created").fetchall()
            parent_counts={}
            for active in c.execute("SELECT request FROM jobs WHERE status IN ('reserved','staging','running','cancelling','unknown')"):
                parent=json.loads(active[0])['parent'];key=(parent['host'],parent['agent']);parent_counts[key]=parent_counts.get(key,0)+1
            def order(row):
                r=json.loads(row['request']);key=(r['parent']['host'],r['parent']['agent']);return (r['priority']!='interactive',parent_counts.get(key,0),row['created'])
            rows.sort(key=order)
            for row in rows:
                request=json.loads(row['request']);key=(request['parent']['host'],request['parent']['agent'])
                if parent_counts.get(key,0)>=4:continue
                choices=self._choices(c,request,time.time())
                target=next((x for x in choices if x['eligible']),None)
                if not target:continue
                attempt=uuid.uuid4().hex
                c.execute("UPDATE jobs SET peer=?,attempt=?,status='reserved',updated=? WHERE id=?",(target['peer'],attempt,time.time(),row['id']))
                self.event(c,row['id'],'reserved',json.dumps(target))
                return self.get_from(c,row['id'])
        return None

    @staticmethod
    def get_from(c,job):
        row=c.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
        if not row:raise ValueError('Unknown job')
        x=dict(row);x['request']=json.loads(x['request']);x['result']=json.loads(x['result']) if x['result'] else None
        return x

    def input_files(self,reference):
        with self.db() as c:
            row=c.execute('SELECT payload FROM inputs WHERE digest=?',(reference,)).fetchone()
            if row is None:raise ValueError('Missing immutable input bundle')
            return json.loads(row[0])

    def get(self,job):
        with self.db() as c:
            x=self.get_from(c,job);x['events']=[dict(r) for r in c.execute('SELECT at,status,detail FROM events WHERE job=? ORDER BY seq DESC LIMIT 20',(job,))];return x

    def transition(self,job,attempt,status,result=None,error=''):
        error=str(error)[:2048]
        if status not in ACTIVE+TERMINAL:raise ValueError('Invalid execution state')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');old=self.get_from(c,job)
            if old['attempt']!=attempt:return False
            if old['status'] in TERMINAL:return old['status']==status
            c.execute('UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?',(status,json.dumps(result) if result is not None else old['result'] and json.dumps(old['result']),error,time.time(),job))
            if old['status']!=status or old['error']!=error or result is not None:self.event(c,job,status,error or 'Executor update')
            return True

    def cancel(self,job):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');old=self.get_from(c,job)
            if old['status'] in TERMINAL:return old
            status='cancelled' if old['status']=='queued' else 'cancelling'
            c.execute('UPDATE jobs SET cancel_requested=1,status=?,updated=? WHERE id=?',(status,time.time(),job));self.event(c,job,status,'Cancellation requested')
        return self.get(job)

    def acknowledge(self,job,parent):
        with self.db() as c:
            x=self.get_from(c,job)
            if x['request']['parent']!=parent:raise ValueError('Result belongs to a different parent')
            if x['status'] not in TERMINAL:raise ValueError('Result is not terminal')
            c.execute('UPDATE jobs SET acknowledged=? WHERE id=?',(time.time(),job))

    def snapshot(self):
        with self.db() as c:
            peers=[]
            for r in c.execute('SELECT * FROM peers'):
                p=dict(r);p['config']=json.loads(p['config']);p['telemetry']=json.loads(p['telemetry']);p['age_seconds']=round(time.time()-p['observed'],1)
                totals=c.execute("SELECT count(*),coalesce(sum(json_extract(request,'$.resources.cpu')),0),coalesce(sum(json_extract(request,'$.resources.ram_mb')),0),coalesce(sum(json_extract(request,'$.resources.gpu_mb')),0) FROM jobs WHERE peer=? AND status IN ('reserved','staging','running','cancelling','unknown')",(p['id'],)).fetchone()
                p['reserved']=dict(zip(['jobs','cpu','ram_mb','gpu_mb'],totals));peers.append(p)
            jobs=[]
            for row in c.execute("SELECT id,peer,attempt,status,created,updated,request,error,cancel_requested,acknowledged,json_remove(result,'$.output','$.artifacts') AS result FROM jobs ORDER BY created DESC LIMIT 50"):
                j=dict(row);j['request']=json.loads(j['request']);j['result']=json.loads(j['result']) if j['result'] else None;jobs.append(j)
            counts=dict(c.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
        for job in jobs:job['request'].pop('files',None)
        return {'protocol':1,'broker_id':self.broker_id(),'observed_at':time.time(),'peers':peers,'jobs':jobs,'counts':counts}
