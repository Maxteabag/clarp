"""ActivityKit Live Activity pushes (docs/live-items.md §8): a phone registers
its activity's push token, and the Host keeps the activity current with the
fleet's aggregate status: how many agents work, how many need the owner, and
the lead agent with its current tool and start time. Updates are throttled
to Apple's budget, except when something needs the owner."""
from __future__ import annotations

from lib import agents as agents_db
from lib import live_activity


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
    for headline in ("Running a", "Running b", "Running c"):
        clock.advance(1)
        pusher.observe(_status(rachel, "rachel", "tool", tool="Bash", started=2, headline=headline))
    assert len(sent) == 1                       # tool churn waits for the window
    clock.advance(15)
    assert len(sent) == 2 and sent[-1][0]["aps"]["content-state"]["lead"]["headline"] == "Running c"
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


def test_pending_decisions_count_as_needing_the_owner():
    rachel, _mike = _agents()
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append((payload, priority)),
        clock=clock, schedule=clock.schedule, pending_decisions=lambda: 2)
    pusher.observe(_status(rachel, "rachel", "thinking", turn_started=1))
    state = sent[-1][0]["aps"]["content-state"]
    assert (state["needs_you"], state["decisions"]) == (2, 2)
    clock.advance(3)
    pusher.set_pending_decisions(3)
    assert sent[-1][0]["aps"]["content-state"]["needs_you"] == 3 and sent[-1][1] == "10"
    clock.advance(3)
    pusher.set_pending_decisions(0)
    assert sent[-1][0]["aps"]["content-state"]["needs_you"] == 0
