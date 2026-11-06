"""Transaction and fill-publication guards for snapshot message caches."""
import sqlite3
import pytest
from lib import db, message_previews as previews
from lib import log as log_module
from test_snapshot_n2 import seed


@pytest.mark.parametrize('warm', [False, True])
def test_transaction_projection_never_populates_caches(warm):
    aid = seed(1)[0]
    if warm:
        previews.dashboard_messages()
    old_map = previews._REVISION_CACHE.peek('all')
    old_preview = previews._PREVIEW_CACHE.peek(aid)
    c = db.conn()
    c.execute('BEGIN')
    try:
        c.execute("UPDATE messages SET text='uncommitted',revision=2,updated_at=2000 WHERE agent_id=?", (aid,))
        assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: uncommitted'
        assert c.in_transaction
        assert previews._REVISION_CACHE.peek('all') == old_map
        assert previews._PREVIEW_CACHE.peek(aid) == old_preview
    finally:
        c.execute('ROLLBACK')
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: synthetic'


@pytest.mark.parametrize('phase', ['map', 'preview'])
def test_foreign_commit_during_fill_prevents_cache_publication(monkeypatch, phase):
    aid = seed(1)[0]
    name = '_dashboard_revisions' if phase == 'map' else '_agent_preview'
    original = getattr(previews,name)
    fired = []
    def compute(*args):
        value = original(*args)
        if not fired:
            fired.append(True)
            foreign = sqlite3.connect(str(db.DB_PATH))
            try:
                foreign.execute("UPDATE messages SET text='after',revision=2,updated_at=2000 WHERE agent_id=?", (aid,))
                foreign.commit()
            finally:
                foreign.close()
        return value
    monkeypatch.setattr(previews,name,compute)
    previews.dashboard_messages()
    assert fired
    assert previews._REVISION_CACHE.peek('all') is None
    assert previews._PREVIEW_CACHE.peek(aid) is None
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: after'


@pytest.mark.parametrize('phase', ['map-read','preview-read','map-write','preview-write','prune','stamp-negative','stamp-error','after-stamp-negative','after-stamp-error'])
def test_cache_failure_logs_and_returns_fresh_projection(monkeypatch, phase):
    aid = seed(1)[0]
    events = []
    monkeypatch.setattr(log_module,'log_exception',lambda event,exc,**kw:events.append(event))
    monkeypatch.setattr(log_module,'log',lambda event,*args,**kw:events.append(event))
    def fail(*args,**kwargs):
        raise RuntimeError('synthetic cache failure')
    if phase.startswith('map-'):
        monkeypatch.setattr(previews._REVISION_CACHE,'get' if phase.endswith('read') else 'put',fail)
        expected = 'dashboardCacheReadFail' if phase.endswith('read') else 'dashboardCacheWriteFail'
    elif phase.startswith('preview-'):
        monkeypatch.setattr(previews._PREVIEW_CACHE,'get' if phase.endswith('read') else 'put',fail)
        expected = 'dashboardCacheReadFail' if phase.endswith('read') else 'dashboardCacheWriteFail'
    elif phase == 'prune':
        monkeypatch.setattr(previews._PREVIEW_CACHE,'retain',fail)
        expected = 'dashboardCachePruneFail'
    else:
        original_stamp = previews.change_stamp
        sampled = []
        def stamp():
            sampled.append(True)
            if phase.startswith('after-') and len(sampled) == 1:
                return original_stamp()
            return (-1,0) if phase.endswith('negative') else fail()
        monkeypatch.setattr(previews,'change_stamp',stamp)
        expected = 'dashboardCacheStampFail'
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: synthetic'
    assert expected in events
    if 'stamp' in phase:
        assert previews._REVISION_CACHE.peek('all') is None
        assert previews._PREVIEW_CACHE.peek(aid) is None


def test_projection_sql_error_is_not_swallowed_as_cache_failure(monkeypatch):
    seed(1)
    def fail():
        raise sqlite3.OperationalError('synthetic query failure')
    monkeypatch.setattr(previews,'_dashboard_revisions',fail)
    with pytest.raises(sqlite3.OperationalError):
        previews.dashboard_messages()


@pytest.mark.xfail(strict=True, raises=AssertionError,
    reason="Lead780: pre-existing borrowed implicit read cursors unsupported; backlog33")
def test_borrowed_read_cursor_is_not_certified_by_a_newer_global_stamp():
    aid, other = seed(2)
    c = db.conn()
    cursor = c.execute('SELECT message_id FROM messages ORDER BY message_id')
    assert cursor.fetchone() is not None
    assert not c.in_transaction  # SQLite implicit read snapshot, no explicit BEGIN
    foreign = sqlite3.connect(str(db.DB_PATH))
    try:
        foreign.execute("UPDATE messages SET text='new committed',revision=2,updated_at=2000 WHERE agent_id=?", (aid,))
        foreign.commit()
        # The caller legitimately sees its earlier read snapshot here.
        assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: synthetic'
    finally:
        foreign.close()
        cursor.fetchall()
        cursor.close()
    # Once the borrowed snapshot ends, the shared cache must not retain that
    # old view under the stamp sampled after the foreign commit.
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: new committed'


@pytest.mark.parametrize('warm',[False,True])
@pytest.mark.parametrize('commit',[False,True])
def test_owned_transaction_roster_changes_never_touch_cache_apis(monkeypatch,warm,commit):
    aid, other = seed(2)
    if warm:
        previews.dashboard_messages()
    touched = []
    with monkeypatch.context() as m:
        for cache in (previews._REVISION_CACHE,previews._PREVIEW_CACHE):
            for name in ('get','put','retain'):
                original = getattr(cache,name)
                def track(*args,_name=name,_real=original,**kwargs):
                    touched.append(_name)
                    return _real(*args,**kwargs)
                m.setattr(cache,name,track)
        c = db.conn()
        c.execute('BEGIN')
        try:
            c.execute("UPDATE messages SET text='transaction',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
            c.execute("UPDATE agents SET deleted_at=1 WHERE agent_id=?",(other,))
            c.execute("INSERT INTO agents(agent_id,persona,voice_id,cwd,session,backend,created_at) VALUES('transient','Transient','V','/fixture','transient','codex',1000)")
            result = previews.dashboard_messages()
            assert result[aid]['head']['preview'] == 'You: transaction'
            assert 'transient' in result and other not in result
            assert c.in_transaction and touched == []
            if commit:
                c.execute('COMMIT')
        finally:
            if c.in_transaction:
                c.execute('ROLLBACK')
    after = previews.dashboard_messages()
    assert after[aid]['head']['preview'] == ('You: transaction' if commit else 'You: synthetic')
    assert ('transient' in after) == commit
    assert (other in after) != commit


def test_cached_map_and_evicted_preview_race_keeps_original_certificate(monkeypatch):
    aid = seed(1)[0]
    previews.dashboard_messages()
    previews._PREVIEW_CACHE.pop(aid)  # fixture models a keyed eviction, not a global reset
    old_map = previews._REVISION_CACHE.peek('all')
    original = previews._agent_preview
    fired = []
    def compute(*args):
        value = original(*args)
        if not fired:
            fired.append(True)
            foreign = sqlite3.connect(str(db.DB_PATH))
            foreign.execute("UPDATE messages SET text='after hit',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
            foreign.commit()
            foreign.close()
        return value
    monkeypatch.setattr(previews,'_agent_preview',compute)
    previews.dashboard_messages()
    assert previews._PREVIEW_CACHE.peek(aid) is None
    assert previews._REVISION_CACHE.peek('all') == old_map
    result = previews.dashboard_messages()[aid]
    assert result['head']['preview'] == 'You: after hit'
    assert result['revisions'][aid] == 2


def test_slow_old_batch_never_prunes_or_overwrites_new_publisher(monkeypatch):
    aid = seed(1)[0]
    original = previews._dashboard_revisions
    peer = []
    def compute():
        value = original()
        if not peer:
            peer.append(None)
            foreign = sqlite3.connect(str(db.DB_PATH))
            foreign.execute("UPDATE messages SET text='new publisher',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
            foreign.execute("INSERT INTO agents(agent_id,persona,voice_id,cwd,session,backend,created_at) VALUES('new-peer','Peer','V','/fixture','new-peer','codex',1000)")
            foreign.commit()
            foreign.close()
            previews.dashboard_messages()
            peer[0] = (previews._REVISION_CACHE.peek('all'),previews._PREVIEW_CACHE.peek(aid))
            assert previews._PREVIEW_CACHE.peek('new-peer') is not None
        return value
    monkeypatch.setattr(previews,'_dashboard_revisions',compute)
    previews.dashboard_messages()
    assert previews._REVISION_CACHE.peek('all') == peer[0][0]
    assert previews._PREVIEW_CACHE.peek(aid) == peer[0][1]
    assert previews._PREVIEW_CACHE.peek('new-peer') is not None
    assert previews.dashboard_messages()[aid]['revisions'][aid] == 2


def test_multiagent_fill_is_not_published_before_batch_validation(monkeypatch):
    aid, other = seed(2)
    original = previews._agent_preview
    def compute(agent,*args):
        if agent == other:
            assert previews._PREVIEW_CACHE.peek(aid) is None
            foreign = sqlite3.connect(str(db.DB_PATH))
            foreign.execute("UPDATE messages SET text='between agents',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
            foreign.commit()
            foreign.close()
        return original(agent,*args)
    monkeypatch.setattr(previews,'_agent_preview',compute)
    previews.dashboard_messages()
    assert previews._REVISION_CACHE.peek('all') is None
    assert all(previews._PREVIEW_CACHE.peek(a) is None for a in (aid,other))


@pytest.mark.parametrize('which',['map','preview'])
def test_cache_publication_failure_after_landing_is_not_retried(monkeypatch,which):
    aid = seed(1)[0]
    cache = previews._REVISION_CACHE if which == 'map' else previews._PREVIEW_CACHE
    original = cache.put
    calls = []
    def landed(*args,**kwargs):
        calls.append(True)
        original(*args,**kwargs)
        raise RuntimeError('synthetic publication receipt failure')
    monkeypatch.setattr(cache,'put',landed)
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: synthetic'
    assert calls == [True]
    assert cache.peek('all' if which == 'map' else aid) is not None
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: synthetic'
    assert calls == [True]


def test_returned_preview_mutation_cannot_poison_cached_heads_or_revisions():
    aid = seed(1)[0]
    for _ in range(2):  # first a fill, then a cache hit
        value = previews.dashboard_messages()[aid]
        value['head']['preview'] = 'caller mutation'
        value['completed_head']['preview'] = 'caller mutation'
        value['revisions'][aid] = 999
        after = previews.dashboard_messages()[aid]
        assert after['head']['preview'] == 'You: synthetic'
        assert after['completed_head']['preview'] == 'You: synthetic'
        assert after['revisions'][aid] == 1


@pytest.mark.parametrize('count',[1,1059])
@pytest.mark.parametrize('length',[80,19])
def test_stamp_work_is_constant_per_batch(monkeypatch,count,length):
    seed(count)
    original = previews.change_stamp
    samples = []
    def stamp():
        samples.append(True)
        return original()
    monkeypatch.setattr(previews,'change_stamp',stamp)
    previews.dashboard_messages(max_len=length)
    assert len(samples) == 2
    samples.clear()
    previews.dashboard_messages(max_len=length)
    assert len(samples) == 2


@pytest.mark.parametrize('method',['get','put'])
def test_failed_cache_is_disabled_for_batch_and_logs_only_code(monkeypatch,method):
    ids = seed(2)
    secret = 'SYNTHETIC_CONTENT_MUST_NOT_BE_LOGGED'
    events = []
    monkeypatch.setattr(log_module,'log',lambda event,detail='',**kw:events.append((event,detail)))
    monkeypatch.setattr(log_module,'log_exception',lambda event,exc,**kw:events.append((event,str(exc))))
    def fail(*args,**kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(previews._PREVIEW_CACHE,method,fail)
    result = previews.dashboard_messages()
    assert all(result[aid]['head']['preview'] == 'You: synthetic' for aid in ids)
    expected = 'dashboardCacheReadFail' if method == 'get' else 'dashboardCacheWriteFail'
    assert sum(event == expected for event,_ in events) == 1
    assert all(secret not in detail for _,detail in events)


@pytest.mark.parametrize('error',[False,True])
def test_unknown_after_stamp_never_prunes_a_cached_roster(monkeypatch,error):
    seed(1)
    original = previews.change_stamp
    sampled,pruned = [],[]
    def stamp():
        sampled.append(True)
        if len(sampled) == 1:
            return original()
        if error:
            raise RuntimeError('synthetic after-stamp failure')
        return -1,0
    monkeypatch.setattr(previews,'change_stamp',stamp)
    monkeypatch.setattr(previews._PREVIEW_CACHE,'retain',lambda keys:pruned.append(True))
    previews.dashboard_messages()
    assert pruned == []


def test_nondefault_length_never_replaces_default_preview():
    aid = seed(1)[0]
    db.conn().execute("UPDATE messages SET text=?,revision=2,updated_at=2000 WHERE agent_id=?",('synthetic '*30,aid))
    full = previews.dashboard_messages()[aid]
    short = previews.dashboard_messages(max_len=19)[aid]
    assert short['head']['preview'] != full['head']['preview']
    assert previews.dashboard_messages()[aid] == full


def test_old_publisher_retains_start_tag_if_writer_commits_after_validation(monkeypatch):
    aid = seed(1)[0]
    original = previews._cache_write
    peer = []
    def publish(cache,key,revision,value,failed):
        if cache is previews._REVISION_CACHE and not peer:
            peer.append(True)
            foreign = sqlite3.connect(str(db.DB_PATH))
            try:
                foreign.execute("UPDATE messages SET text='after validation',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
                foreign.commit()
            finally:
                foreign.close()
            previews.dashboard_messages()  # a newer successful publisher
        return original(cache,key,revision,value,failed)
    monkeypatch.setattr(previews,'_cache_write',publish)
    previews.dashboard_messages()
    assert peer
    stored = previews._REVISION_CACHE.peek('all')
    # An old correctly tagged overwrite is a safe redundant miss. It must not
    # be relabelled with the newer stamp and selected as a current map.
    if stored[0] == db.change_stamp():
        assert stored[1][0][aid][aid] == 2
    after = previews.dashboard_messages()[aid]
    assert after['head']['preview'] == 'You: after validation'
    assert after['revisions'][aid] == 2
