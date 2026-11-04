# Reviewer A's probe on 7d9f4367 (WIP 00e897e8), unchanged, as the stage-2 round-7 regression.
"""notSubscribed is not end evidence for a loaded thread's writer lock."""
import threading
import pytest
from lib import codex_app_server as cas
from tests.unit.test_codex_generations import fake, _agent, _turn


def test_a_not_subscribed_reply_does_not_release_a_live_writer(fake, tmp_path, monkeypatch):
    monkeypatch.setenv('CLARP_QA_UNLOAD_DELAY', 'never')
    agent = _agent(tmp_path, 'unsubscribed-owner')
    done, outcome, thread = _turn(tmp_path, agent, 'unsubscribed-owner', 'hello')
    assert done.wait(5) and outcome == ['completed']
    client = cas._client(agent, 'unsubscribed-owner')
    client.draining = True
    monkeypatch.setattr(cas, '_DRAINING', {id(client): client})
    monkeypatch.setattr(cas, '_CLIENTS', {})
    (fake / 'auth.json').write_text('account-two')
    original_request, original_shutdown = client.request, client.shutdown
    unsubscribes = []
    def request(method, params, timeout=30):
        result = original_request(method, params, timeout=timeout)
        if method == 'thread/unsubscribe':
            unsubscribes.append(True)
            if len(unsubscribes) == 1:
                raise RuntimeError('synthetic lost reply after unsubscribe applied')
            # Pinned Codex returns this for the repeat: get_thread succeeded,
            # but this connection was already unsubscribed (thread_processor.rs:1028-1045).
            return {'status': 'notSubscribed'}
        return result
    monkeypatch.setattr(client, 'request', request)
    client.drain_step()
    assert thread[0] in client._loaded_threads and client.proc.poll() is None
    exiting, release = threading.Event(), threading.Event()
    def shutdown():
        exiting.set()
        assert release.wait(5)
        original_shutdown()
    monkeypatch.setattr(client, 'shutdown', shutdown)
    errors = []
    def repeat_drain():
        try:
            client.drain_step()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=repeat_drain)
    worker.start()
    try:
        exiting.wait(.3) # correct code may simply keep the loaded hold
        assert client.proc.poll() is None
        with pytest.raises(cas.CodexSessionStuck):
            _turn(tmp_path, agent, 'unsubscribed-owner', 'must wait', thread=thread[0])
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors, errors
