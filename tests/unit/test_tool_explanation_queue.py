from lib import tool_explanation_queue as queue, tool_explanation_cache as cache
from lib.db import conn


def prepared(owner=None):
    return [('row','digest',{'command':'read a private script','scripts':[{'source_excerpt':'bounded source'}]},owner)]


def test_atomic_claim_completion_and_payload_removal(monkeypatch):
    monkeypatch.setattr(cache,'now_ms',lambda:1000)
    assert queue.request(3,prepared(),[],0)[0]['status']=='pending'
    assert len(queue.claim('first-worker'))==1
    assert queue.claim('second-worker')==[]
    queue.complete('wrong-worker',[('digest',{'status':'ready','text':'Wrong.'})],60)
    assert cache.get('digest') is None
    queue.complete('first-worker',[('digest',{'status':'ready','text':'Read the project notes.'})],60)
    assert queue.request(3,prepared(),[],0)[0]['text']=='Read the project notes.'
    assert conn().execute('SELECT count(*) FROM tool_explanation_jobs').fetchone()[0]==0
    assert conn().execute('SELECT count(*) FROM tool_explanation_demands').fetchone()[0]==0
    assert cache.get('digest')[1]==1000+86400000


def test_dead_worker_recovery_and_stale_completion_fence(monkeypatch):
    now=[1000]
    monkeypatch.setattr(cache,'now_ms',lambda:now[0])
    queue.request(3,prepared(),[],0)
    queue.claim('dead')
    now[0]+=60001
    assert len(queue.claim('replacement'))==1
    queue.complete('dead',[('digest',{'status':'ready','text':'Stale.'})],60)
    assert cache.get('digest') is None
    queue.complete('replacement',[('digest',{'status':'ready','text':'Fresh.'})],60)
    assert cache.get('digest')[0]=='Fresh.'


def test_release_and_ready_cache_survive_connection_reopen():
    from lib.db import close_local
    queue.request(3,prepared('view'),[],0)
    close_local()
    queue.request(3,[],['view'],0)
    close_local()
    assert queue.request(3,prepared('view'),[],0)[0]['status']=='cancelled'
    assert queue.claim('worker')==[]


def test_queued_job_survives_reopen_and_failure_discards_payload(monkeypatch):
    from lib.db import close_local
    now=[1000]
    monkeypatch.setattr(cache,'now_ms',lambda:now[0])
    queue.request(3,prepared(),[],0)
    close_local()
    assert queue.claim('worker')[0][0]=='digest'
    queue.complete('worker',[('digest',{'status':'failed','reason':'timeout'})],60)
    row=conn().execute('SELECT activity_json,status FROM tool_explanation_jobs').fetchone()
    assert tuple(row)==('','failed')
    assert cache.get('digest') is None
    assert queue.request(3,prepared(),[],0)[0]['reason']=='timeout'
    now[0]+=60001
    assert queue.request(3,prepared(),[],0)[0]['status']=='pending'


def test_ready_answer_survives_worker_restart_without_inference():
    from lib.tool_explanations import ToolExplanations
    import time
    activity=[{'id':'1','activity':{'command':'ls'}}]
    with ToolExplanations(translate=lambda level,items:{'1':'List the files.'},debounce=0) as first:
        for _ in range(100):
            result=first.request(3,activity)['items'][0]
            if result['status']=='ready': break
            time.sleep(.01)
        assert result['status']=='ready'
    def forbidden(*_): raise AssertionError('cached answer must not run a model')
    with ToolExplanations(translate=forbidden,debounce=0) as second:
        assert second.request(3,activity)['items'][0]['text']=='List the files.'


def test_v71_migration_preserves_existing_rows():
    import sqlite3
    from lib import db
    connection=sqlite3.connect(':memory:',isolation_level=None)
    db._migrate(connection)
    connection.execute("INSERT INTO settings(key,value,updated_at) VALUES('cache-migration-sentinel','preserved',0)")
    for table in ['tool_explanation_demands','tool_explanation_jobs','tool_explanation_releases','tool_explanation_cache']:
        connection.execute('DROP TABLE '+table)
    connection.execute('PRAGMA user_version=71')
    db._migrate(connection)
    assert connection.execute('PRAGMA user_version').fetchone()[0]==72
    assert connection.execute("SELECT value FROM settings WHERE key='cache-migration-sentinel'").fetchone()[0]=='preserved'
    assert connection.execute('SELECT count(*) FROM tool_explanation_cache').fetchone()[0]==0
    connection.close()
