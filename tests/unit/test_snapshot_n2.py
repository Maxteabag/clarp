"""SNAP-N2 cache freshness, bounded roster scans and runtime-sample ownership."""
import sqlite3
import pytest
from lib import agents, backends, db, message_previews as previews, message_store


def seed(count):
    ids = [f"snap-n2-{i:04d}" for i in range(count)]
    db.conn().executemany("""INSERT INTO agents
        (agent_id,persona,voice_id,cwd,session,backend,created_at)
        VALUES (?,?, 'V','/fixture',?,'codex',1000)""", [(a,a,a) for a in ids])
    db.conn().executemany("""INSERT INTO messages
        (message_id,agent_id,backend_session_id,seq,role,text,revision,updated_at)
        VALUES (?,?,?,1,'user','synthetic',1,1000)""", [(a,a,a) for a in ids])
    return ids


def watch(monkeypatch):
    calls = []
    original = previews._agent_preview
    def compute(aid, *args):
        calls.append(aid)
        return original(aid, *args)
    monkeypatch.setattr(previews, "_agent_preview", compute)
    return calls


def test_one_import_recomputes_one_preview_above_old_roster_limit(monkeypatch):
    ids = seed(1059)  # actual roster count at diagnosis, versus the old 1024 LRU
    db.conn().execute("UPDATE agents SET archived_at=1000 WHERE agent_id=?", (ids[1],))
    assert len(previews.dashboard_messages()) == len(ids)
    calls = watch(monkeypatch)
    message_store.store_transcript_turns(agent_id=ids[0], backend_session_id=ids[0],
        source_file="transcript:synthetic", turns=[{"role":"assistant", "text":"changed"}])
    result = previews.dashboard_messages()
    assert result[ids[0]]["head"]["preview"] == "changed"
    assert len(result) == len(ids) and ids[1] in result
    assert calls == [ids[0]]
    calls.clear()
    assert previews.dashboard_messages() == result
    assert calls == []
