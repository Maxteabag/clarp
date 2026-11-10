"""Pin the real callers' no-borrowed-read-cursor contract (lead780)."""
import sqlite3
import pytest
from lib import agents, agent_conversations, cache_warmup, db, message_previews
from lib import message_store, snapshot
from test_snapshot_n2 import seed


class BorrowedRead(BaseException):
    """Test failure that the warm-up's best-effort Exception catch cannot mask."""


@pytest.mark.parametrize('caller',['snapshot','warmup'])
@pytest.mark.parametrize('inject_borrowed_read',[False,True])
def test_real_caller_finishes_reads_before_dashboard_projection(monkeypatch,caller,inject_borrowed_read):
    aid, other = seed(2)
    c = db.conn()
    pending, read_count = set(), []
    original_execute = type(c).execute
    class Cursor:
        def __init__(self,raw):
            self.raw = raw
            self.token = object()
            pending.add(self.token)
            read_count.append(True)
        def __getattr__(self,name):
            return getattr(self.raw,name)
        def __del__(self):
            pending.discard(self.token)
        def fetchone(self):
            row = self.raw.fetchone()
            if row is None:
                pending.discard(self.token)
            return row
        def fetchall(self):
            rows = self.raw.fetchall()
            pending.discard(self.token)
            return rows
        def __iter__(self):
            return self
        def __next__(self):
            try:
                return next(self.raw)
            except StopIteration:
                pending.discard(self.token)
                raise
        def close(self):
            self.raw.close()
            pending.discard(self.token)
    def execute(connection,*args,**kwargs):
        raw = original_execute(connection,*args,**kwargs)
        return Cursor(raw) if getattr(raw,'description',None) is not None else raw
    monkeypatch.setattr(type(c),'execute',execute)
    held = []
    if inject_borrowed_read:
        if caller == 'snapshot':
            owner,name = agents,'get_focus'
        else:
            owner,name = agent_conversations,'list_conversations'
        original = getattr(owner,name)
        def hold(*args,**kwargs):
            result = original(*args,**kwargs)
            cursor = c.execute('SELECT message_id FROM messages ORDER BY message_id')
            cursor.fetchone()  # another row remains, so the SQLite view is held
            held.append(cursor)
            return result
        monkeypatch.setattr(owner,name,hold)
    actual = message_previews.dashboard_messages
    checked = []
    def projection(*args,**kwargs):
        assert read_count  # real preceding database reads were observed
        if pending:
            raise BorrowedRead('caller still owns an unread cursor')
        foreign = sqlite3.connect(str(db.DB_PATH))
        try:
            foreign.execute("UPDATE messages SET text='caller boundary',revision=2,updated_at=2000 WHERE agent_id=?",(aid,))
            foreign.commit()
        finally:
            foreign.close()
        value = actual(*args,**kwargs)
        assert value[aid]['head']['preview'] == 'You: caller boundary'
        checked.append(True)
        return value
    monkeypatch.setattr(message_previews,'dashboard_messages',projection)
    monkeypatch.setattr(message_store,'dashboard_messages',projection)
    def invoke():
        if caller == 'snapshot':
            snapshot.build_agent_snapshot(None)
        else:
            cache_warmup.CacheWarmupWorker(delay_sec=0)._run()  # synchronous real orchestration
    try:
        if inject_borrowed_read:
            with pytest.raises(BorrowedRead):
                invoke()
            assert checked == []
        else:
            invoke()
            assert checked == [True]
    finally:
        for cursor in held:
            cursor.close()
