from lib.janitor_active_interval import advance, DEFAULTS


def test_idle_sleeps_and_resume_does_not_replay_missed_intervals():
    state = {"pending": {"old": {}}}
    assert not advance(state, DEFAULTS, now=0, active=False)
    assert state["pending"] == {} and state["next_run_at"] is None
    assert advance(state, DEFAULTS, now=1_000, active=True)
    assert not advance(state, DEFAULTS, now=900_999, active=True)
    assert advance(state, DEFAULTS, now=901_000, active=True)
    assert not advance(state, DEFAULTS, now=901_001, active=True)
    assert not advance(state, DEFAULTS, now=1_000_000, active=False)
    assert advance(state, DEFAULTS, now=100_000_000, active=True)
    assert not advance(state, DEFAULTS, now=100_000_001, active=True)


def test_resume_can_wait_a_full_interval_and_restart_keeps_deadline():
    config = {**DEFAULTS, "interval_seconds": 60, "run_on_resume": False}
    state = {}
    assert not advance(state, config, now=10, active=True)
    assert not advance(dict(state), config, now=60_009, active=True)
    assert advance(state, config, now=60_010, active=True)


def test_long_stall_admits_only_one_occurrence():
    state = {}
    assert advance(state, DEFAULTS, now=0, active=True)
    assert advance(state, DEFAULTS, now=999_999_999, active=True)
    assert state["next_run_at"] == 1_000_899_999
