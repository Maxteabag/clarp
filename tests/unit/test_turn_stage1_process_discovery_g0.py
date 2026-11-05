"""A broken process probe must not hide another live session owner."""
from types import SimpleNamespace

import pytest

from lib import turn_dispatch as td


class Handle:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


class BrokenHandle:
    def is_alive(self):
        raise OSError('synthetic process probe failure')


def test_broken_or_missing_probes_do_not_hide_later_live_handles():
    first, second = Handle(True), Handle(True)
    assert td._alive([object(), BrokenHandle(), Handle(False), first, second]) == [first, second]
    assert td._alive([]) == []


@pytest.mark.parametrize(('mode', 'count', 'held'), [
    ('missing', 0, False),
    ('empty', 0, False),
    ('live', 1, True),
    ('failure', 0, True),
])
def test_session_discovery_and_repair_keep_the_existing_uncertainty_policy(tmp_path, mode, count, held):
    live = Handle(True)
    registry = SimpleNamespace()
    if mode != 'missing':
        def holders(backend, agent_id):
            assert (backend, agent_id) == ('claude', 'synthetic-agent')
            if mode == 'failure':
                raise OSError('synthetic holder query failure')
            return [BrokenHandle(), live] if mode == 'live' else []
        registry.turn_holders = holders
    service = td.TurnDispatchService(SimpleNamespace(stream=None), backend_registry=registry, home=tmp_path)
    spec = SimpleNamespace(backend='claude', agent_id='synthetic-agent')
    found = service._session_holds(spec)
    assert len(found) == count
    if count:
        assert found == [live]
    assert service._session_held(spec.agent_id, spec.backend) is held
