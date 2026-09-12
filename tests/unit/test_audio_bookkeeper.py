from concurrent.futures import ThreadPoolExecutor
import pytest
from lib import db, agents, janitors, janitor_builtins, clip_store, audio_bookkeeper

@pytest.fixture
def setup():
    owner=janitor_builtins.ensure_builtins()['audio-bookkeeper']
    target=agents.create_agent(persona='Ada',voice_id='v',cwd='/tmp',session='ada')
    return owner,target

def clip(target,path='/tmp/a.mp3'):
    return clip_store.record_clip(agent_id=target,path=path,runtime_id=lambda _:None)

def test_real_producer_and_ack_changes_create_janitor_receipts(setup):
    owner,target=setup
    cid=clip(target)
    clip_store.mark_clip_status(clip_id=cid,status='play-start')
    clip_store.mark_clip_status(clip_id=cid,status='play-ok')
    n=audio_bookkeeper.drain()
    assert n==4
    assert audio_bookkeeper.drain()==0
    rows=db.conn().execute('SELECT * FROM janitor_runs WHERE agent_id=?',(owner['agent_id'],)).fetchall()
    assert len(rows)==4
    assert all(r['status']=='completed' for r in rows)
    assert all(janitors._decode(r['configuration_json'],{})['executor']=='deterministic' for r in rows)
    assert db.conn().execute('SELECT COUNT(*) FROM queued_turns').fetchone()[0]==0
    assert db.conn().execute('SELECT COUNT(*) FROM janitor_demand_claims').fetchone()[0]==0

def test_duplicate_ack_and_repeated_play_are_first_observations_not_attempts(setup):
    _,target=setup;cid=clip(target)
    for status in ['play-start','play-ok','play-start','play-ok']:
        clip_store.mark_clip_status(clip_id=cid,status=status)
    assert audio_bookkeeper.drain()==4
    assert db.conn().execute('SELECT COUNT(*) FROM audio_bookkeeping_events WHERE clip_id=?',(cid,)).fetchone()[0]==4

def test_pause_keeps_outbox_for_resume_and_generation_is_current(setup):
    owner,target=setup
    paused=janitors.set_enabled(owner['session'],owner['revision'],False)
    clip(target)
    assert audio_bookkeeper.drain()==0
    enabled=janitors.set_enabled(owner['session'],paused['revision'],True)
    assert audio_bookkeeper.drain()==2
    assert {r[0] for r in db.conn().execute('SELECT generation FROM janitor_runs WHERE agent_id=?',(owner['agent_id'],))}=={enabled['generation']}

def test_transaction_rollback_leaves_no_clip_or_event(setup):
    _,target=setup;c=db.conn();c.execute('BEGIN IMMEDIATE');clip(target);c.execute('ROLLBACK')
    assert c.execute('SELECT COUNT(*) FROM clips').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM audio_bookkeeping_events').fetchone()[0]==0

def test_concurrent_drains_commit_once(setup):
    owner,target=setup;clip(target)
    with ThreadPoolExecutor(2) as pool:counts=list(pool.map(lambda _:audio_bookkeeper.drain(),range(2)))
    assert sum(counts)==2
    assert db.conn().execute('SELECT COUNT(*) FROM janitor_runs WHERE agent_id=?',(owner['agent_id'],)).fetchone()[0]==2

def test_producer_failure_is_distinct_and_no_error_text_is_copied(setup):
    _,target=setup;cid=clip(target)
    clip_store.mark_clip_producer_status(clip_id=cid,producer_status='failed',error='private provider detail')
    assert audio_bookkeeper.drain()==3
    assert 'private provider detail' not in str([tuple(r) for r in db.conn().execute('SELECT * FROM janitor_demand_results')])

def test_upgrade_from_v83_installs_outbox_and_trigger_without_rewriting_clips(setup):
    _,target=setup;c=db.conn()
    for name in ['audio_bookkeeping_created','audio_bookkeeping_producer','audio_bookkeeping_client']:
        c.execute('DROP TRIGGER '+name)
    c.execute('DROP TABLE audio_bookkeeping_events')
    cid=clip(target)
    c.execute('PRAGMA user_version=83')
    db._migrate(c)
    assert c.execute('PRAGMA user_version').fetchone()[0]==84
    assert c.execute('SELECT COUNT(*) FROM audio_bookkeeping_events').fetchone()[0]==0
    clip_store.mark_clip_status(clip_id=cid,status='play-ok')
    assert audio_bookkeeper.drain()==1

def test_two_clips_same_stage_remain_distinct(setup):
    _,target=setup;clip(target);clip(target,'/tmp/b.mp3')
    assert audio_bookkeeper.drain()==4

def test_atomic_receipt_failure_rolls_back_outbox_completion(setup):
    _,target=setup;clip(target);c=db.conn()
    c.execute("CREATE TRIGGER fail_audio_result BEFORE INSERT ON janitor_demand_results BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(Exception,match='injected failure'):audio_bookkeeper.drain()
    assert c.execute('SELECT COUNT(*) FROM janitor_runs').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM audio_bookkeeping_events WHERE completed_at IS NOT NULL').fetchone()[0]==0
    c.execute('DROP TRIGGER fail_audio_result')
    assert audio_bookkeeper.drain()==2

def test_excluded_pending_target_does_not_starve_eligible_target(setup):
    owner,target=setup
    other=agents.create_agent(persona='Bob',voice_id='v',cwd='/tmp',session='bob')
    config=janitors.configure(owner['session'],owner['revision'],scope={'agent_ids':[other]})
    janitors.set_enabled(config['session'],config['revision'],True)
    for i in range(20):clip(target,f'/tmp/excluded-{i}.mp3')
    clip(other,'/tmp/eligible.mp3')
    assert audio_bookkeeper.drain(limit=2)==2
    assert db.conn().execute('SELECT COUNT(*) FROM audio_bookkeeping_events WHERE agent_id=? AND completed_at IS NULL',(target,)).fetchone()[0]==40


def test_deterministic_role_cannot_admit_provider_work(setup):
    _,target=setup
    assert janitor_builtins.begin_run('audio-bookkeeper','no-model',target_agent_id=target) is None
    assert db.conn().execute('SELECT COUNT(*) FROM janitor_demand_claims').fetchone()[0]==0
