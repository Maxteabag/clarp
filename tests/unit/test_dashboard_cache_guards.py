"""Transaction and fill-publication guards for snapshot message caches.

What the guard is for: a preview is never stored that is older than the rows
it describes, and nothing uncommitted reaches a shared cache.

The rule for a batch that something committed into changed on 10 Nov 2026.
Before, any commit by any connection discarded every preview the batch had
computed; on a Host that is always being written a cold cache then never
filled, and each agent list after a restart ran three statements per agent
for seconds. Now each agent's preview is stored when that agent's own key
(conversation, highest revision, highest updated_at) is the same after the
batch as before, and left out when it moved. The tests below that write to
exactly the agent during the fill still expect nothing stored for it; the
ones that write elsewhere now expect the others' previews to be kept.
"""
import json
import sqlite3
import threading
import pytest
from lib import db, message_previews as previews
from lib import log as log_module
from test_snapshot_n2 import seed, watch


def _foreign(sql, params=()):
    """One committed write from another connection, as a hook or the runtime makes."""
    foreign = sqlite3.connect(str(db.DB_PATH))
    try:
        foreign.execute(sql, params)
        foreign.commit()
    finally:
        foreign.close()


def _once_during_fill(monkeypatch, action, *, before=None, after=None):
    """Run ``action`` once inside a batch: just before the preview of agent
    ``before`` is computed, or just after the preview of agent ``after`` was."""
    original, fired = previews._agent_preview, []
    def compute(agent_id, *args):
        if before == agent_id and not fired:
            fired.append(True)
            action()
        value = original(agent_id, *args)
        if after == agent_id and not fired:
            fired.append(True)
            action()
        return value
    monkeypatch.setattr(previews, '_agent_preview', compute)
    return fired


def _fresh(agent_id):
    """The preview computed now, straight from the rows."""
    from lib import origins
    return previews._agent_preview(agent_id, 80, tuple(sorted(origins.ROUTINE_AUTOMATION_ORIGINS)))


def _stored(agent_id):
    entry = previews._PREVIEW_CACHE.peek(agent_id)
    return None if entry is None else entry[1]


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


@pytest.mark.parametrize('phase', ['map', 'preview', 'mid-preview'])
def test_a_write_to_the_agent_during_the_fill_keeps_its_preview_out_of_the_cache(monkeypatch, phase):
    """The guard itself. 'map': the row changes after the keys were read and
    before the preview is computed. 'preview': after the preview was computed,
    so the preview is older than the row. 'mid-preview': between the preview's
    own statements, so it is a mixture of both."""
    aid = seed(1)[0]
    name = {'map': '_dashboard_revisions', 'preview': '_agent_preview', 'mid-preview': '_format_preview'}[phase]
    original = getattr(previews,name)
    fired = []
    def compute(*args):
        value = original(*args)
        if not fired:
            fired.append(True)
            _foreign("UPDATE messages SET text='after',revision=2,updated_at=2000 WHERE agent_id=?", (aid,))
        return value
    monkeypatch.setattr(previews,name,compute)
    previews.dashboard_messages()
    assert fired
    assert previews._REVISION_CACHE.peek('all') is None
    assert previews._PREVIEW_CACHE.peek(aid) is None
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: after'
    assert _stored(aid) == _fresh(aid)                     # and now the true one is stored


@pytest.mark.parametrize('change', ['in-place rewrite', 'newest row deleted', 'first message', 'agent deleted'])
def test_every_kind_of_change_to_the_agent_moves_its_key(monkeypatch, change):
    aid, other = seed(2)
    if change == 'first message':
        db.conn().execute("DELETE FROM messages WHERE agent_id=?", (aid,))
    action = {
        # Same revision, new text: only updated_at tells.
        'in-place rewrite': lambda: _foreign("UPDATE messages SET text='rewritten',updated_at=2000 WHERE agent_id=?", (aid,)),
        'newest row deleted': lambda: _foreign("DELETE FROM messages WHERE agent_id=?", (aid,)),
        'first message': lambda: _foreign("""INSERT INTO messages
            (message_id,agent_id,backend_session_id,seq,role,text,revision,updated_at)
            VALUES ('first',?,?,1,'user','first',1,1000)""", (aid, aid)),
        'agent deleted': lambda: _foreign("UPDATE agents SET deleted_at=1 WHERE agent_id=?", (aid,)),
    }[change]
    fired = _once_during_fill(monkeypatch, action, after=aid)
    previews.dashboard_messages()
    assert fired
    assert previews._PREVIEW_CACHE.peek(aid) is None       # it changed: not stored
    assert _stored(other) == _fresh(other)                 # the agent beside it did not: stored


# --- a write to something else ----------------------------------------------------------

@pytest.mark.parametrize('when', ['before-its-preview', 'after-its-preview'])
def test_a_write_to_another_agent_during_the_fill_leaves_this_agents_preview_stored(monkeypatch, when):
    aid, other = seed(2)
    write = lambda: _foreign("UPDATE messages SET text='other changed',revision=2,updated_at=2000 WHERE agent_id=?", (other,))
    fired = _once_during_fill(monkeypatch, write, **({'before': other} if when == 'before-its-preview' else {'after': other}))
    result = previews.dashboard_messages()
    assert fired
    assert result[aid]['head']['preview'] == 'You: synthetic'
    assert _stored(aid) == _fresh(aid)                     # untouched agent: kept
    assert previews._PREVIEW_CACHE.peek(other) is None     # the written agent: not stored, either way
    assert previews._REVISION_CACHE.peek('all') is None    # the map belongs to the old stamp

    calls = watch(monkeypatch)
    again = previews.dashboard_messages()
    assert calls == [other]                                # only the agent that changed is computed again
    assert again[other]['head']['preview'] == 'You: other changed'
    assert again[aid] == result[aid]


def test_an_unrelated_write_during_a_cold_fill_still_leaves_a_warm_cache(monkeypatch):
    """The Host's case after a restart: something is always being written."""
    ids = seed(40)
    fired = _once_during_fill(
        monkeypatch, lambda: _foreign("UPDATE agents SET custom_status='busy' WHERE agent_id=?", (ids[5],)),
        after=ids[20])
    cold = previews.dashboard_messages()
    assert fired
    assert all(_stored(agent_id) == _fresh(agent_id) for agent_id in ids)

    calls = watch(monkeypatch)
    assert previews.dashboard_messages() == cold
    assert calls == []                                     # warm: before, all forty were computed again


def test_the_certificate_is_one_grouped_statement_for_the_computed_agents_only(monkeypatch):
    ids = seed(30)
    previews.dashboard_messages()                          # warm
    _foreign("UPDATE messages SET text='changed',revision=2,updated_at=2000 WHERE agent_id IN (?,?)", ids[:2])
    asked, statements = [], []
    current = previews._current_keys
    monkeypatch.setattr(previews, '_current_keys', lambda agent_ids: asked.append(list(agent_ids)) or current(agent_ids))
    fired = _once_during_fill(
        monkeypatch, lambda: _foreign("UPDATE agents SET custom_status='busy' WHERE agent_id=?", (ids[9],)),
        after=ids[0])
    con = db.conn()
    con.set_trace_callback(statements.append)
    try:
        previews.dashboard_messages()
    finally:
        con.set_trace_callback(None)
    assert fired
    assert asked == [ids[:2]]                              # the two that were computed, not the roster
    certificates = [sql for sql in statements if 'json_each' in sql]
    assert len(certificates) == 1
    assert _stored(ids[0]) == _fresh(ids[0]) and _stored(ids[1]) == _fresh(ids[1])


def test_a_quiet_database_does_exactly_what_it_did(monkeypatch):
    """Holds for the old rule and the new one alike: with nothing written
    during the batch there is no certificate to read, and the statements are
    the map, the roster and three per agent, then none at all."""
    ids = seed(12)
    maps, statements = [], []
    revisions = previews._dashboard_revisions
    monkeypatch.setattr(previews, '_dashboard_revisions', lambda: maps.append(True) or revisions())
    calls = watch(monkeypatch)
    con = db.conn()
    con.set_trace_callback(statements.append)
    try:
        cold = previews.dashboard_messages()
        assert (len(maps), sorted(calls)) == (1, sorted(ids))
        assert len(statements) == 2 + 3 * len(ids)
        assert previews._REVISION_CACHE.peek('all') is not None
        assert all(_stored(agent_id) == _fresh(agent_id) for agent_id in ids)

        calls.clear()
        statements.clear()
        assert previews.dashboard_messages() == cold
        assert (len(maps), calls, statements) == (1, [], [])   # nothing read again, nothing certified
    finally:
        con.set_trace_callback(None)


def test_a_failed_certificate_loses_only_the_cache_fill(monkeypatch):
    aid, other = seed(2)
    events = []
    monkeypatch.setattr(log_module,'log',lambda event,detail='',**kw:events.append((event,detail)))
    def fail(agent_ids):
        raise sqlite3.OperationalError('SYNTHETIC_CONTENT_MUST_NOT_BE_LOGGED')
    monkeypatch.setattr(previews,'_current_keys',fail)
    fired = _once_during_fill(
        monkeypatch, lambda: _foreign("UPDATE agents SET custom_status='busy' WHERE agent_id=?", (other,)), after=aid)
    result = previews.dashboard_messages()
    assert fired
    assert result[aid]['head']['preview'] == result[other]['head']['preview'] == 'You: synthetic'
    assert previews._PREVIEW_CACHE.peek(aid) is None and previews._PREVIEW_CACHE.peek(other) is None
    assert events == [('dashboardCacheCertifyFail', 'error=OperationalError')]


def test_a_batch_that_was_written_into_never_prunes(monkeypatch):
    """Its roster may be out of date: an entry a newer batch stored stays."""
    aid, other = seed(2)
    previews.dashboard_messages()
    previews._PREVIEW_CACHE.pop(aid)
    pruned = []
    monkeypatch.setattr(previews._PREVIEW_CACHE,'retain',lambda keys:pruned.append(True))
    fired = _once_during_fill(
        monkeypatch, lambda: _foreign("UPDATE agents SET custom_status='busy' WHERE agent_id=?", (other,)), after=aid)
    previews.dashboard_messages()
    assert fired and pruned == []
    assert _stored(aid) == _fresh(aid) and _stored(other) == _fresh(other)


# --- two batches at once ------------------------------------------------------------------

def test_a_slow_batch_and_a_fast_one_leave_only_true_previews(monkeypatch):
    """Batch one has computed the agent's preview and stops. The agent's row
    changes and batch two runs to the end in another thread and stores the new
    preview. Batch one then finishes: it must not put its older one back."""
    aid, other = seed(2)
    original = previews._agent_preview
    paused, resume, second = threading.Event(), threading.Event(), {}
    def compute(agent_id, *args):
        value = original(agent_id, *args)
        if agent_id == aid and threading.current_thread() is threading.main_thread() and not paused.is_set():
            paused.set()
            assert resume.wait(30)
        return value
    monkeypatch.setattr(previews, '_agent_preview', compute)

    def fast():
        assert paused.wait(30)
        try:
            _foreign("UPDATE messages SET text='newer',revision=2,updated_at=2000 WHERE agent_id=?", (aid,))
            second['result'] = previews.dashboard_messages()
            second['stored'] = _stored(aid)
        finally:
            db.close_local()
            resume.set()
    thread = threading.Thread(target=fast)
    thread.start()
    first = previews.dashboard_messages()
    thread.join(30)
    assert not thread.is_alive()

    assert first[aid]['head']['preview'] == 'You: synthetic'      # what batch one saw
    assert second['result'][aid]['head']['preview'] == 'You: newer'
    assert second['stored']['head']['preview'] == 'You: newer'
    assert _stored(aid) == _fresh(aid)                            # still the newer one
    assert _stored(aid)['head']['preview'] == 'You: newer'
    assert _stored(other) == _fresh(other)


def test_concurrent_batches_under_writes_never_store_a_wrong_preview():
    ids = seed(24)
    stop, errors = threading.Event(), []

    def writer():
        connection = sqlite3.connect(str(db.DB_PATH), timeout=5)
        try:
            for n in range(120):
                agent_id = ids[n % len(ids)]
                connection.execute("UPDATE messages SET text=?,revision=revision+1,updated_at=updated_at+1 WHERE agent_id=?",
                                   (f'text {n}', agent_id))
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)
        finally:
            connection.close()
            stop.set()

    def builder():
        try:
            while not stop.is_set():
                previews.dashboard_messages()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close_local()

    threads = [threading.Thread(target=writer), threading.Thread(target=builder), threading.Thread(target=builder)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == [] and not any(thread.is_alive() for thread in threads)

    # Whatever the interleaving was: the database is quiet now, and a batch
    # that reads the cache must answer what a batch without any cache answers.
    served = previews.dashboard_messages()
    previews._PREVIEW_CACHE.reset()
    previews._REVISION_CACHE.reset()
    assert json.dumps(served, sort_keys=True) == json.dumps(previews.dashboard_messages(), sort_keys=True)
    assert served[ids[0]]['head']['preview'].startswith('You: text ')


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
    """Nothing is stored while the batch runs. Changed on purpose: the write
    below is to the first agent only, so afterwards the first agent's preview
    (computed before the write) is left out and the second agent's is stored;
    before, both were discarded."""
    aid, other = seed(2)
    original = previews._agent_preview
    def compute(agent,*args):
        if agent == other:
            assert previews._PREVIEW_CACHE.peek(aid) is None
            _foreign("UPDATE messages SET text='between agents',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
        return original(agent,*args)
    monkeypatch.setattr(previews,'_agent_preview',compute)
    previews.dashboard_messages()
    assert previews._REVISION_CACHE.peek('all') is None
    assert previews._PREVIEW_CACHE.peek(aid) is None
    assert _stored(other) == _fresh(other)


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
