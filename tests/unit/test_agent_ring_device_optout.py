"""A phone can switch off agent rings for itself ("Ring denne telefonen" off).

Peter, 18 Oct: the phone that films the demo must not ring while the other one does. A phone that
switched rings off gets no agent ring of any kind, classic or Live: no VoIP, no ring alert push, no
agent-ring SSE event and nothing from GET /calls/ring. Every other phone is unchanged, and with no
phone switched off delivery is exactly as before.

Ring alerts are excluded by alert-token OWNER: POST /devices binds a token to the authenticated device
that registers it. Legacy unbound tokens keep the state "partial" until no live unbound token remains.
"""
from __future__ import annotations

import pytest

from lib import agent_ring_live as live
from lib import agent_rings as rings
from lib import apns, db
from lib.group_calls import CallError
from tests.unit.test_agent_ring_live import OTHER_PHONE, _live_ring, _pair, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_rings import PHONE, _ring, env  # noqa: F401 - env is a fixture

# The env fixture replaces both senders; these tests need the real token selection under them.
_REAL_PUSH, _REAL_PUSH_VOIP = rings._push, rings._push_voip

T_PHONE, T_PHONE_2, T_OTHER, T_LEGACY = "a1" * 32, "a2" * 32, "b2" * 32, "c3" * 32


@pytest.fixture
def senders(sander, monkeypatch):
    """Both phones registered for VoIP (Live-capable); capture what each sender is asked to reach."""
    rings.register_voip_token(OTHER_PHONE, "cd" * 32, supported_conversation_modes=["classic", "agent_live_v1"])
    sent = {"voip": [], "alert": []}

    def voip(ring, tokens):
        sent["voip"].append(sorted(t["principal"] for t in tokens))
        return {"sent": len(tokens)}

    def alert(ring, kind, exclude_owners=None):
        sent["alert"].append((kind, sorted(t["token"] for t in apns.ring_alert_tokens(exclude_owners))))
        return {"sent": 1}
    monkeypatch.setattr(rings, "_push", _REAL_PUSH)
    monkeypatch.setattr(rings, "_push_voip", _REAL_PUSH_VOIP)
    monkeypatch.setattr(apns, "send_agent_ring_voip", voip)
    monkeypatch.setattr(apns, "send_agent_ring", alert)
    return sent


def _event(**extra):
    return {"type": "agent-ring", "ring_id": "rng_x", "state": "ringing", **extra}


def _rows():
    return {r["token"]: dict(r) for r in db.conn().execute("SELECT token, principal AS owner, disabled_at FROM device_tokens")}


# ---- the switch --------------------------------------------------------------------------

def test_every_phone_receives_rings_by_default(sander):
    assert rings.receives_rings(PHONE) and rings.receives_rings(OTHER_PHONE)
    assert rings.device_state(PHONE) == {"ok": True, "receive_rings": True, "alerts_suppressed": False, "state": "on"}


def test_a_phone_that_switched_rings_off_gets_no_voip_for_a_classic_or_a_live_ring(senders):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    classic = _ring()["ring"]
    rings.decline(OTHER_PHONE, classic["ring_id"])          # one ring at a time
    _live_ring()
    assert senders["voip"] == [[OTHER_PHONE], [OTHER_PHONE]]


def test_with_no_phone_switched_off_every_alert_and_voip_goes_out_as_before(senders):
    apns.register_token(T_PHONE, owner=PHONE)
    apns.register_token(T_LEGACY, session="s")
    _ring()
    assert senders["voip"] == [sorted([PHONE, OTHER_PHONE])]
    assert senders["alert"] == [] or all(tokens == sorted([T_PHONE, T_LEGACY]) for _, tokens in senders["alert"])


def test_a_phone_that_switched_rings_off_gets_no_ring_alert_of_any_kind_on_its_owned_tokens(senders):
    apns.register_token(T_PHONE, owner=PHONE)
    apns.register_token(T_OTHER, owner=OTHER_PHONE)
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    classic = _ring()["ring"]
    rings.decline(OTHER_PHONE, classic["ring_id"])
    assert senders["alert"] and all(tokens == [T_OTHER] for _, tokens in senders["alert"])


def test_a_phone_that_switched_rings_off_sees_no_ring_event_and_no_ring_in_the_poll(sander):
    classic = _ring()["ring"]
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert not rings.event_visible(_event(), PHONE)
    assert not rings.event_visible(_event(conversation_mode="agent_live_v1"), PHONE)
    assert not rings.event_visible(classic, PHONE)               # a public record (GET ?ring_id) too
    assert rings.current(PHONE) is None
    assert rings.event_visible(_event(), OTHER_PHONE) and rings.current(OTHER_PHONE)["ring_id"] == classic["ring_id"]
    assert rings.event_visible({"type": "transcript-updated"}, PHONE)   # only agent rings are held back


def test_switching_rings_on_again_restores_every_path(senders):
    apns.register_token(T_PHONE, owner=PHONE)
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert rings.set_receive_rings(PHONE, {"receive_rings": True})["state"] == "on"
    _ring()
    assert senders["voip"] == [sorted([PHONE, OTHER_PHONE])]
    assert senders["alert"] == [] or all(T_PHONE in tokens for _, tokens in senders["alert"])
    assert rings.event_visible(_event(), PHONE)


def test_only_a_paired_full_scope_device_switches_its_own_rings(sander, monkeypatch):
    for principal in ("administrator", "", "device_unpaired"):
        with pytest.raises(CallError) as exc:
            rings.set_receive_rings(principal, {"receive_rings": False})
        assert (exc.value.status, exc.value.body()["error"]) == (403, "ring_device_required")
    monkeypatch.setattr(live, "device_ok", lambda principal: False)      # revoked or limited
    with pytest.raises(CallError) as exc:
        rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert exc.value.status == 403
    assert rings.receives_rings(PHONE)


@pytest.mark.parametrize("body", [
    {}, {"receive_rings": 0}, {"receive_rings": "false"}, {"receive_rings": None},
    {"receive_rings": False, "push_token": T_PHONE},             # the self-asserted token is gone
    {"receive_rings": False, "principal": OTHER_PHONE},          # no field names another phone
])
def test_a_malformed_request_changes_nothing(sander, body):
    with pytest.raises(CallError) as exc:
        rings.set_receive_rings(PHONE, body)
    assert (exc.value.status, exc.value.body()["error"]) == (400, "ring_device_invalid")
    assert rings.receives_rings(PHONE) and rings.receives_rings(OTHER_PHONE)


def test_one_phone_switching_off_never_touches_another(sander):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert not rings.receives_rings(PHONE) and rings.receives_rings(OTHER_PHONE)
    assert rings.device_state(OTHER_PHONE)["receive_rings"] is True


# ---- truthful state ------------------------------------------------------------------------

def test_rings_off_is_partial_while_a_live_unbound_token_remains_and_off_once_none_does(sander):
    apns.register_token(T_PHONE, owner=PHONE)
    apns.register_token(T_LEGACY, session="s")                  # legacy: cannot be attributed
    state = rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert state == {"ok": True, "receive_rings": False, "alerts_suppressed": False, "state": "partial",
                     "reason": "unbound_alert_tokens", "unbound_alert_tokens": 1}
    apns.disable_token(T_LEGACY, "gone")
    assert rings.device_state(PHONE)["state"] == "off"


def test_a_repeated_off_keeps_the_phone_off(sander):
    apns.register_token(T_PHONE, owner=PHONE)
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert rings.set_receive_rings(PHONE, {"receive_rings": False})["state"] == "off"


# ---- provenance: POST /devices binds a token to the authenticated device -------------------

def test_two_phones_on_the_same_session_never_retire_each_others_tokens(sander):
    apns.register_token(T_PHONE, session="koko", owner=PHONE)
    apns.register_token(T_OTHER, session="koko", owner=OTHER_PHONE)
    rows = _rows()
    assert rows[T_PHONE]["disabled_at"] is None and rows[T_OTHER]["disabled_at"] is None
    assert (rows[T_PHONE]["owner"], rows[T_OTHER]["owner"]) == (PHONE, OTHER_PHONE)


def test_a_rotation_retires_only_the_same_phones_old_token_and_keeps_rings_off(senders):
    apns.register_token(T_PHONE, owner=PHONE)
    apns.register_token(T_OTHER, owner=OTHER_PHONE)
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    apns.register_token(T_PHONE_2, owner=PHONE)                 # iOS rotated the token
    rows = _rows()
    assert rows[T_PHONE]["disabled_at"] is not None and rows[T_PHONE_2]["owner"] == PHONE
    assert rows[T_OTHER]["disabled_at"] is None
    assert rings.device_state(PHONE)["state"] == "off"
    classic = _ring()["ring"]
    rings.decline(OTHER_PHONE, classic["ring_id"])
    assert senders["alert"] and all(tokens == [T_OTHER] for _, tokens in senders["alert"])


def test_a_token_owned_by_another_phone_is_refused_with_no_side_effects(sander):
    apns.register_token(T_PHONE, session="koko", owner=PHONE)
    apns.register_token(T_OTHER, session="koko", owner=OTHER_PHONE)
    before = _rows()
    with pytest.raises(apns.TokenOwnedElsewhere):
        apns.register_token(T_PHONE, session="koko", owner=OTHER_PHONE)
    assert _rows() == before


def test_a_non_device_registrant_never_overwrites_an_owned_token(sander):
    apns.register_token(T_PHONE, session="koko", owner=PHONE)
    before = _rows()
    with pytest.raises(apns.TokenOwnedElsewhere):
        apns.register_token(T_PHONE, session="koko")            # administrator / desktop / unbound
    apns.register_token(T_LEGACY, session="koko")               # a new unbound token: allowed
    rows = _rows()
    assert rows[T_PHONE] == before[T_PHONE] and rows[T_LEGACY]["owner"] == ""


def test_legacy_unbound_tokens_are_preserved_never_guessed_or_mass_disabled(sander):
    apns.register_token(T_LEGACY, session="koko")
    apns.register_token(T_PHONE, session="koko", owner=PHONE)   # same session, now an owned token
    rows = _rows()
    assert rows[T_LEGACY]["disabled_at"] is None and rows[T_LEGACY]["owner"] == ""


def test_a_revoked_phone_keeps_provenance_gets_no_ring_alert_and_a_repaired_id_cannot_take_its_token(sander,
                                                                                                    monkeypatch):
    apns.register_token(T_PHONE, owner=PHONE)
    apns.register_token(T_OTHER, owner=OTHER_PHONE)
    real_ok = live.device_ok
    monkeypatch.setattr(live, "device_ok", lambda principal: principal != PHONE and real_ok(principal))
    assert [t["token"] for t in apns.ring_alert_tokens()] == [T_OTHER]
    assert _rows()[T_PHONE]["owner"] == PHONE                   # provenance kept, never cleared
    with pytest.raises(apns.TokenOwnedElsewhere):               # re-paired under a new id: no transfer
        apns.register_token(T_PHONE, owner="device_repaired")
    assert _rows()[T_PHONE]["owner"] == PHONE


# ---- Live admission and the answered ring --------------------------------------------------

def test_a_live_ring_is_refused_when_every_capable_phone_switched_rings_off(senders):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    rings.set_receive_rings(OTHER_PHONE, {"receive_rings": False})
    before = len(rings._load())
    with pytest.raises(CallError) as exc:
        _live_ring()
    assert (exc.value.status, exc.value.body()["error"]) == (409, "live_client_required")
    assert len(rings._load()) == before and senders["voip"] == []


def test_the_ring_a_phone_answered_stays_visible_to_it_after_rings_off_or_dropping_live(sander):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    rings.register_voip_token(PHONE, "ab" * 32)                 # capability dropped too
    assert rings.event_visible(_event(state="answered", answered_by=PHONE), PHONE)
    assert rings.event_visible(_event(state="ended", answered_by=PHONE, conversation_mode="agent_live_v1"), PHONE)
    assert not rings.event_visible(_event(state="answered", answered_by=OTHER_PHONE), PHONE)
    assert not rings.event_visible(_event(state="ringing"), PHONE)


def test_the_owner_migration_keeps_legacy_tokens_unbound_and_is_idempotent(tmp_path):
    from lib import db_migrations, db_schema
    path = tmp_path / "old.sqlite"
    db.reset_for_tests(path)
    c = db.conn()
    c.execute("ALTER TABLE device_tokens DROP COLUMN principal")       # a database from before the migration
    c.execute("INSERT INTO device_tokens(token, session, created_at, updated_at) VALUES ('ab', 's', 1, 1)")
    c.execute("PRAGMA user_version = 117")
    c.commit()
    db.reset_for_tests(path)
    c = db.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == db_schema._SCHEMA_VERSION
    assert dict(c.execute("SELECT token, principal AS owner, disabled_at FROM device_tokens").fetchone()) == \
        {"token": "ab", "owner": "", "disabled_at": None}
    assert db_migrations._migrate_to_v120(c) is None              # guarded: a rerun is a no-op


def test_a_fresh_database_has_the_principal_column_and_the_current_version(tmp_path):
    from lib import db_schema
    db.reset_for_tests(tmp_path / "fresh.sqlite")
    c = db.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == db_schema._SCHEMA_VERSION
    columns = {row[1]: row for row in c.execute("PRAGMA table_info(device_tokens)")}
    assert "principal" in columns and "owner" not in columns and columns["principal"][4] == "''"


# ---- token identity and transaction ownership (review of 5be1473b) -------------------------

def _raw(token, principal=""):
    db.conn().execute("INSERT INTO device_tokens(token, session, created_at, updated_at, principal) "
                      "VALUES (?, 's', 1, 1, ?)", (token, principal))


def test_another_phone_cannot_bypass_the_owner_with_an_uppercase_variant(sander):
    apns.register_token(T_PHONE, owner=PHONE)
    before = _rows()
    with pytest.raises(apns.TokenOwnedElsewhere):
        apns.register_token(T_PHONE.upper(), owner=OTHER_PHONE)
    with pytest.raises(apns.TokenOwnedElsewhere):
        apns.register_token(T_PHONE.upper())                     # a non-device registrant either
    assert _rows() == before


def test_the_same_phone_registering_a_case_variant_is_idempotent(sander):
    apns.register_token(T_PHONE, owner=PHONE)
    apns.register_token(T_PHONE.upper(), owner=PHONE)
    assert [t for t, r in _rows().items() if r["disabled_at"] is None] == [T_PHONE]
    assert _rows()[T_PHONE]["owner"] == PHONE


def test_a_legacy_case_duplicate_stays_unresolved_never_merged_or_claimed(sander):
    _raw(T_PHONE.upper())                                        # legacy, stored before canonical case
    apns.register_token(T_PHONE, owner=PHONE)
    rows = _rows()
    assert rows[T_PHONE.upper()] == {"token": T_PHONE.upper(), "owner": "", "disabled_at": None}
    assert rows[T_PHONE]["owner"] == PHONE
    assert rings.set_receive_rings(PHONE, {"receive_rings": False})["state"] == "partial"


def test_inside_a_callers_transaction_registration_never_commits_or_rolls_back_for_it(sander):
    c = db.conn()
    c.execute("BEGIN")
    apns.register_token(T_PHONE, owner=PHONE)
    with pytest.raises(apns.TokenOwnedElsewhere):
        apns.register_token(T_PHONE, owner=OTHER_PHONE)
    assert c.in_transaction                                      # the caller still owns its transaction
    c.execute("ROLLBACK")
    assert T_PHONE not in _rows()


@pytest.mark.parametrize("token", ["abc123", "zz" * 32, "a1" * 31, "a1" * 101])
def test_a_device_binds_only_a_real_apns_token_and_writes_nothing_otherwise(sander, token):
    before = _rows()
    with pytest.raises(apns.InvalidDeviceToken):
        apns.register_token(token, owner=PHONE)
    assert _rows() == before
    apns.register_token("abc123", session="s")                  # an unbound registrant: legacy acceptance kept


def test_distinct_non_hex_tokens_that_differ_only_in_case_keep_their_own_identity(sander):
    apns.register_token("AbG-token", session="one")
    apns.register_token("abg-token", session="two")              # a different opaque token: no conflict
    rows = _rows()
    assert rows["AbG-token"]["disabled_at"] is None and rows["abg-token"]["disabled_at"] is None
    apns.register_token(T_PHONE, owner=PHONE)                    # hex collision still sees case variants
    with pytest.raises(apns.TokenOwnedElsewhere):
        apns.register_token(T_PHONE.upper(), owner=OTHER_PHONE)
