"""GPT-Live-1.1 (issue #473): one configured model per Host, chosen per client on an
agent Live call, standby only on the Oracle's WebRTC create, and standby usage.

Every case runs on stand-ins: nothing here reaches OpenAI.
"""
from __future__ import annotations

import pytest

from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from lib import agent_rings as rings
from lib import config, oracle_live, oracle_live_stable, oracle_realtime
from lib.oracle_live_usage import LiveUsage
from lib.oracle_live_wire import (BASELINE_LIVE_MODEL, DEFAULT_LIVE_MODEL, LIVE_MODELS, LiveWire, live_model,
                                  oracle_wire)
from tests.unit.test_agent_ring_live import PHONE, _context, _live_ring, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_ring_live_socket import (Handler, Upstream, _join, _live_lifecycle, _serve,  # noqa: F401
                                                    _types, _wait)
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture

STANDBY = ("wake_policy", "idle_timeout_s")


# ---- the configuration ----------------------------------------------------------------------------

def test_the_documented_models_are_the_allowlist_and_the_default_is_1_1():
    assert LIVE_MODELS == ("gpt-live-1", "gpt-live-1.1")
    assert (DEFAULT_LIVE_MODEL, BASELINE_LIVE_MODEL) == ("gpt-live-1.1", "gpt-live-1")
    assert config.Config().live_model == "gpt-live-1.1" and config.Config().live_idle_timeout_s == 0


@pytest.mark.parametrize("raw, expected", [
    (None, "gpt-live-1.1"), ("gpt-live-1", "gpt-live-1"), ("gpt-live-1.1", "gpt-live-1.1"),
    (" gpt-live-1 ", "gpt-live-1"),
])
def test_an_allowed_model_is_kept(raw, expected, monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: lines.append(a))
    assert config._live_model(raw) == expected and lines == []


@pytest.mark.parametrize("raw", ["gpt-live-2", "gpt-live-1-codex", "gpt-realtime-2.1", "", 1.1, ["gpt-live-1"]])
def test_any_other_model_is_refused_with_one_log_line(raw, monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: lines.append(a))
    assert config._live_model(raw) == DEFAULT_LIVE_MODEL
    assert len(lines) == 1 and lines[0][0] == "config.live_model"


@pytest.mark.parametrize("raw, expected", [(None, 0), (0, 0), (5, 5), (20, 20), (60, 60),
                                           (4, 0), (61, 0), (20.0, 0), (True, 0), ("20", 0), (-5, 0)])
def test_the_idle_timeout_is_off_or_5_to_60_whole_seconds(raw, expected, monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: lines.append(a))
    assert config._live_idle_timeout(raw) == expected
    assert len(lines) == (0 if raw in (None, 0, 5, 20, 60) and type(raw) is not float and raw is not True else 1)


def test_the_per_client_minimum_keeps_a_platform_and_a_contract_or_0_for_never(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: lines.append(a))
    assert config._live_model_min_client({"ios": 75, "android": 30}) == {"ios": 75, "android": 30} and not lines
    assert config._live_model_min_client({"ios": 0, "android": 31}) == {"ios": 0, "android": 31} and not lines
    assert config._live_model_min_client({"ios": -1, "android": True, "web": 1.0, "desktop": "4", "Android": 3,
                                          "watch": 29}) == {"watch": 29}
    assert len(lines) == 1
    for refused in ("'ios'", "'android'", "'web'", "'desktop'", "'Android'"):
        assert refused in lines[0][1]
    assert config._live_model_min_client("ios=75") == {} and len(lines) == 2


@pytest.mark.parametrize("header, expected", [
    ("desktop/1 contract=5", "gpt-live-1.1"),     # a name in the table and not in the constants: its number
    ("desktop/1 contract=4", "gpt-live-1"),       # below that number
    ("watch/1 contract=999", "gpt-live-1"),       # a name in neither: never
    ("tv/1 contract=999", "gpt-live-1"),          # 0 in the table: never
    ("ios/2986 contract=75", "gpt-live-1.1"),     # the constants still stand beside the table
])
def test_the_table_may_name_any_client(tmp_path, header, expected):
    path = tmp_path / "config.toml"
    path.write_text('[openai]\nlive_model = "gpt-live-1.1"\n[openai.live_model_min_client]\ndesktop = 5\ntv = 0\n')
    cfg = config.load(path)
    assert cfg.live_model_min_client == {"desktop": 5, "tv": 0}
    assert live.model_for_client(header, cfg) == expected


@pytest.mark.parametrize("header", ["ios/2986 contract=75", "ios/9999 contract=999999999"])
def test_0_in_the_table_switches_that_client_off_and_no_other(tmp_path, header):
    path = tmp_path / "config.toml"
    path.write_text('[openai]\nlive_model = "gpt-live-1.1"\n[openai.live_model_min_client]\nios = 0\n')
    cfg = config.load(path)
    assert cfg.live_model_min_client == {"ios": 0}
    assert live.model_for_client(header, cfg) == "gpt-live-1"
    assert live.model_for_client("android/993 contract=29", cfg) == "gpt-live-1.1"


def test_the_file_setting_reaches_the_one_reader(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[openai]\nlive_model = "gpt-live-1"\nlive_idle_timeout_s = 20\n'
                    '[openai.live_model_min_client]\nandroid = 30\n')
    cfg = config.load(path)
    assert (cfg.live_model, cfg.live_idle_timeout_s, cfg.live_model_min_client) == ("gpt-live-1", 20, {"android": 30})
    assert live_model(cfg) == "gpt-live-1"


# ---- standby: the WebRTC create in API mode only -------------------------------------------------------------

def test_standby_fields_are_absent_at_zero_everywhere():
    for webrtc in (True, False):
        session = LiveWire("api", model="gpt-live-1.1").session("x", webrtc=webrtc)
        assert not set(STANDBY) & set(session)


def test_standby_fields_at_20_are_sent_only_on_the_webrtc_create():
    wire = LiveWire("api", model="gpt-live-1.1", idle_timeout_s=20)
    webrtc = wire.session("x", webrtc=True)
    assert webrtc["wake_policy"] == {"type": "speech"} and webrtc["idle_timeout_s"] == 20
    assert not set(STANDBY) & set(wire.session("x", webrtc=False))      # the Host-held WebSocket: never


def test_the_oracle_wire_asks_for_standby_only_for_the_webrtc_create_in_api_mode():
    cfg = config.Config(openai_api_key="k", live_idle_timeout_s=20)
    assert oracle_wire(cfg, standby=True).session("x", webrtc=True)["idle_timeout_s"] == 20
    assert not set(STANDBY) & set(oracle_wire(cfg).session("x", webrtc=True))   # the capability's wire
    sub = oracle_wire(config.Config(oracle_voice_backend="subscription", live_idle_timeout_s=20), standby=True)
    assert sub.model == "gpt-live-1-codex" and not set(STANDBY) & set(sub.session("x", webrtc=True))
    off = config.Config(openai_api_key="k")
    assert not set(STANDBY) & set(oracle_wire(off, standby=True).session("x", webrtc=True))


def test_an_impossible_wire_is_refused():
    with pytest.raises(ValueError):
        LiveWire("api", model="gpt-live-2")
    with pytest.raises(ValueError):
        LiveWire("api", model="gpt-live-1.1", idle_timeout_s=61)


# ---- the Oracle: what the Host advertises is what the session runs ------------------------------------------

@pytest.mark.parametrize("model", LIVE_MODELS)
def test_the_advertised_oracle_model_is_the_model_every_oracle_path_runs(model, monkeypatch):
    cfg = config.Config(openai_api_key="k", live_model=model)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    from lib import oracle_live_provider
    monkeypatch.setattr(oracle_live_provider, "available", lambda *a: True)
    advertised = oracle_realtime.capability()
    assert advertised["v2"]["model"] == oracle_live_stable.live_config()["model"] == model           # stable
    assert advertised["v2_tinkered"]["model"] == oracle_live.live_config()["model"] == model          # WebSocket
    created = oracle_wire(cfg, standby=True)                                                          # WebRTC
    assert advertised["v2_tinkered"]["model"] == created.model == \
        oracle_live.live_config(wire=created, webrtc=True)["model"]
    assert advertised["v2"]["voice"] == advertised["v2_tinkered"]["voice"] == created.voice == "marin"


# ---- agent rings: the model is chosen for the answering client -----------------------------------------------

CASES = [  # header, configured minimum override, the model the call runs on
    ("ios/2985 contract=74", {}, "gpt-live-1"),            # iOS below its minimum
    ("ios/2986 contract=75", {}, "gpt-live-1.1"),          # iOS at it
    ("ios/3001 contract=80", {}, "gpt-live-1.1"),          # and above
    ("android/992 contract=28", {}, "gpt-live-1"),         # Android below its minimum
    ("android/993 contract=29", {}, "gpt-live-1.1"),       # Android at it (0.1.0, build 993)
    ("android/992 contract=28", {"android": 28}, "gpt-live-1.1"),   # the configuration lowers a platform's number
    ("ios/2986 contract=75", {"ios": 76}, "gpt-live-1"),   # or raises it
    ("desktop/9 contract=999", {}, "gpt-live-1"),          # another client name
    ("IOS/2986 contract=75", {}, "gpt-live-1"),            # the name is exact
    (None, {}, "gpt-live-1"),                              # no client name at all
    ("ios/2986", {}, "gpt-live-1"),                        # no contract
    ("ios/2986 contract=75 ", {}, "gpt-live-1"),           # a trailing blank names no client here (the Rust
    ("android/993 contract=29 ", {}, "gpt-live-1"),        # Host's door strips it: a registered difference)
]


def test_the_first_builds_that_accept_both_names():
    assert live.AGENT_LIVE_MODEL_MIN_CLIENT == {"ios": 75, "android": 29}


# The vectors both Hosts agreed (Arnold, 10:24): the Rust Host answers each of these the same. Word for word.
AGREED = [
    ("iOS/2985 contract=75", "gpt-live-1"),
    ("Android/1 contract=29", "gpt-live-1"),
    ("ios/2985 contract=075", "gpt-live-1.1"),            # read as 75
    ("ios/2985 contract=74", "gpt-live-1"),
    ("ios/2985  contract=75", "gpt-live-1"),              # two spaces
    ("ios/2985\tcontract=75", "gpt-live-1"),              # a tab for the space
    ("ios/2985 contract=75 ", "gpt-live-1"),              # a trailing blank
    ("ios/2985 contract=75\n", "gpt-live-1"),             # a trailing newline
    ("ios/2985 contract=75 beta", "gpt-live-1"),          # anything after the digits
    ("i" * 33 + "/2985 contract=75", "gpt-live-1"),       # a name of 33
    ("ios/" + "2" * 81 + " contract=75", "gpt-live-1"),   # a build of 81
    ("ios/2985 contract=1234567890", "gpt-live-1"),       # ten digits
    (None, "gpt-live-1"),                                 # no header
]


@pytest.mark.parametrize("header, expected", AGREED)
def test_the_vectors_both_hosts_agreed(header, expected):
    cfg = config.Config(openai_api_key="k", live_model="gpt-live-1.1", live_model_min_client={})
    assert live.model_for_client(header, cfg) == expected


@pytest.mark.parametrize("header", [h for h, _e in AGREED])
def test_the_agreed_vectors_on_a_host_configured_to_gpt_live_1(header):
    cfg = config.Config(openai_api_key="k", live_model="gpt-live-1", live_model_min_client={})
    assert live.model_for_client(header, cfg) == "gpt-live-1"


STRICT = [  # each must name no client (or a client below its minimum): the strict form, the name as sent
    "iOS/2985 contract=75", "IOS/2986 contract=75", "Android/1 contract=29", "ANDROID/993 contract=29",
    "ios/2986  contract=75", "ios/2986 contract=75 ", "ios/2986\tcontract=75", "ios/2986 contract=75\n",
    "ios/2986 contract=75 x", " ios/2986 contract=75", "i" * 33 + "/1 contract=75", "ios/" + "1" * 81 + " contract=75",
    "ios/2986 contract=0000000075", "ios/2986 contract=", "ios/ contract=75", "ios/2986 contract=-75",
    "ios/2986 contract=7.5e1", "ios/2986 contract=٧٥",
]


@pytest.mark.parametrize("header", STRICT)
def test_anything_but_the_strict_form_with_the_name_as_sent_runs_gpt_live_1(header):
    cfg = config.Config(openai_api_key="k", live_model="gpt-live-1.1", live_model_min_client={})
    assert live.model_for_client(header, cfg) == "gpt-live-1"


@pytest.mark.parametrize("header", ["ios/2986 contract=075", "ios/2986 contract=000000075", "ios/" + "1" * 80 + " contract=75",
                                    "a" * 32 + "/1 contract=1", "android/993 contract=999999999"])
def test_the_strict_form_at_its_limits_is_read(header):
    cfg = config.Config(openai_api_key="k", live_model="gpt-live-1.1", live_model_min_client={"a" * 32: 1})
    assert live.model_for_client(header, cfg) == "gpt-live-1.1"


@pytest.mark.parametrize("header, minimum, expected", CASES)
def test_the_model_for_a_client(header, minimum, expected):
    cfg = config.Config(openai_api_key="k", live_model="gpt-live-1.1", live_model_min_client=minimum)
    assert live.model_for_client(header, cfg) == expected


@pytest.mark.parametrize("header", [h for h, _m, _e in CASES])
def test_a_host_configured_back_to_gpt_live_1_runs_it_for_everyone(header):
    cfg = config.Config(openai_api_key="k", live_model="gpt-live-1", live_model_min_client={"desktop": 1})
    assert live.model_for_client(header, cfg) == "gpt-live-1"


def _reset():
    with oracle_realtime._ACTIVE_LOCK:
        oracle_realtime._ACTIVE_PRINCIPALS.clear()
    with oracle_live_stable._CLOSING_CONDITION:
        oracle_live_stable._CLOSING.clear()
        oracle_live_stable._STOP_HOOKS.clear()
    live.reset_for_tests()


@pytest.mark.parametrize("header, minimum, expected", CASES)
def test_grant_session_frame_and_upstream_name_the_one_model_that_runs(sander, monkeypatch, _live_lifecycle,
                                                                       header, minimum, expected):
    _reset()
    cfg = config.Config(openai_api_key="sk-test", live_model="gpt-live-1.1", live_model_min_client=minimum)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    ring = _live_ring()["ring"]
    grant = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1",
                         client_nonce="n-r1", client=header)["live"]
    # The owner's repeat (whatever it now sends) gets the same grant: the model was fixed with it.
    assert rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1",
                        client_nonce="n-r1", client="ios/9999 contract=999")["live"] == grant
    up = Upstream()
    monkeypatch.setattr(sock, "_connect", lambda key: up)
    monkeypatch.setattr(live, "device_ok", lambda principal: True)
    # A configuration change after the grant changes nothing for this call.
    monkeypatch.setattr(config, "load", lambda *a, **k: config.Config(openai_api_key="sk-test", live_model="gpt-live-1"
                                                                      if expected == "gpt-live-1.1" else "gpt-live-1.1",
                                                                      live_model_min_client={"ios": 1, "android": 1,
                                                                                             "desktop": 1}))
    h = Handler()
    _serve(h)
    h.send(_join(grant))
    _wait(lambda: "agent_live.session" in _types(h) and up.of("session.start"))
    frame = next(f for f in h.wfile.frames() if f["type"] == "agent_live.session")
    [start] = up.of("session.start")
    assert grant["model"] == frame["model"] == start["session"]["model"] == expected
    assert not set(STANDBY) & set(start["session"])                   # never standby on an agent ring
    assert (grant["voice"], frame["voice"], start["session"]["audio"]["output"]["voice"]) == ("marin",) * 3
    assert _live_lifecycle() == []
    _reset()


def test_the_grant_for_1_1_differs_from_the_grant_for_1_in_the_model_only(sander, monkeypatch):
    _reset()
    monkeypatch.setattr(config, "load", lambda *a, **k: config.Config(openai_api_key="k", live_model="gpt-live-1.1"))
    monkeypatch.setattr(rings, "AGENT_INTERVAL_MS", 0)      # two rings from one agent in one test
    grants = []
    for header, request in (("ios/2985 contract=74", "r1"), ("ios/2986 contract=75", "r2")):
        event = {**_context()["event"], "id": "lw-2026-10-17-" + request}       # two rings, two events
        ring = _live_ring(context=_context(event=event))["ring"]
        grants.append(rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id=request,
                                   client_nonce="n-" + request, client=header)["live"])
    old, new = grants
    assert (old["model"], new["model"]) == ("gpt-live-1", "gpt-live-1.1")
    per_call = {"ring_id", "call_id", "attempt_id", "owner_nonce", "join_ticket", "expires_at", "context_digest"}
    assert {k: v for k, v in old.items() if k not in per_call | {"model"}} == \
        {k: v for k, v in new.items() if k not in per_call | {"model"}}
    assert (new["provider"], new["voice"], new["opening_owner"]) == ("openai", "marin", "live")
    _reset()


def _as_stored_before_the_upgrade(ring_id, *, answered):
    """Make a ring look as the previous Host saved it: its snapshot names the model and its stored digest is that
    snapshot's; answered before the upgrade, it also has no chosen model."""
    from lib.agent_rings import _lock
    with _lock:
        records = rings._load()
        ring = next(r for r in records if r["ring_id"] == ring_id)
        ring["_live_snapshot"] = {**ring["_live_snapshot"], "model": "gpt-live-1"}
        ring["_live_context_digest"] = live.digest(ring["_live_snapshot"])
        if answered:
            ring.pop("_live_model", None)
        rings._save(records)
        return ring["_live_context_digest"]


def _run(grant, monkeypatch):
    up = Upstream()
    monkeypatch.setattr(sock, "_connect", lambda key: up)
    monkeypatch.setattr(live, "device_ok", lambda principal: True)
    h = Handler()
    _serve(h)
    h.send(_join(grant))
    _wait(lambda: "agent_live.session" in _types(h) and up.of("session.start"))
    frame = next(f for f in h.wfile.frames() if f["type"] == "agent_live.session")
    [start] = up.of("session.start")
    return frame, start


@pytest.mark.parametrize("answered_before, header, expected", [
    (True, "ios/2986 contract=75", "gpt-live-1"),      # answered before the upgrade: the baseline, whoever joins
    (False, "ios/2986 contract=75", "gpt-live-1.1"),   # ringing at the upgrade, answered after: the client's choice
    (False, "ios/2985 contract=74", "gpt-live-1"),
])
def test_a_stored_ring_keeps_its_snapshot_and_digest_and_runs_on_what_model_of_gives(
        sander, monkeypatch, _live_lifecycle, answered_before, header, expected):
    _reset()
    monkeypatch.setattr(config, "load", lambda *a, **k: config.Config(openai_api_key="sk-test", live_model="gpt-live-1.1"))
    ring_id = _live_ring()["ring"]["ring_id"]
    if answered_before:
        rings.answer(PHONE, ring_id, conversation_mode="agent_live_v1", request_id="r1", client_nonce="n-r1", client=header)
    stored = _as_stored_before_the_upgrade(ring_id, answered=answered_before)
    grant = rings.answer(PHONE, ring_id, conversation_mode="agent_live_v1", request_id="r1", client_nonce="n-r1",
                         client=header)["live"]
    frame, start = _run(grant, monkeypatch)
    ring = next(r for r in rings._load() if r["ring_id"] == ring_id)
    assert grant["context_digest"] == frame["context_digest"] == ring["_live_context_digest"] == stored
    assert ring["_live_snapshot"]["model"] == "gpt-live-1"           # the stored snapshot is never rewritten
    assert grant["model"] == frame["model"] == start["session"]["model"] == live.model_of(ring) == expected
    assert _live_lifecycle() == []
    _reset()


def test_nothing_reads_a_model_from_the_snapshot():
    import pathlib
    import re
    lib = pathlib.Path(live.__file__).parent
    readers = [f"{path.name}:{n}" for path in lib.rglob("*.py") for n, line in enumerate(path.read_text().splitlines(), 1)
               if re.search(r"snapshot[^\n]*\[\s*[\"']model[\"']\s*\]|snapshot[^\n]*\.get\(\s*[\"']model", line)]
    assert readers == []


def test_a_ring_saved_before_the_model_was_chosen_runs_on_the_baseline():
    assert live.model_of({"_live_snapshot": {"model": "gpt-live-1"}}) == "gpt-live-1"
    assert live.model_of({"_live_model": "gpt-live-1.1"}) == "gpt-live-1.1"
    assert live.model_of({"_live_model": "gpt-live-9"}) == "gpt-live-1"


# ---- standby events and usage ------------------------------------------------------------------------------------

def test_standby_is_relayed_as_one_oracle_event():
    assert oracle_live.standby_event("session.paused") == {"type": "oracle_v2.standby", "active": True}
    assert oracle_live.standby_event("session.resumed") == {"type": "oracle_v2.standby", "active": False}


def test_a_conversation_relays_paused_and_resumed(monkeypatch):
    down = []

    class Tools:
        def results(self):
            return []
    c = oracle_live.Conversation(type("Up", (), {"send": lambda self, raw: None})(), down.append, Tools(), "k",
                                 wire=LiveWire("api", model="gpt-live-1.1"))
    try:
        c.receive({"type": "session.paused", "event_id": "e1"})
        c.receive({"type": "session.resumed", "event_id": "e2"})
    finally:
        c.stop.set(); c.pool.shutdown()
    assert [e for e in down if e["type"] == "oracle_v2.standby"] == [
        {"type": "oracle_v2.standby", "active": True}, {"type": "oracle_v2.standby", "active": False}]


def test_usage_prices_active_and_standby_time_at_their_rates():
    usage = LiveUsage("api")
    usage.observe({"type": "session.usage.updated", "usage": {"seconds": 90, "active_seconds": 60, "paused_seconds": 30}})
    snap = usage.snapshot()
    assert (snap["active_seconds"], snap["paused_seconds"]) == (60, 30)
    assert snap["voice_estimate_usd"] == round(.05 + .01, 6)      # one active minute, half a standby minute
    # A later snapshot without the breakdown replaces it: all time at the active rate, as before.
    usage.observe({"type": "session.usage.updated", "usage": {"seconds": 120}})
    snap = usage.snapshot()
    assert (snap["active_seconds"], snap["paused_seconds"], snap["voice_estimate_usd"]) == (None, None, .1)


def test_usage_without_a_breakdown_is_priced_as_before():
    usage = LiveUsage("api")
    usage.observe({"type": "session.usage.updated", "usage": {"seconds": 12, "active_seconds": 12}})
    assert usage.snapshot()["voice_estimate_usd"] == round(12 * .05 / 60, 6)
    assert LiveUsage("subscription").snapshot()["voice_estimate_usd"] is None
