"""B2: a bookkeeping delegate on another provider never reads an EU agent.

``goal_ledger.enable`` refuses a delegate whose provider profile differs from
the principal's, and ``observe`` (through ``_authorize``) refuses an existing
delegation that crosses profiles, so the principal's messages never reach
another provider's turns. An ordinary principal and helper keep working.
"""
from __future__ import annotations

import pytest

from lib import agents, db, goal_ledger as ledger, message_store

pytestmark = pytest.mark.usefixtures("fake_codex_profile")

SENTINEL = "SENTINEL-EU-LEDGER-7d21"


def _agent(session, *, parent=None, backend="claude", profile=""):
    agent_id = agents.create_agent(persona=session.title(), voice_id="v", cwd="/tmp",
                                   session=session, backend=backend,
                                   **({"provider_profile": profile} if profile else {}))
    if parent:
        agents.set_lineage(agent_id, parent_agent_id=parent, role="helper")
    agents.start_runtime(agent_id, session)
    return agents.get_by_agent_id(agent_id)


def test_enabling_a_delegate_outside_the_principals_profile_is_refused():
    eu = _agent("eu", backend="codex", profile="example-eu")
    helper = _agent("eu-books", parent=eu["agent_id"])

    with pytest.raises(ledger.LedgerError, match="provider_profile_boundary"):
        ledger.enable(eu, helper, "keep the EU agent's books")

    plain = _agent("pip")                              # control: ordinary pair
    accountant = _agent("pip-books", parent=plain["agent_id"])
    assert ledger.enable(plain, accountant, "books")["delegation_id"]


def test_an_existing_cross_profile_delegation_cannot_observe_the_eu_agent():
    eu = _agent("eu", backend="codex", profile="example-eu")
    helper = _agent("eu-books", parent=eu["agent_id"])
    agents.bind_backend_session(eu["agent_id"], "native-eu")
    message_store.record_user_message(
        agent_id=eu["agent_id"], backend_session_id="native-eu", client_msg_id="c1",
        text=f"customer invoice {SENTINEL}", origin="user", sender_agent_id=None)
    token = "tok-cross"
    db.conn().execute(
        """INSERT INTO goal_delegations (delegation_id, principal_agent_id, delegate_agent_id,
               status, token_hash, created_at, created_reason, message_through)
           VALUES ('dgcross', ?, ?, 'active', ?, 1, 'pre-fix', 0)""",
        (eu["agent_id"], helper["agent_id"], ledger._hash(token)))

    with pytest.raises(ledger.DelegationDenied, match="provider_profile_boundary"):
        ledger.observe("dgcross", token, caller=helper["agent_id"])
