"""`live_items` is advertised only while live events actually flow
(docs/live-items.md): a Host whose runtime has no live hub (an older runtime
release still running after the HTTP server was updated) must not tell
clients to wait for live events, or working agents look idle."""
from __future__ import annotations

import threading
import time

from lib import server_identity
from lib.live_hub import LiveFanout, LiveHub, LiveRelay
from lib.runtime_bridge import RuntimeRPCServer

LIVE_FEATURES = {"live_items", "live_activity_push"}


def _advertised():
    features = set(server_identity.capabilities()["features"])
    contract = set(server_identity.contract()["features"])
    return features & LIVE_FEATURES, contract & LIVE_FEATURES


def test_live_features_follow_the_probe_on_every_read():
    serving = {"now": False}
    server_identity.set_live_probe(lambda: serving["now"])
    try:
        assert _advertised() == (set(), set())
        serving["now"] = True
        assert _advertised() == (LIVE_FEATURES, LIVE_FEATURES)
        serving["now"] = False
        assert _advertised() == (set(), set())
    finally:
        server_identity.set_live_probe(None)
    # No probe registered (nothing in this process serves live events).
    assert _advertised() == (set(), set())
    # Everything else is still advertised.
    assert "tool_explanation_setting" in server_identity.capabilities()["features"]


class _Dispatch:
    pass


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def test_a_relay_serves_only_while_its_runtime_has_a_live_hub(tmp_path):
    from lib.audio_stream import AudioStream

    old = RuntimeRPCServer(tmp_path / "old.sock", dispatch_service=_Dispatch(), status_provider=lambda: {})
    threading.Thread(target=old.serve_forever, daemon=True).start()
    relay = LiveRelay(tmp_path / "old.sock", AudioStream(tmp_path / "a1")).start()
    try:
        time.sleep(0.5)
        assert relay.serving is False          # an older runtime: no hub, no live events
    finally:
        relay.stop()
        old.shutdown()
        old.server_close()

    fanout = LiveFanout()
    new = RuntimeRPCServer(tmp_path / "new.sock", dispatch_service=_Dispatch(), status_provider=lambda: {})
    new.live_fanout = fanout
    new.live_hub = LiveHub(sink=fanout.publish)
    threading.Thread(target=new.serve_forever, daemon=True).start()
    relay = LiveRelay(tmp_path / "new.sock", AudioStream(tmp_path / "a2")).start()
    try:
        assert _wait(lambda: relay.serving is True)
        new.shutdown()
        new.server_close()
        assert _wait(lambda: relay.serving is False)   # the runtime went away
    finally:
        relay.stop()
