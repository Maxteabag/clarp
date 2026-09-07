from lib import tool_explanation_cache as cache
from lib.db import conn


def test_fixed_24h_expiry_not_extended_by_reads(monkeypatch):
    clock=[1000]
    monkeypatch.setattr(cache,'now_ms',lambda:clock[0])
    expiry=cache.put('hash','Read the project files.')
    assert expiry==1000+86400000
    clock[0]=expiry-1
    assert cache.get('hash')==('Read the project files.',expiry)
    clock[0]=expiry
    assert cache.get('hash') is None
    cache.prune()
    assert conn().execute('SELECT count(*) FROM tool_explanation_cache').fetchone()[0]==0


def test_only_hash_text_and_timestamps_are_persisted():
    assert [r[1] for r in conn().execute('PRAGMA table_info(tool_explanation_cache)')]==['cache_key','explanation','created_at','expires_at']
