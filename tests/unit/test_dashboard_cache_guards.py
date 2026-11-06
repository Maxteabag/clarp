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


@pytest.mark.parametrize('phase', ['map-read','preview-read','map-write','preview-write','prune','stamp-negative','stamp-error'])
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
        monkeypatch.setattr(previews,'change_stamp',(lambda:(-1,0)) if phase.endswith('negative') else fail)
        expected = 'dashboardCacheStampFail'
    assert previews.dashboard_messages()[aid]['head']['preview'] == 'You: synthetic'
    assert expected in events
    if phase.startswith('stamp-'):
        assert previews._REVISION_CACHE.peek('all') is None
        assert previews._PREVIEW_CACHE.peek(aid) is None


def test_projection_sql_error_is_not_swallowed_as_cache_failure(monkeypatch):
    seed(1)
    def fail():
        raise sqlite3.OperationalError('synthetic query failure')
    monkeypatch.setattr(previews,'_dashboard_revisions',fail)
    with pytest.raises(sqlite3.OperationalError):
        previews.dashboard_messages()


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
