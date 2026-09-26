"""SQLite owns queue, viewer demand, release fences, worker leases and failures.

No model call occurs in a transaction. Work is claimed atomically and stale
completions are rejected by owner token. A running job gets 60s (model timeout
45s); after a crash it can be reclaimed only while some view still needs it.
"""
from contextlib import contextmanager
import json
from .db import conn
from . import tool_explanation_cache as cache


@contextmanager
def transaction():
    db=conn()
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def _prune(db, now):
    db.execute('DELETE FROM tool_explanation_cache WHERE expires_at<=?',(now,))
    db.execute('DELETE FROM tool_explanation_releases WHERE expires_at<=?',(now,))
    db.execute('DELETE FROM tool_explanation_demands WHERE expires_at<=?',(now,))
    db.execute("UPDATE tool_explanation_jobs SET status='queued',owner='',lease_until=0 WHERE status='running' AND lease_until<=?",(now,))
    db.execute("DELETE FROM tool_explanation_jobs WHERE status='failed' AND available_at<=?",(now,))
    db.execute("DELETE FROM tool_explanation_jobs WHERE status='queued' AND NOT EXISTS (SELECT 1 FROM tool_explanation_demands d WHERE d.cache_key=tool_explanation_jobs.cache_key)")


def _ready(row):
    # The producer is preserved; `cached` is separate provenance of this reply.
    return {'status':'ready','text':row[0],'source':row[1],'cached':True,'provenance':json.loads(row[2] or '{}')}


def released(demands):
    now=cache.now_ms()
    db=conn()
    return {d for d in demands if d and db.execute('SELECT 1 FROM tool_explanation_releases WHERE demand_id=? AND expires_at>?',(d,now)).fetchone()}


def request(level, prepared, releases, debounce, *, guard=None):
    """Answer each prepared item and record viewer demand for pending ones.

    Everything is read before the write lock: a poll whose items are ready,
    cancelled or disabled never takes it. The transaction holds only the
    writes plus `guard`, which must be cheap (compare a revision, never
    resolve a configuration). Expiry pruning belongs to `claim()`.
    """
    now=cache.now_ms()
    db=conn()
    released_now=set(releases)
    admitted=bool(level) and (guard is None or guard(db))
    queued=None
    added=0
    responses=[]
    writes=[]
    for identity,key,activity,demand in prepared:
        if demand and (demand in released_now or db.execute('SELECT 1 FROM tool_explanation_releases WHERE demand_id=? AND expires_at>?',(demand,now)).fetchone()):
            value={'status':'cancelled'}
        elif not admitted:
            value={'status':'disabled'}
        else:
            ready=db.execute('SELECT explanation,source,provenance_json FROM tool_explanation_cache WHERE cache_key=? AND expires_at>?',(key,now)).fetchone()
            job=None if ready else db.execute('SELECT status,failure_reason,available_at FROM tool_explanation_jobs WHERE cache_key=?',(key,)).fetchone()
            # An expired failure is pruned lazily; until then it is no job at all.
            if job and job[0]=='failed' and job[2]<=now:
                job=None
            if ready:
                value=_ready(ready)
            elif job and job[0]=='failed':
                value={'status':'failed','reason':job[1]}
            else:
                if not job and queued is None:
                    queued=db.execute("SELECT count(*) FROM tool_explanation_jobs WHERE status='queued'").fetchone()[0]
                if not job and queued+added>=64:
                    value={'status':'busy','reason':'queue_full'}
                else:
                    owner=demand or 'legacy'
                    exists=db.execute('SELECT 1 FROM tool_explanation_demands WHERE cache_key=? AND demand_id=? AND expires_at>?',(key,owner,now)).fetchone()
                    if not exists and db.execute('SELECT count(*) FROM tool_explanation_demands WHERE cache_key=? AND expires_at>?',(key,now)).fetchone()[0]>=256:
                        value={'status':'busy','reason':'too_many_views'}
                    else:
                        added+=0 if job else 1
                        writes.append((key,level,activity,owner,now+5000 if demand else now+cache.TTL_MS))
                        value={'status':'pending'}
        responses.append({'id':identity,**value})
    if not releases and not writes:
        return responses
    with transaction() as db:
        if releases:
            db.executemany('INSERT INTO tool_explanation_releases VALUES(?,?) ON CONFLICT(demand_id) DO UPDATE SET expires_at=excluded.expires_at',[(d,now+120000) for d in releases])
            keys={key for d in releases for (key,) in db.execute('DELETE FROM tool_explanation_demands WHERE demand_id=? RETURNING cache_key',(d,)).fetchall()}
            # A job nobody still views stops here, not at the next claim().
            db.executemany("DELETE FROM tool_explanation_jobs WHERE cache_key=? AND status='queued' AND NOT EXISTS (SELECT 1 FROM tool_explanation_demands d WHERE d.cache_key=tool_explanation_jobs.cache_key)",[(k,) for k in keys])
            db.execute('DELETE FROM tool_explanation_releases WHERE demand_id IN (SELECT demand_id FROM tool_explanation_releases ORDER BY expires_at DESC LIMIT -1 OFFSET 4096)')
        if writes and guard is not None and not guard(db):
            # The configuration changed since the reads: no new demand.
            return [{'id':entry['id'],'status':'disabled'} if entry['status']=='pending' else entry for entry in responses]
        # Every pending key gets its job: one read as present may have been
        # pruned by claim() since, and an expired failure is replaced.
        db.executemany("DELETE FROM tool_explanation_jobs WHERE cache_key=? AND status='failed' AND available_at<=?",[(p[0],now) for p in writes])
        db.executemany("INSERT INTO tool_explanation_jobs(cache_key,detail_level,activity_json,status,created_at,available_at) VALUES(?,?,?,'queued',?,?) ON CONFLICT(cache_key) DO NOTHING",
                       [(p[0],p[1],json.dumps(p[2]),now,now+int(debounce*1000)) for p in writes])
        db.executemany('INSERT INTO tool_explanation_demands VALUES(?,?,?) ON CONFLICT(cache_key,demand_id) DO UPDATE SET expires_at=excluded.expires_at',
                       [(p[0],p[3],p[4]) for p in writes])
    return responses


def claim(owner, *, enabled=True):
    now=cache.now_ms()
    db=conn()
    if db.execute("SELECT 1 FROM tool_explanation_jobs WHERE status='running' AND lease_until>? LIMIT 1",(now,)).fetchone():
        return []
    # Idle workers must not take a write transaction four times a second.
    actionable=enabled and db.execute("SELECT 1 FROM tool_explanation_jobs WHERE (status='queued' AND available_at<=?) OR (status='running' AND lease_until<=?) OR (status='failed' AND available_at<=?) LIMIT 1",(now,now,now)).fetchone()
    expired=db.execute('SELECT 1 FROM tool_explanation_cache WHERE expires_at<=? LIMIT 1',(now,)).fetchone()
    expired_release=db.execute('SELECT 1 FROM tool_explanation_releases WHERE expires_at<=? LIMIT 1',(now,)).fetchone()
    expired_demand=db.execute('SELECT 1 FROM tool_explanation_demands WHERE expires_at<=? LIMIT 1',(now,)).fetchone()
    if not actionable and not expired and not expired_release and not expired_demand:
        return []
    with transaction() as db:
        _prune(db,now)
        # Pause controls inference admission, not the retention deadline for
        # private queued metadata, viewport leases and cached explanations.
        if not enabled:
            return []
        # A single translator across all HTTP worker instances, not one per process.
        if db.execute("SELECT 1 FROM tool_explanation_jobs WHERE status='running' LIMIT 1").fetchone():
            return []
        first=db.execute("SELECT detail_level FROM tool_explanation_jobs WHERE status='queued' AND available_at<=? ORDER BY created_at,cache_key LIMIT 1",(now,)).fetchone()
        if not first: return []
        candidates=db.execute("SELECT cache_key,detail_level,activity_json,created_at FROM tool_explanation_jobs WHERE status='queued' AND detail_level=? AND available_at<=? ORDER BY created_at,cache_key LIMIT 64",(first[0],now)).fetchall()
        decoded=[(r[0],r[1],json.loads(r[2]),r[3]) for r in candidates]
        # A batch must use a single frozen Janitor configuration. Older queue
        # rows remain claimable so their caller can discard them safely.
        identity=decoded[0][2].get('janitor')
        rows=[r for r in decoded if r[2].get('janitor')==identity][:8]
        for row in rows:
            db.execute("UPDATE tool_explanation_jobs SET status='running',owner=?,lease_until=? WHERE cache_key=?",(owner,now+60000,row[0]))
        return rows


def complete(owner, values, failure_ttl, *, guard=None):
    """Publish only while both the queue lease and optional owner guard hold.

    The guard receives this transaction's connection so a Janitor run receipt
    and cache publication commit atomically. Rejected obsolete jobs are removed;
    a new configuration's demand has a different cache key.
    """
    now=cache.now_ms()
    with transaction() as db:
        owned=[]
        for key,value in values:
            job=db.execute("SELECT 1 FROM tool_explanation_jobs WHERE cache_key=? AND owner=? AND status='running' AND lease_until>?",(key,owner,now)).fetchone()
            if job:
                owned.append((key,value))
        if not owned:
            return False
        if guard is not None and not guard(db):
            for key,_ in owned:
                db.execute('DELETE FROM tool_explanation_jobs WHERE cache_key=?',(key,))
            return False
        for key,value in owned:
            if value['status']=='ready':
                db.execute('INSERT INTO tool_explanation_cache(cache_key,explanation,created_at,expires_at,source,provenance_json,signature) VALUES(?,?,?,?,?,?,?) '
                           'ON CONFLICT(cache_key) DO UPDATE SET explanation=excluded.explanation,created_at=excluded.created_at,expires_at=excluded.expires_at,'
                           'source=excluded.source,provenance_json=excluded.provenance_json,signature=excluded.signature',
                           (key,value['text'],now,now+cache.TTL_MS,value.get('source','llm'),json.dumps(value.get('provenance',{})),value.get('signature','')))
                db.execute('DELETE FROM tool_explanation_jobs WHERE cache_key=?',(key,))
            else:
                db.execute("UPDATE tool_explanation_jobs SET status='failed',activity_json='',owner='',lease_until=0,failure_reason=?,available_at=? WHERE cache_key=?",(value['reason'],now+int(failure_ttl*1000),key))
                db.execute('DELETE FROM tool_explanation_demands WHERE cache_key=?',(key,))
        return True


def abandon(owner, *, delay=0):
    with transaction() as db:
        now=cache.now_ms()
        db.execute("UPDATE tool_explanation_jobs SET status='queued',owner='',lease_until=0,available_at=MAX(available_at,?) WHERE owner=? AND status='running'",(now+int(delay*1000),owner))
        _prune(db,now)
