"""Observable contracts for passive groups, hierarchy, and opt-in coordination."""
import sqlite3
import pytest
from lib import agents, db, team_store, team_leader
from lib.protocol import AgentState


def members(tmp_path):
    return [agents.create_agent(persona=n, voice_id='V', cwd=str(tmp_path), session=n)
            for n in ('lena', 'omar')]


def test_new_team_has_no_behavior_even_with_members(tmp_path):
    lena, omar = members(tmp_path)
    team = team_store.create_team('Clarp')
    for member in (lena, omar):
        team_store.add_member(team['team_id'], member)
    assert not team['leader_enabled']
    assert not team['communication_enabled']
    assert not team['nudge_enabled']
    assert team_store.team_protocol_instruction(lena) == ''
    assert team_store.capture_assistant_message(agent_id=lena, source_message_id='one', text='<team>Hi</team>') == 0
    assert team_store.pending_digest(omar) == ('', [])
    agents.record_state(omar, AgentState.INTERRUPTED)
    assert team_store.latest_activity_for_agent(lena) == 0
    assert team_leader.pending_leader_ticks() == []


def test_deep_groups_and_cycle_rejection_are_atomic():
    root = team_store.create_team('Clarp')
    parent = root
    for i in range(105):
        parent = team_store.create_team(str(i), parent_team_id=parent['team_id'])
    with pytest.raises(ValueError, match='ancestor'):
        team_store.update_team(root['team_id'], name='Wrong', parent_team_id=parent['team_id'])
    assert team_store.get_team(root['team_id'])['name'] == 'Clarp'
    assert team_store.get_team(root['team_id'])['parent_team_id'] is None
    with pytest.raises(ValueError):
        team_store.update_team(parent['team_id'], parent_team_id=parent['team_id'])
    with pytest.raises(ValueError):
        team_store.create_team('Orphan', parent_team_id='missing')
    assert len(team_store.list_teams()) == 106
    team_store.update_team(parent['team_id'], parent_team_id='')
    assert team_store.get_team(parent['team_id'])['parent_team_id'] is None


def test_communication_toggle_stops_backlog_and_does_not_inherit(tmp_path):
    lena, omar = members(tmp_path)
    parent = team_store.create_team('Clarp', communication_enabled=True)
    child = team_store.create_team('Development', parent_team_id=parent['team_id'])
    for team in (parent, child):
        for member in (lena, omar):
            team_store.add_member(team['team_id'], member)
    assert team_store.capture_assistant_message(agent_id=lena, source_message_id='one', text='<team>Hi</team>') == 1
    assert team_store.list_team_messages(child['team_id']) == []
    assert team_store.pending_digest(omar)[1]
    team_store.update_team(parent['team_id'], communication_enabled=False)
    assert team_store.pending_digest(omar) == ('', [])
    assert team_store.team_protocol_instruction(omar) == ''
    assert team_store.latest_activity_for_agent(omar) == 0
    assert team_store.capture_assistant_message(agent_id=lena, source_message_id='two', text='<team>Bye</team>') == 0
    team_store.update_team(child['team_id'], communication_enabled=True)
    assert 'Development' in team_store.team_protocol_instruction(omar)
    assert team_store.pending_digest(omar) == ('', [])


def test_leader_role_independent_of_communication_and_disable_stops_ticks(tmp_path):
    lena, omar = members(tmp_path)
    team = team_store.create_team('Clarp')
    for member in (lena, omar):
        team_store.add_member(team['team_id'], member)
    team_store.set_leader(team['team_id'], lena)
    team_store.set_nudge_enabled(team['team_id'], True)
    agents.record_state(omar, AgentState.INTERRUPTED)
    assert team_leader.pending_leader_ticks()
    assert 'Leader role' in team_store.team_protocol_instruction(lena)
    assert '<team>' not in team_store.team_protocol_instruction(lena)
    assert team_store.team_protocol_instruction(omar) == ''
    team_store.update_team(team['team_id'], leader_enabled=False)
    assert team_store.team_protocol_instruction(lena) == ''
    assert team_store.get_leader(team['team_id']) == ''
    assert team_leader.pending_leader_ticks() == []
    team_store.update_team(team['team_id'], leader_enabled=True)
    team_store.remove_member(team['team_id'], lena)
    assert not team_store.get_team(team['team_id'])['leader_enabled']


def test_deleting_or_archiving_parent_preserves_children():
    parent = team_store.create_team('Clarp')
    child = team_store.create_team('Development', parent_team_id=parent['team_id'])
    team_store.update_team(parent['team_id'], archived=True)
    assert team_store.get_team(child['team_id'])['parent_team_id'] is None
    team_store.update_team(parent['team_id'], archived=False)
    team_store.update_team(child['team_id'], parent_team_id=parent['team_id'])
    team_store.delete_team(parent['team_id'])
    assert team_store.get_team(child['team_id'])['parent_team_id'] is None


def test_migration_preserves_existing_settings_but_new_groups_are_passive():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE teams (team_id TEXT, leader_agent_id TEXT, nudge_enabled INTEGER DEFAULT 1)')
    c.executemany('INSERT INTO teams VALUES (?, ?, ?)', [('a', 'lena', 1), ('b', '', 0)])
    db._migrate_to_v71(c)
    assert c.execute('SELECT leader_enabled, communication_enabled, nudge_enabled FROM teams ORDER BY team_id').fetchall() == [(1, 1, 1), (0, 1, 0)]
    c.execute("INSERT INTO teams (team_id) VALUES ('new')")
    assert c.execute("SELECT leader_enabled, communication_enabled FROM teams WHERE team_id='new'").fetchone() == (0, 0)
