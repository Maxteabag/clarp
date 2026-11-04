"""VP-04: audio bookkeeping observations and their janitor runs are pruned with
the clip retention, in small transactions, instead of growing for ever."""
import time
from types import SimpleNamespace

import pytest

from lib import agents, audio_bookkeeper, clip_store, db, janitor_builtins, janitors, maintenance

DAY_MS = 24 * 60 * 60 * 1000
NOW = 1_800_000_000_000
OLD = NOW - 31 * DAY_MS
RECENT = NOW - 1 * DAY_MS


@pytest.fixture
def setup():
    owner = janitor_builtins.ensure_builtins()['audio-bookkeeper']
    target = agents.create_agent(persona='Ada', voice_id='v', cwd='/tmp', session='ada')
    return owner, target


def _clip(target, path):
    return clip_store.record_clip(agent_id=target, path=path, runtime_id=lambda _: None)


def _age(clip_id, at):
    c = db.conn()
    ids = [r[0] for r in c.execute('SELECT event_id FROM audio_bookkeeping_events WHERE clip_id=?', (clip_id,))]
    c.execute('UPDATE audio_bookkeeping_events SET created_at=? WHERE clip_id=?', (at, clip_id))
    for event_id in ids:
        c.execute('UPDATE janitor_runs SET created_at=?, started_at=?, finished_at=? WHERE run_id=?',
                  (at, at, at, f'audio-bookkeeping-{event_id}'))
    return ids


def _count(sql, *args):
    return db.conn().execute(sql, args).fetchone()[0]


def _prune():
    return maintenance.prune_database(now_ms=NOW)


@pytest.fixture
def no_pause(monkeypatch):
    pauses = []
    monkeypatch.setattr(audio_bookkeeper, 'time', SimpleNamespace(
        sleep=pauses.append, monotonic=time.monotonic), raising=False)
    return pauses


def test_old_observations_and_their_runs_go_and_recent_ones_stay(setup, no_pause):
    _owner, target = setup
    old_clip, new_clip = _clip(target, '/tmp/a.mp3'), _clip(target, '/tmp/b.mp3')
    assert audio_bookkeeper.drain() == 4
    old_events = _age(old_clip, OLD)
    _age(new_clip, RECENT)
    counts = _prune()
    assert counts['audio_bookkeeping_events'] == 2
    assert counts['audio_bookkeeping_runs'] == 2
    assert _count('SELECT COUNT(*) FROM audio_bookkeeping_events WHERE clip_id=?', old_clip) == 0
    assert _count('SELECT COUNT(*) FROM audio_bookkeeping_events WHERE clip_id=?', new_clip) == 2
    old_runs = [f'audio-bookkeeping-{e}' for e in old_events]
    marks = ','.join('?' * len(old_runs))
    assert _count(f'SELECT COUNT(*) FROM janitor_runs WHERE run_id IN ({marks})', *old_runs) == 0
    assert _count(f'SELECT COUNT(*) FROM janitor_demand_results WHERE run_id IN ({marks})', *old_runs) == 0
    assert _count("SELECT COUNT(*) FROM janitor_runs WHERE run_id GLOB 'audio-bookkeeping-*'") == 2


def test_an_old_observation_no_janitor_will_drain_is_dropped(setup, no_pause):
    owner, target = setup
    janitors.set_enabled(owner['session'], owner['revision'], False)
    clip_id = _clip(target, '/tmp/a.mp3')
    assert audio_bookkeeper.drain() == 0
    _age(clip_id, OLD)
    assert _prune()['audio_bookkeeping_events'] == 2
    assert _count('SELECT COUNT(*) FROM audio_bookkeeping_events') == 0


def test_other_janitor_runs_are_kept(setup, no_pause):
    _owner, target = setup
    clip_id = _clip(target, '/tmp/a.mp3')
    audio_bookkeeper.drain()
    event_id = _age(clip_id, OLD)[0]
    db.conn().execute(
        """INSERT INTO janitor_runs (run_id, agent_id, session, attachment_id, generation, trace_id,
                                     status, outcome, candidates_json, configuration_json,
                                     created_at, started_at, finished_at)
           SELECT 'routing-old', agent_id, session, attachment_id, generation, 'routing-old-trace',
                  status, outcome, candidates_json, configuration_json, created_at, started_at, finished_at
             FROM janitor_runs WHERE run_id=?""", (f'audio-bookkeeping-{event_id}',))
    _prune()
    assert _count("SELECT COUNT(*) FROM janitor_runs WHERE run_id='routing-old'") == 1
    assert _count("SELECT COUNT(*) FROM janitor_runs WHERE run_id GLOB 'audio-bookkeeping-*'") == 0


def test_a_pass_deletes_in_small_transactions_up_to_its_cap(setup, no_pause, capsys):
    _owner, target = setup
    db.conn().executemany(
        "INSERT INTO audio_bookkeeping_events(clip_id, agent_id, stage, created_at) VALUES (?, ?, 'client:queued', ?)",
        [(n, target, OLD) for n in range(1, 2101)])
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        counts = _prune()
    finally:
        db.conn().set_trace_callback(None)
    assert counts['audio_bookkeeping_events'] == 2000, 'at most 2000 rows per pass'
    assert _count('SELECT COUNT(*) FROM audio_bookkeeping_events') == 100
    event_deletes = [s for s in statements if s.lstrip().startswith('DELETE FROM audio_bookkeeping_events')]
    assert len(event_deletes) == 10, 'batches of 200'
    assert sum(1 for s in statements if s.strip() == 'BEGIN IMMEDIATE') >= len(event_deletes), \
        'each batch is its own transaction'
    assert len(no_pause) >= 9, 'a short pause between batches'
    err = capsys.readouterr().err
    assert 'audioBookkeepingPruned' in err and 'longest_batch_ms=' in err
