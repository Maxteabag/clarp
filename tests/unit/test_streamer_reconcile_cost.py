"""Roster scans must not resolve executable configuration for every agent."""
import pytest

from lib import config, db
from lib.transcript_streamer import TranscriptStreamer


@pytest.mark.parametrize("count", [1, 1059])
def test_idle_streamer_configuration_work_does_not_scale_with_roster(monkeypatch, count):
    c = db.conn()
    c.executemany("""INSERT INTO agents
        (agent_id, session, persona, voice_id, cwd, backend, created_at, archived_at)
        VALUES (?, ?, ?, '', '/fixture', 'claude', 1, ?)""",
        [(f"stream-cost-{i}", f"stream-cost-{i}", "Synthetic",
          1 if i % 5 == 0 else None) for i in range(count)])
    loads = []
    original = config.load

    def load(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(config, "load", load)
    streamer = TranscriptStreamer()
    streamer.reconcile_once()
    assert streamer._bound == {} and streamer.pool.agent_ids() == []
    # A scan reads a static support flag. Even an implementation that resolves
    # one shared configuration must not do executable I/O per idle agent.
    assert len(loads) <= 1, f"{count} agents caused {len(loads)} config loads"
