"""Harvesting an agent's working directory is a choice, not a default.

Every transcription used to scrape the agent's `cwd` unconditionally. One
agent rooted at `/home` therefore fed hundreds of library names from a
package tree into the biasing payload, ahead of a hand-written glossary and
inside a fixed budget. Filtering the ugly ones treats the symptom; the cause
is that nobody ever asked for the scanning.

A profile now decides. No profile, or a profile that has not opted in, means
the workspace contributes nothing at all.
"""
from __future__ import annotations

import pytest

from lib import vocab_store


@pytest.fixture
def profile():
    pid = vocab_store.create_profile("Test profile")
    yield pid
    vocab_store.delete_profile(pid)


def test_a_new_profile_does_not_harvest_the_workspace(profile):
    """Off is the safe default: scraping someone's folders is opt-in."""
    assert vocab_store.profile_harvests_workspace(profile) is False


def test_a_profile_can_opt_in_and_out(profile):
    vocab_store.set_profile_harvests_workspace(profile, True)
    assert vocab_store.profile_harvests_workspace(profile) is True
    vocab_store.set_profile_harvests_workspace(profile, False)
    assert vocab_store.profile_harvests_workspace(profile) is False


def test_an_agent_with_no_profile_never_harvests():
    """The common case, and the one that produced the library names."""
    assert vocab_store.agent_harvests_workspace("no-such-agent") is False


def test_an_assigned_profile_decides_for_its_agent(profile):
    from lib import agents as agents_db
    agent_id = agents_db.create_agent(
        persona="Probe", voice_id="v", cwd="/tmp", session="probe-harvest")

    vocab_store.set_profile_harvests_workspace(profile, True)
    vocab_store.assign(profile_id=profile, agent_id=agent_id)
    try:
        assert vocab_store.agent_harvests_workspace(agent_id) is True
        vocab_store.set_profile_harvests_workspace(profile, False)
        assert vocab_store.agent_harvests_workspace(agent_id) is False
    finally:
        vocab_store.unassign(agent_id=agent_id)


def test_profile_detail_reports_the_setting_so_a_client_can_show_it(profile):
    vocab_store.set_profile_harvests_workspace(profile, True)
    detail = vocab_store.profile_detail(profile)
    assert detail is not None
    assert detail["harvests_workspace"] is True


def test_an_unknown_profile_is_treated_as_opted_out():
    assert vocab_store.profile_harvests_workspace("nope") is False
