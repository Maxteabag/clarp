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


def request(level, prepared, releases, debounce):
    now=cache.now_ms()
    responses=[]
    if level and prepared and not releases:
        # Ready hits need only reads; do not take the database writer lock on
        # every re-render or repeated poll of an already completed explanation.
        db=conn()
        for identity,key,activity,demand in prepared:
            if demand and db.execute('SELECT 1 FROM tool_explanation_releases WHERE demand_id=? AND expires_at>?',(demand,now)).fetchone():
                responses.append({'id':identity,'status':'cancelled'})
                continue
            ready=db.execute('SELECT explanation FROM tool_explanation_cache WHERE cache_key=? AND expires_at>?',(key,now)).fetchone()
            if not ready:
                break
            responses.append({'id':identity,'status':'ready','text':ready[0]})
        if len(responses)==len(prepared):
            return responses
        responses=[]
    with transaction() as db:
        for demand in releases:
            db.execute('INSERT INTO tool_explanation_releases VALUES(?,?) ON CONFLICT(demand_id) DO UPDATE SET expires_at=excluded.expires_at',(demand,now+120000))
            db.execute('DELETE FROM tool_explanation_demands WHERE demand_id=?',(demand,))
        _prune(db,now)
        db.execute('DELETE FROM tool_explanation_releases WHERE demand_id IN (SELECT demand_id FROM tool_explanation_releases ORDER BY expires_at DESC LIMIT -1 OFFSET 4096)')
        for identity,key,activity,demand in prepared:
            value=None
            if demand and db.execute('SELECT 1 FROM tool_explanation_releases WHERE demand_id=?',(demand,)).fetchone():
                value={'status':'cancelled'}
            elif not level:
                value={'status':'disabled'}
            else:
                ready=db.execute('SELECT explanation FROM tool_explanation_cache WHERE cache_key=?',(key,)).fetchone()
                job=db.execute('SELECT status,failure_reason FROM tool_explanation_jobs WHERE cache_key=?',(key,)).fetchone()
                if ready:
                    value={'status':'ready','text':ready[0]}
                elif job and job[0]=='failed':
                    value={'status':'failed','reason':job[1]}
                elif not job and db.execute("SELECT count(*) FROM tool_explanation_jobs WHERE status='queued'").fetchone()[0]>=64:
                    value={'status':'busy','reason':'queue_full'}
                else:
                    owner=demand or 'legacy'
                    exists=db.execute('SELECT 1 FROM tool_explanation_demands WHERE cache_key=? AND demand_id=?',(key,owner)).fetchone()
                    count=db.execute('SELECT count(*) FROM tool_explanation_demands WHERE cache_key=?',(key,)).fetchone()[0]
                    if not exists and count>=256:
                        value={'status':'busy','reason':'too_many_views'}
                    else:
                        if not job:
                            db.execute("INSERT INTO tool_explanation_jobs(cache_key,detail_level,activity_json,status,created_at,available_at) VALUES(?,?,?,'queued',?,?)",(key,level,json.dumps(activity),now,now+int(debounce*1000)))
                        db.execute('INSERT INTO tool_explanation_demands VALUES(?,?,?) ON CONFLICT(cache_key,demand_id) DO UPDATE SET expires_at=excluded.expires_at',(key,owner,now+5000 if demand else now+cache.TTL_MS))
                        value={'status':'pending'}
            responses.append({'id':identity,**value})
    return responses


def claim(owner):
    now=cache.now_ms()
    db=conn()
    if db.execute("SELECT 1 FROM tool_explanation_jobs WHERE status='running' AND lease_until>? LIMIT 1",(now,)).fetchone():
        return []
    # Idle workers must not take a write transaction four times a second.
    actionable=db.execute("SELECT 1 FROM tool_explanation_jobs WHERE (status='queued' AND available_at<=?) OR (status='running' AND lease_until<=?) OR (status='failed' AND available_at<=?) LIMIT 1",(now,now,now)).fetchone()
    expired=db.execute('SELECT 1 FROM tool_explanation_cache WHERE expires_at<=? LIMIT 1',(now,)).fetchone()
    expired_release=db.execute('SELECT 1 FROM tool_explanation_releases WHERE expires_at<=? LIMIT 1',(now,)).fetchone()
    if not actionable and not expired and not expired_release:
        return []
    with transaction() as db:
        _prune(db,now)
        # A single translator across all HTTP worker instances, not one per process.
        if db.execute("SELECT 1 FROM tool_explanation_jobs WHERE status='running' LIMIT 1").fetchone():
            return []
        first=db.execute("SELECT detail_level FROM tool_explanation_jobs WHERE status='queued' AND available_at<=? ORDER BY created_at,cache_key LIMIT 1",(now,)).fetchone()
        if not first: return []
        rows=db.execute("SELECT cache_key,detail_level,activity_json,created_at FROM tool_explanation_jobs WHERE status='queued' AND detail_level=? AND available_at<=? ORDER BY created_at,cache_key LIMIT 8",(first[0],now)).fetchall()
        for row in rows:
            db.execute("UPDATE tool_explanation_jobs SET status='running',owner=?,lease_until=? WHERE cache_key=?",(owner,now+60000,row[0]))
        return [(r[0],r[1],json.loads(r[2]),r[3]) for r in rows]


def complete(owner, values, failure_ttl):
    now=cache.now_ms()
    with transaction() as db:
        for key,value in values:
            job=db.execute("SELECT 1 FROM tool_explanation_jobs WHERE cache_key=? AND owner=? AND status='running' AND lease_until>?",(key,owner,now)).fetchone()
            if not job: continue
            if value['status']=='ready':
                db.execute('INSERT INTO tool_explanation_cache VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET explanation=excluded.explanation,created_at=excluded.created_at,expires_at=excluded.expires_at',(key,value['text'],now,now+cache.TTL_MS))
                db.execute('DELETE FROM tool_explanation_jobs WHERE cache_key=?',(key,))
            else:
                db.execute("UPDATE tool_explanation_jobs SET status='failed',activity_json='',owner='',lease_until=0,failure_reason=?,available_at=? WHERE cache_key=?",(value['reason'],now+int(failure_ttl*1000),key))
                db.execute('DELETE FROM tool_explanation_demands WHERE cache_key=?',(key,))


def abandon(owner):
    with transaction() as db:
        db.execute("UPDATE tool_explanation_jobs SET status='queued',owner='',lease_until=0 WHERE owner=? AND status='running'",(owner,))
        _prune(db,cache.now_ms())
