# Reviewer A's probe on 251f7aca (WIP e5db190e), unchanged, as the stage-2 round-8 regression.
"""A goal request triggering a generation switch keeps its thread owner."""
import pytest
from lib import codex_app_server as cas
from tests.unit.test_codex_generations import fake, _goal_agent


@pytest.mark.parametrize('action,expected', [('get', 'active'), ('pause', 'paused')])
def test_a_goal_read_that_triggers_the_switch_uses_the_existing_owner(fake, tmp_path, monkeypatch, action, expected):
    agent, thread = _goal_agent(fake, tmp_path, monkeypatch)
    owner = cas._client(agent, 'goal')
    assert thread in owner._loaded_threads
    (fake / 'auth.json').write_text('account-two')
    goal = cas.goal(agent, action)  # this lookup itself starts the new generation
    assert goal['status'] == expected
