"""ActivityKit Live Activity pushes (docs/live-items.md §8): a phone registers
its activity's push token, and the Host keeps the activity current with the
fleet's aggregate status: how many agents work, how many need the owner, and
the lead agent with its current tool and start time. Updates are throttled
to Apple's budget, except when something needs the owner."""
from __future__ import annotations

import json
import sqlite3

import pytest

from lib import agents as agents_db
from lib import db, live_activity


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.timers = []

    def __call__(self):
        return self.now

    def schedule(self, delay, fn):
        timer = [self.now + delay, fn, False]
        self.timers.append(timer)

        class _H:
            def cancel(self_inner):
                timer[2] = True
        return _H()

    def advance(self, seconds):
        self.now += seconds
        for timer in [t for t in self.timers if not t[2] and t[0] <= self.now]:
            timer[2] = True
            timer[1]()


def _status(agent_id, session, state, *, tool=None, started=None, turn_started=None, headline=None):
    return {"type": "live", "agent_id": agent_id, "session": session, "conv": f"c-{session}",
            "epoch": "e", "lseq": 1, "ops": [{"op": "status", "conv": f"c-{session}", "activity": {
                "state": state, "headline": headline, "turn_started_ms": turn_started,
                "tool": ({"name": tool, "call_id": "c1", "label": "npm test",
                          "started_at_ms": started} if tool else None)}}]}


def _agents():
    a = agents_db.create_agent(persona="Rachel", voice_id="v", cwd="/tmp", session="rachel")
    b = agents_db.create_agent(persona="Mike", voice_id="v", cwd="/tmp", session="mike")
    return a, b


def test_a_registered_activity_token_is_listed_until_dropped():
    live_activity.register(token="ab" * 32, activity_id="act-1", kind="agents-working",
                           environment="sandbox")
    live_activity.register(token="cd" * 32, activity_id="act-1", kind="agents-working")
    [row] = live_activity.tokens()
    assert (row["token"], row["activity_id"], row["environment"]) == ("cd" * 32, "act-1", "sandbox")
    assert live_activity.unregister(activity_id="act-1") == 1
    assert live_activity.tokens() == []


def test_content_state_counts_work_and_names_the_lead():
    rachel, mike = _agents()
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append((payload, priority)),
        clock=clock, schedule=clock.schedule)
    pusher.observe(_status(rachel, "rachel", "tool", tool="Bash", started=5_000,
                           turn_started=4_000, headline="Running npm test"))
    pusher.observe(_status(mike, "mike", "waiting", headline="Needs attention"))
    clock.advance(20)
    state = sent[-1][0]["aps"]["content-state"]
    assert (state["working"], state["needs_you"]) == (1, 1)
    assert state["lead"] == {"agent_id": rachel, "session": "rachel", "persona": "Rachel",
                             "state": "tool", "headline": "Running npm test", "tool": "Bash",
                             "started_at_ms": 5_000, "turn_started_ms": 4_000}
    assert [row["persona"] for row in state["agents"]] == ["Mike", "Rachel"]
    aps = sent[-1][0]["aps"]
    assert aps["event"] == "update" and aps["stale-date"] > aps["timestamp"]


def test_updates_are_throttled_but_attention_goes_out_at_once():
    rachel, mike = _agents()
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append((payload, priority)),
        clock=clock, schedule=clock.schedule, min_interval=15.0)
    pusher.observe(_status(rachel, "rachel", "thinking", turn_started=1))
    assert len(sent) == 1                       # a turn starting: at once
    for label in ("make a", "make b", "make c"):
        clock.advance(1)
        pusher.observe(_tool_status(rachel, "rachel", "Bash", label, "exec", started=2))
    assert len(sent) == 1                       # tool churn waits for the window
    clock.advance(15)
    assert len(sent) == 2 and sent[-1][0]["aps"]["content-state"]["lead"]["headline"] == "Running make c"
    pusher.observe(_status(mike, "mike", "waiting"))
    assert len(sent) == 3 and sent[-1][1] == "10"   # needs you: immediate, high priority
    assert sent[1][1] == "5"


def test_direct_apns_sends_live_activity_pushes_to_the_liveactivity_topic():
    from lib import apns

    class Client:
        def __init__(self):
            self.headers = None

        def post(self, url, headers, content):
            self.headers = headers

            class R:
                status_code = 200
                headers = {"apns-id": "x"}
            return R()

    client = Client()
    apns._send_one(client, "https://api.sandbox.push.apple.com", "jwt", "com.clarp.app",
                   "ab" * 32, {"aps": {}}, push_type="liveactivity", priority="5")
    assert client.headers["apns-topic"] == "com.clarp.app.push-type.liveactivity"
    assert client.headers["apns-push-type"] == "liveactivity"


def _badge_pages(agent_id, session, count):
    """Small real catalogue; one off-page body is larger than the rest."""
    rows = [(f"badge-extra-{i:04d}", agent_id, session, f"Failure {i}",
             json.dumps({"content": "x" * (80000 if i == count - 1 else 256)}),
             2_000_000_000_000 + i, 2_000_000_000_000 + i) for i in range(count)]
    db.conn().executemany("""INSERT INTO artifacts
        (artifact_id,agent_id,session,type,title,status,payload_json,created_at,updated_at)
        VALUES (?,?,?,'document',?,'failed',?,?,?)""", rows)
    return [row[0] for row in rows]


@pytest.mark.parametrize("extra", [0, 410], ids=["one-page", "three-pages"])
def test_needs_you_matches_the_updates_badge(monkeypatch, extra):
    from lib import artifacts, janitor_attention

    rachel, mike = _agents()
    nina = agents_db.create_agent(persona="Nina", voice_id="v", cwd="/tmp", session="nina")
    artifacts.create_decision(session="mike", title="Ship?", question="Ship it?")
    archived = artifacts.create_decision(session="mike", title="Old", question="Old?")
    artifacts.archive(archived["artifact_id"], archived=True,
                      expected_updated_at=archived["updated_at"])
    artifacts.create(session="rachel", type="document", title="Build failed", status="failed",
                     payload={"content": "log"})
    stale = artifacts.create(session="nina", type="document", title="Old failure", status="failed",
                             payload={"content": "log"})
    artifacts.archive(stale["artifact_id"], archived=True, expected_updated_at=stale["updated_at"])
    artifacts.create(session="nina", type="document", title="Report", status="ready",
                     payload={"content": "x"})   # review, not response
    monkeypatch.setattr(janitor_attention, "pending",
                        lambda include_archived=False: [{"artifact_id": "j1", "agent_id": "janitor-1"}])
    _badge_pages(rachel, "rachel", extra)
    scans = []
    def count_global_selection(sql):
        normalized = " ".join(sql.lower().split())
        if "as pj_object" in normalized and "from artifacts a join agents g" in normalized:
            scans.append(sql)
    db.conn().set_trace_callback(count_global_selection)
    try:
        attention = live_activity.badge_attention()
    finally:
        db.conn().set_trace_callback(None)
    assert attention["count"] == 3 + extra
    assert attention["agent_ids"] == {mike, rachel, "janitor-1"}
    assert attention["items"] == [mike, "janitor-1"] + [rachel] * (1 + extra)
    assert not db.conn().in_transaction
    assert len(scans) == 1  # full literal badge oracle precedes the intended RED

    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append((payload, priority)),
        clock=clock, schedule=clock.schedule, attention=live_activity.badge_attention,
        roster=agents_db.list_agents)
    pusher.observe(_status(mike, "mike", "waiting"))      # already counted by its decision
    pusher.observe(_status(nina, "nina", "waiting"))      # waiting with nothing filed: counts
    clock.advance(20)
    state = sent[-1][0]["aps"]["content-state"]
    # Mike, Rachel (idle, her failure filed) and Nina once each, plus the
    # Janitor item, which names no agent on the chip.
    assert state["needs_you"] == 4
    assert state["decisions"] == 3 + extra
    assert {row["persona"] for row in state["agents"]} == {"Mike", "Rachel", "Nina"}


def test_badge_pages_share_one_read_view_while_a_foreign_writer_commits(monkeypatch):
    from lib import attention_index

    rachel, _ = _agents()
    ids = _badge_pages(rachel, "rachel", 410)
    target = ids[-1]
    original_full_rows = attention_index._full_rows
    committed, observed = [], []

    def full_rows(connection, page_ids):
        rows = original_full_rows(connection, page_ids)
        if target in rows:
            observed.append((rows[target]["status"], rows[target]["payload_json"]))
        if not committed:
            assert target not in page_ids  # the changed row belongs to page three
            writer = sqlite3.connect(str(db.DB_PATH), timeout=1, isolation_level=None)
            try:
                changed = writer.execute("""UPDATE artifacts SET status='cancelled',
                    payload_json='{}',updated_at=updated_at+1 WHERE artifact_id=?""", (target,))
                assert changed.rowcount == 1
                assert writer.execute("SELECT status FROM artifacts WHERE artifact_id=?",
                                      (target,)).fetchone() == ("cancelled",)
                committed.append(target)  # actually committed before the next page
            finally:
                writer.close()
        return rows

    monkeypatch.setattr(attention_index, "_full_rows", full_rows)
    first = live_activity.badge_attention()
    assert committed == [target]
    assert first == {"count": 410, "items": [rachel] * 410, "agent_ids": {rachel}}
    assert observed == [("failed", json.dumps({"content": "x" * 80000}))]
    assert not db.conn().in_transaction
    later = live_activity.badge_attention()
    assert later == {"count": 409, "items": [rachel] * 409, "agent_ids": {rachel}}
    assert not db.conn().in_transaction


@pytest.mark.parametrize("payload", ["null", "1", "true", '["content"]', '"content"',
                                    '{"chart":{"kind":[]}}'])
def test_badge_serializer_failure_preserves_prior_value_and_ends_read(payload, monkeypatch):
    from lib import artifacts

    rachel, _ = _agents()
    row = artifacts.create(session="rachel", type="document", title="Failure", status="failed")
    prior = live_activity.badge_attention()
    assert prior == {"count": 1, "items": [rachel], "agent_ids": {rachel}}
    db.conn().execute("UPDATE artifacts SET payload_json=?,updated_at=updated_at+1 WHERE artifact_id=?",
                      (payload, row["artifact_id"]))
    serialized = []
    released = []
    original_public = artifacts._public
    original_release = artifacts.SummaryBudget.release
    def public(raw):
        serialized.append(raw["payload_json"])
        return original_public(raw)
    def release(budget):
        released.append(budget)
        return original_release(budget)
    monkeypatch.setattr(artifacts, "_public", public)
    monkeypatch.setattr(artifacts.SummaryBudget, "release", release)
    clock = FakeClock()
    pusher = live_activity.LiveActivityPusher(lambda *_: None, clock=clock,
                                             attention=live_activity.badge_attention)
    pusher.set_attention(prior)
    assert pusher._read_attention() == prior  # actual serializer raises into old fallback
    assert serialized == [payload]
    assert len(released) == 1 and not released[0]._scopes
    assert not db.conn().in_transaction
    db.conn().execute("UPDATE artifacts SET payload_json='{}' WHERE artifact_id=?", (row["artifact_id"],))
    assert live_activity.badge_attention() == prior  # failed sequence cannot poison its successor


def test_attention_changes_push_and_raise_priority():
    rachel, _mike = _agents()
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append((payload, priority)),
        clock=clock, schedule=clock.schedule,
        attention=lambda: {"count": 2, "agent_ids": set()})
    pusher.observe(_status(rachel, "rachel", "thinking", turn_started=1))
    assert sent[-1][0]["aps"]["content-state"]["needs_you"] == 2
    clock.advance(3)
    pusher.set_attention({"count": 3, "agent_ids": set()})
    assert sent[-1][0]["aps"]["content-state"]["needs_you"] == 3 and sent[-1][1] == "10"
    clock.advance(3)
    pusher.set_attention({"count": 0, "agent_ids": set()})
    # A drop is structural, not urgent: it waits for the 10 s floor (APNS-N1).
    assert sent[-1][0]["aps"]["content-state"]["needs_you"] == 3
    clock.advance(7)
    assert sent[-1][0]["aps"]["content-state"]["needs_you"] == 0


def _tool_status(agent_id, session, name, label, category=None, started=7):
    return {"type": "live", "agent_id": agent_id, "session": session, "conv": "c", "epoch": "e",
            "lseq": 1, "ops": [{"op": "status", "conv": "c", "activity": {
                "state": "tool", "headline": None, "turn_started_ms": 1,
                "tool": {"name": name, "call_id": "c1", "label": label, "started_at_ms": started,
                         **({"category": category} if category else {})}}}]}


def test_every_row_says_what_the_agent_is_doing():
    rachel, mike = _agents()
    rows = {}
    for state_event, expected in [
        (_tool_status(rachel, "rachel", "Edit", "Stores/AppModel.swift", "edit"), "Editing AppModel.swift"),
        (_tool_status(rachel, "rachel", "Bash", "npm test", "exec"), "Running npm test"),
        (_tool_status(rachel, "rachel", "Read", "src/parser.py"), "Reading parser.py"),
        (_tool_status(rachel, "rachel", "Grep", "tokenize", "search"), "Searching tokenize"),
        (_status(rachel, "rachel", "thinking"), "Thinking"),
        (_status(rachel, "rachel", "responding"), "Responding"),
        (_status(rachel, "rachel", "compacting"), "Compacting"),
        (_status(mike, "mike", "waiting"), "Waiting for you"),
    ]:
        clock, sent = FakeClock(), []
        pusher = live_activity.LiveActivityPusher(
            lambda payload, priority: sent.append(payload), clock=clock, schedule=clock.schedule,
            attention=lambda: {"count": 0, "agent_ids": set()})
        pusher.observe(state_event)
        row = sent[-1]["aps"]["content-state"]["agents"][0]
        assert row["headline"] == expected, (state_event, row)
        rows[expected] = row
    assert rows["Editing AppModel.swift"]["started_at_ms"] == 7


def test_rows_carry_the_agents_display_name():
    shouted = agents_db.create_agent(persona="CLARP-MEMORY-EXPERT", voice_id="v", cwd="/tmp",
                                     session="clarp-memory-expert-51a1")
    plain = agents_db.create_agent(persona="ECIT Rachel", voice_id="v", cwd="/tmp", session="ecit-rachel")
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append(payload), clock=clock, schedule=clock.schedule,
        attention=lambda: {"count": 0, "agent_ids": set()})
    pusher.observe(_status(shouted, "clarp-memory-expert-51a1", "thinking", turn_started=2))
    pusher.observe(_status(plain, "ecit-rachel", "thinking", turn_started=1))
    clock.advance(20)
    names = [row["persona"] for row in sent[-1]["aps"]["content-state"]["agents"]]
    assert names == ["Clarp Memory Expert", "ECIT Rachel"]
    assert live_activity.display_name("clarp-memory-expert") == "Clarp Memory Expert"


# --- helpers roll up into their top-level agent (the app's chip rule) ------------

def _helper(persona, parent, session=None):
    agent_id = agents_db.create_agent(persona=persona, voice_id="v", cwd="/tmp",
                                      session=session or persona.lower())
    agents_db.set_lineage(agent_id, parent_agent_id=parent, role="helper")
    return agent_id


def _pusher(attention=None, roster=agents_db.list_agents):
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append(payload), clock=clock, schedule=clock.schedule,
        attention=attention or (lambda: {"count": 0, "agent_ids": set(), "items": []}),
        roster=roster)
    return pusher, clock, sent


def _faces(state):
    return [row["persona"] for row in state["agents"]]


def test_helpers_never_count_or_show_on_their_own():
    """The lock screen read "7 working" for one agent and its helpers."""
    arnold = agents_db.create_agent(persona="Arnold", voice_id="v", cwd="/tmp", session="arnold")
    mike = agents_db.create_agent(persona="Mike", voice_id="v", cwd="/tmp", session="mike")
    fixer, tester = _helper("Fixer", arnold), _helper("Tester", arnold)
    scout = _helper("Scout", mike)
    pusher, clock, sent = _pusher()
    pusher.observe(_status(arnold, "arnold", "thinking", turn_started=100))
    pusher.observe(_status(fixer, "fixer", "tool", tool="Bash", started=300, turn_started=290))
    pusher.observe(_status(tester, "tester", "thinking", turn_started=280))
    pusher.observe(_status(scout, "scout", "thinking", turn_started=200))   # Mike himself is idle
    clock.advance(20)
    state = sent[-1]["aps"]["content-state"]
    assert (state["working"], state["needs_you"]) == (2, 0)
    assert _faces(state) == ["Mike", "Arnold"]
    assert state["lead"]["persona"] == "Mike"
    assert state["lead"]["headline"] == "1 sub-agent working"
    assert state["lead"]["turn_started_ms"] == 200 and state["lead"]["tool"] is None
    assert state["agents"][1]["headline"] == "Thinking", "a working parent keeps its own line"


def test_nested_helpers_reach_the_top_and_a_wait_counts_once_per_agent():
    arnold = agents_db.create_agent(persona="Arnold", voice_id="v", cwd="/tmp", session="arnold")
    lead = _helper("Lead", arnold)
    deep = _helper("Deep", lead)
    other = _helper("Other", arnold)
    pusher, clock, sent = _pusher(attention=lambda: {
        "count": 2, "agent_ids": {deep}, "items": [deep, deep]})
    pusher.observe(_status(other, "other", "waiting"))
    clock.advance(20)
    state = sent[-1]["aps"]["content-state"]
    assert (state["working"], state["needs_you"]) == (0, 1)
    assert state["decisions"] == 2
    assert _faces(state) == ["Arnold"]
    assert state["agents"][0]["state"] == "waiting"
    assert state["agents"][0]["headline"] == "Waiting for you"


def test_a_busy_parent_with_a_waiting_helper_works_and_needs_you():
    arnold = agents_db.create_agent(persona="Arnold", voice_id="v", cwd="/tmp", session="arnold")
    asker = _helper("Asker", arnold)
    pusher, clock, sent = _pusher()
    pusher.observe(_status(arnold, "arnold", "thinking", turn_started=1))
    pusher.observe(_status(asker, "asker", "waiting"))
    clock.advance(20)
    state = sent[-1]["aps"]["content-state"]
    assert (state["working"], state["needs_you"]) == (1, 1)
    assert _faces(state) == ["Arnold"]


def test_a_helper_whose_parent_is_archived_shows_as_its_own_agent():
    """Its work is never hidden; the app's chip does the same."""
    gone = agents_db.create_agent(persona="Gone", voice_id="v", cwd="/tmp", session="gone")
    orphan = _helper("Orphan", gone)
    agents_db.set_archived(gone, True)
    pusher, clock, sent = _pusher()
    pusher.observe(_status(orphan, "orphan", "thinking", turn_started=1))
    clock.advance(20)
    state = sent[-1]["aps"]["content-state"]
    assert state["working"] == 1 and state["lead"]["persona"] == "Orphan"


def test_a_parent_cycle_keeps_everyone_in_it_top_level():
    roster = {"a": {"session": "a", "persona": "A", "role": "helper", "parent_agent_id": "b"},
              "b": {"session": "b", "persona": "B", "role": "helper", "parent_agent_id": "a"},
              "c": {"session": "c", "persona": "C", "role": "helper", "parent_agent_id": "unknown"},
              "d": {"session": "d", "persona": "D", "role": "agent", "parent_agent_id": "a"}}
    assert live_activity.top_level_owners(roster) == {"a": "a", "b": "b", "c": "c", "d": "d"}
    roster["b"]["parent_agent_id"] = "d"
    roster["c"]["parent_agent_id"] = "a"
    assert live_activity.top_level_owners(roster) == {"a": "d", "b": "d", "c": "d", "d": "d"}, \
        "once the cycle is broken, the chain reaches the agent that is not a helper"


def test_a_parent_named_by_session_and_a_helper_first_seen_live_roll_up_without_a_roster():
    """A helper spawned after the last roster refresh rolls up at once: the
    pusher fetches its parent chain the first time it sees the helper."""
    arnold = agents_db.create_agent(persona="Arnold", voice_id="v", cwd="/tmp", session="arnold")
    lead = _helper("Lead", "arnold")         # parent named by session
    deep = _helper("Deep", lead)
    pusher, clock, sent = _pusher(roster=None)
    pusher.observe(_status(deep, "deep", "thinking", turn_started=5))
    clock.advance(20)
    state = sent[-1]["aps"]["content-state"]
    assert state["working"] == 1 and _faces(state) == ["Arnold"]
    assert state["lead"]["agent_id"] == arnold


def test_badge_items_that_name_no_listed_agent_still_count_each():
    rachel, _mike = _agents()
    state = live_activity.content_state({}, 1, {"count": 3, "items": [rachel, rachel, ""]},
                                        {rachel: agents_db.get_by_agent_id(rachel)})
    assert state["needs_you"] == 2, "Rachel once, the unnamed item once"
    assert _faces(state) == ["Rachel"]
    old_shape = live_activity.content_state({}, 1, {"count": 3, "agent_ids": {rachel}},
                                            {rachel: agents_db.get_by_agent_id(rachel)})
    assert old_shape["needs_you"] == 3
