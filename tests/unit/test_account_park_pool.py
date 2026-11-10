# Reviewer A's probe on 0b74de58 (WIP e9e5866e), kept unchanged as the regression.
"""A trace's expired park cannot restart when another agent starts pool recovery."""
import pytest

from lib.account_failover import Attempt
from tests.unit.test_account_park_cap import _coordinator, _attempt, _run_park_to_expiry


@pytest.mark.parametrize('retry_order', ['before_other_recovery', 'during_other_recovery'])
def test_expired_work_is_not_reparked_by_another_agents_recovery(retry_order):
    coordinator, clock, scheduled = _coordinator()
    resumed = []
    coordinator.register(_attempt('expired-trace', resumed))
    assert coordinator.request('synthetic', 'expired-trace', ('selector',))
    _run_park_to_expiry(coordinator, clock, scheduled)
    assert resumed == ['expired-trace'] and not coordinator.recovering
    paused = []
    retry = Attempt('synthetic', 'expired-trace', 'sonnet', {}, lambda: True,
                    lambda: paused.append('expired'), lambda: None)
    retry.spawned.set()
    other = Attempt('other-agent', 'new-trace', 'sonnet', {}, lambda: True,
                    lambda: paused.append('other'), lambda: None)
    other.spawned.set()
    if retry_order == 'before_other_recovery':
        assert coordinator.register(retry) is False
    assert coordinator.register(other) is False
    assert coordinator.request('other-agent', 'new-trace', ('selector',))
    if retry_order == 'during_other_recovery':
        parked = coordinator.register(retry)
    else:
        parked = False
    assert 'other' in paused, 'control: the other agents new work still parks'
    assert not parked and 'expired' not in paused, 'another agents recovery restarted the expired traces park'
