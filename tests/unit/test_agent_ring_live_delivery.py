"""H6: a Live ring is never announced by the global classic alert, in any state.

The alert push reaches every phone, also one that cannot run Live (a published 2818 phone shows any
ring it hears about). So a Live ring is announced by VoIP to Live-capable phones only, for its first
ring and for every later state; a classic ring keeps its alert for every state, unchanged.
"""
from __future__ import annotations

import pytest

from lib import agent_rings as rings
from tests.unit.test_agent_ring_live import _live_ring, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_rings import PHONE, _ring, env  # noqa: F401 - env is a fixture

KINDS = ("ring", "answered", "declined", "missed", "cancelled")


def _pushes(monkeypatch, voip_sent):
    alerts, voips = [], []
    monkeypatch.setattr(rings, "_push", lambda ring, kind: alerts.append(kind) or {"sent": 1})
    monkeypatch.setattr(rings, "_push_voip", lambda ring: voips.append(ring["ring_id"]) or {"sent": voip_sent})
    return alerts, voips


@pytest.mark.parametrize("voip_sent", [0, 1])
@pytest.mark.parametrize("kind", KINDS)
def test_no_state_of_a_live_ring_sends_the_global_alert(sander, monkeypatch, kind, voip_sent):
    alerts, _ = _pushes(monkeypatch, voip_sent)
    live = _live_ring()["ring"]
    alerts.clear()
    record = next(r for r in rings._load() if r["ring_id"] == live["ring_id"])
    rings._announce(record, kind)
    assert alerts == []


@pytest.mark.parametrize("kind", KINDS)
def test_every_state_of_a_classic_ring_keeps_its_alert(sander, monkeypatch, kind):
    alerts, _ = _pushes(monkeypatch, 0)
    classic = _ring()["ring"]
    alerts.clear()
    record = next(r for r in rings._load() if r["ring_id"] == classic["ring_id"])
    rings._announce(record, kind)
    assert alerts == [kind]


def test_a_classic_ring_that_went_out_as_voip_skips_only_its_duplicate_ring_alert(sander, monkeypatch):
    alerts, voips = _pushes(monkeypatch, 1)
    classic = _ring()["ring"]
    rings.decline(PHONE, classic["ring_id"])
    assert voips == [classic["ring_id"]] and alerts == ["declined"]


def test_a_live_rings_real_lifecycle_sends_no_alert(sander, monkeypatch):
    alerts, voips = _pushes(monkeypatch, 0)          # even a VoIP push that reached nobody falls back to nothing
    live = _live_ring()["ring"]
    rings.decline(PHONE, live["ring_id"])
    assert voips == [live["ring_id"]] and alerts == []


def test_a_mode_that_is_not_live_is_not_treated_as_live(sander, monkeypatch):
    alerts, _ = _pushes(monkeypatch, 0)
    classic = _ring()["ring"]
    record = next(r for r in rings._load() if r["ring_id"] == classic["ring_id"])
    alerts.clear()
    rings._announce({**record, "conversation_mode": "classic"}, "missed")
    assert alerts == ["missed"]
