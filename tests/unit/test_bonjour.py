from __future__ import annotations

import sys
import types

from lib.bonjour import BonjourAdvertiser


def test_bonjour_registers_and_unregisters_clarp_service(monkeypatch):
    calls = []

    class Info:
        def __init__(self, service_type, name, **kwargs):
            self.type = service_type
            self.name = name
            self.kwargs = kwargs

    class Zeroconf:
        def register_service(self, info, **_kwargs): calls.append(("register", info))
        def unregister_service(self, info): calls.append(("unregister", info))
        def close(self): calls.append(("close", None))

    monkeypatch.setitem(
        sys.modules, "zeroconf",
        types.SimpleNamespace(ServiceInfo=Info, Zeroconf=Zeroconf))
    monkeypatch.setattr(
        BonjourAdvertiser, "_addresses", staticmethod(lambda: [b"\x0a\x00\x00\x05"]))
    advertiser = BonjourAdvertiser(
        name="Friend's Mac", server_id="server-1", port=7682,
        auth_required=True)
    assert advertiser.start() is True
    info = calls[0][1]
    assert info.type == "_clarp._tcp.local."
    assert info.kwargs["port"] == 7682
    assert info.kwargs["properties"][b"auth"] == b"required"
    advertiser.stop()
    assert [call[0] for call in calls] == ["register", "unregister", "close"]
