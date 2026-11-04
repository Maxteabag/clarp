# Reviewer A's probe on d8cd925b (WIP ed6afbe8), unchanged, as the stage-2 round-9 regression.
"""Release detection must see a move's own accidental interruption too."""
from lib import codex_app_server as cas
from tests.unit.test_codex_generations import fake, _agent, _turn


def test_an_internal_draining_shutdown_is_not_labelled_a_user_stop(fake, tmp_path, monkeypatch, capsys):
    agent = _agent(tmp_path, 'internal-interrupt')
    done, outcome, _ = _turn(tmp_path, agent, 'internal-interrupt', '[qa-slow] still running')
    client = cas._client(agent, 'internal-interrupt')
    handle = cas.active_handles(agent)[0]
    assert not getattr(handle, 'stop_requested', False)
    client.draining = True
    original_stop = client._stop_process
    def wait_for_terminal_then_stop():
        assert done.wait(3), 'control: consume the interrupted terminal event before killing transport'
        original_stop()
    monkeypatch.setattr(client, '_stop_process', wait_for_terminal_then_stop)
    # Deliberately exercise the internal shutdown failure the release check
    # is supposed to detect; no public Stop/terminate was requested.
    client.shutdown()
    assert outcome == ['codex turn interrupted']
    error_log = capsys.readouterr().err
    assert 'codexTurnInterruptedUnasked' in error_log and 'moving=True' in error_log, \
        'internal shutdown hid a move-caused interruption by marking it as a user Stop'
