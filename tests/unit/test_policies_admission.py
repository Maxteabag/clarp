"""Table-driven decisions for the turn admission policy.

Every origin that ``origins.py`` knows walks through every rule so a new
origin that forgets a row fails here, not in the dispatcher.
"""
import pytest

from lib import origins
from lib.policies import admission as policy
from lib.policies.admission import (
    Allow, HostSettings, LiveWork, Queue, QueueState, Reject, admission)

ALL_ORIGINS = sorted(
    origins.ROUTINE_AUTOMATION_ORIGINS
    | origins.CLIENT_SETTABLE_ORIGINS
    | origins.USER_FACING_ORIGINS
    | origins.SUPPRESSED_ORIGINS
    | {origins.MARKER_ORIGIN}
)
CHAT_AGENT = {"agent_id": "a1", "session": "arnold"}
JANITOR_AGENT = {"agent_id": "j1", "session": "janitor", "is_janitor": 1}
LEADER_TEAMS = [{"leader_enabled": 1, "nudge_enabled": 1, "leader_agent_id": "a1"}]


def decide(origin, *, agent=CHAT_AGENT, teams=(), settings=HostSettings(),
           work=LiveWork(), queue=QueueState()):
    return admission(origin, agent, list(teams), settings, work, queue)


def test_every_known_origin_has_a_row():
    assert set(ALL_ORIGINS) == {
        "agent", "automation", "dreaming", "heartbeat", "janitor", "leader_tick",
        "oracle", "schedule", "system", "user", "watcher",
    }


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_plain_request_outcome_per_origin(origin):
    decision = decide(origin, teams=LEADER_TEAMS)
    if origin == "janitor":
        assert decision == Reject(409, policy.JANITOR_ORIGIN_NEEDS_RUN, janitor=True)
        return
    assert isinstance(decision, Allow)
    effective = decision.effective
    assert effective.origin == origin
    assert effective.notify_herald is (origin not in origins.ROUTINE_AUTOMATION_ORIGINS)
    assert effective.mute_audio is False
    assert effective.queue_if_busy is False
    assert effective.paused_bypass is False
    assert effective.steer_allowed is True       # not queue_if_busy
    assert effective.protected_peer is False     # no sender


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_leader_tick_is_the_only_origin_gated_on_team_nudging(origin):
    decision = decide(origin, teams=[])
    if origin == "leader_tick":
        assert decision == Reject(409, policy.LEADER_NUDGE_DISABLED)
    elif origin == "janitor":
        assert isinstance(decision, Reject)
    else:
        assert isinstance(decision, Allow)


@pytest.mark.parametrize("teams,allowed", [
    ([], False),
    ([{"leader_enabled": 1, "nudge_enabled": 0, "leader_agent_id": "a1"}], False),
    ([{"leader_enabled": 0, "nudge_enabled": 1, "leader_agent_id": "a1"}], False),
    ([{"leader_enabled": 1, "nudge_enabled": 1, "leader_agent_id": "other"}], False),
    ([{"leader_enabled": 1, "nudge_enabled": 1, "leader_agent_id": "a1"}], True),
    ([{"leader_enabled": 0, "nudge_enabled": 0, "leader_agent_id": "a1"},
      {"leader_enabled": 1, "nudge_enabled": 1, "leader_agent_id": "a1"}], True),
])
def test_leader_nudge_gate_matches_dispatch_and_spawn(teams, allowed):
    assert policy.leader_nudge_allowed("a1", teams) is allowed
    decision = decide("leader_tick", teams=teams)
    assert isinstance(decision, Allow) is allowed


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_heartbeats_disabled_only_refuses_heartbeat(origin):
    decision = decide(origin, teams=LEADER_TEAMS,
                      settings=HostSettings(heartbeats_disabled=True))
    if origin == "heartbeat":
        assert decision == Reject(409, policy.HEARTBEATS_DISABLED)
    elif origin == "janitor":
        assert isinstance(decision, Reject)
    else:
        assert isinstance(decision, Allow)


@pytest.mark.parametrize("origin,client_msg_id,verdict,expected", [
    ("heartbeat", "janitor-demand-1", False, "reject"),
    ("heartbeat", "janitor-demand-1", True, "allow"),
    ("heartbeat", "janitor-demand-1", None, "allow"),   # not looked up
    ("heartbeat", "cmid-1", False, "allow"),            # not a demand id
    ("user", "janitor-demand-1", False, "allow"),       # not a heartbeat
])
def test_janitor_demand_authority(origin, client_msg_id, verdict, expected):
    assert policy.needs_janitor_demand_check(origin, client_msg_id) is (
        origin == "heartbeat" and client_msg_id.startswith("janitor-demand-"))
    decision = decide(origin, work=LiveWork(
        client_msg_id=client_msg_id, janitor_demand_valid=verdict))
    if expected == "reject":
        assert decision == Reject(409, policy.HEARTBEAT_AUTHORITY_CHANGED)
    else:
        assert isinstance(decision, Allow)


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_janitor_agent_accepts_only_admitted_runs(origin):
    # Any origin without a run is refused for a janitor agent.
    assert decide(origin, agent=JANITOR_AGENT) == Reject(
        409, policy.JANITOR_RUNS_ONLY, janitor=True)
    # A run that is not active for the configuration is refused.
    assert decide(origin, agent=JANITOR_AGENT, work=LiveWork(
        janitor_run_id="run-1", janitor_run_active=False)) == Reject(
        409, policy.JANITOR_RUN_INACTIVE, janitor=True)
    # An active run rewrites the request regardless of the requested origin.
    decision = decide(origin, agent=JANITOR_AGENT, work=LiveWork(
        janitor_run_id="run-1", janitor_run_active=True))
    assert isinstance(decision, Allow)
    assert decision.effective.origin == "janitor"
    assert decision.effective.client_msg_id == "run-1"
    assert decision.effective.queue_if_busy is True
    assert decision.effective.mute_audio is True
    assert decision.effective.notify_herald is False
    assert decision.effective.steer_allowed is False


def test_maintenance_run_never_targets_a_chat_agent():
    assert decide("user", work=LiveWork(janitor_run_id="run-1")) == Reject(
        409, policy.JANITOR_RUN_NOT_JANITOR, janitor=True)


def test_maintenance_run_ids_must_match():
    work = LiveWork(janitor_run_id="run-1", janitor_run_active=True,
                    client_msg_id="other")
    assert decide("user", agent=JANITOR_AGENT, work=work) == Reject(
        409, policy.JANITOR_REQUEST_ID_MISMATCH, janitor=True)
    work = LiveWork(janitor_run_id="run-1", janitor_run_active=True,
                    durable_queue_id="other")
    assert decide("user", agent=JANITOR_AGENT, work=work) == Reject(
        409, policy.JANITOR_QUEUE_ID_MISMATCH, janitor=True)
    work = LiveWork(janitor_run_id="run-1", janitor_run_active=True,
                    client_msg_id="run-1", durable_queue_id="run-1")
    assert isinstance(decide("user", agent=JANITOR_AGENT, work=work), Allow)


def test_heartbeat_disabled_wins_over_nothing_but_janitor_rules():
    # Order: janitor target/run rules precede the heartbeat switch.
    decision = decide("heartbeat", agent=JANITOR_AGENT,
                      settings=HostSettings(heartbeats_disabled=True))
    assert decision == Reject(409, policy.JANITOR_RUNS_ONLY, janitor=True)


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize("skip_admission", [False, True])
def test_paused_queue_is_bypassed_only_by_fresh_user_or_oracle_intent(
        origin, skip_admission):
    work = LiveWork(queue_if_busy=True, skip_admission=skip_admission)
    decision = decide(origin, teams=LEADER_TEAMS, work=work,
                      queue=QueueState(paused=True))
    if origin == "janitor":
        assert isinstance(decision, Reject)
        return
    fresh = origin in {"user", "oracle"} and not skip_admission
    if fresh:
        assert isinstance(decision, Allow)
        assert decision.effective.paused_bypass is True
    else:
        assert isinstance(decision, Queue)
        assert decision.reason == "paused-queue"
        assert decision.effective.origin == origin
        assert decision.effective.paused_bypass is False


def test_paused_queue_is_ignored_when_explicitly_allowed_or_not_queueing():
    paused = QueueState(paused=True)
    allowed = decide("agent", work=LiveWork(queue_if_busy=True, allow_paused_queue=True),
                     queue=paused)
    assert isinstance(allowed, Allow) and allowed.effective.paused_bypass is False
    direct = decide("agent", work=LiveWork(queue_if_busy=False), queue=paused)
    assert isinstance(direct, Allow) and direct.effective.paused_bypass is False


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_steering_is_allowed_for_direct_sends_and_oracle_queues(origin):
    if origin == "janitor":
        return
    queued = decide(origin, teams=LEADER_TEAMS, work=LiveWork(queue_if_busy=True))
    assert queued.effective.steer_allowed is (origin == "oracle")
    direct = decide(origin, teams=LEADER_TEAMS, work=LiveWork(queue_if_busy=False))
    assert direct.effective.steer_allowed is True


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_only_agent_origin_with_a_sender_is_a_protected_peer(origin):
    if origin == "janitor":
        return
    with_sender = decide(origin, teams=LEADER_TEAMS, work=LiveWork(sender_agent_id="a2"))
    assert with_sender.effective.protected_peer is (origin == "agent")
    without = decide(origin, teams=LEADER_TEAMS)
    assert without.effective.protected_peer is False


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_post_spawn_traits_table(origin):
    traits = policy.traits(origin)
    assert traits.tracks_oracle_delegation is (origin == "oracle")
    assert traits.records_dreaming_result is (origin == "dreaming")
    assert traits.records_heartbeat_noop_on_failure is (origin == "heartbeat")


def test_traits_of_unknown_or_blank_origin_are_inert():
    for value in ("", None, "made-up"):
        assert policy.traits(value) == policy.OriginTraits()


def test_origin_is_stripped_before_matching():
    decision = decide("  heartbeat ", settings=HostSettings(heartbeats_disabled=True))
    assert decision == Reject(409, policy.HEARTBEATS_DISABLED)
