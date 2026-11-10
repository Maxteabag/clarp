"""Hotseat status: the persisted snapshot behind GET /hotseat/status.

Contract v2 /var/tmp/hotseat-ui/CONTRACT-v2-root.md sha256
0b8c0d2d2cd9b3a585e6a8b8b6cef4eeb8885551896389dc771e7c298ca5042b (canonical; v2 with the C/D
clarification folded in). Every fixture is synthetic; the sentinels stand in for
what a real Hotseat payload can carry. The shared wire fixtures in
tests/fixtures/hotseat_status/ are the same files Arnold's iOS tests decode.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from lib import hotseat_status as status
from lib import settings_store

T0 = 1_800_000_000_000  # ms
FUTURE_S = T0 / 1000 + 3 * 3600
PAST_S = T0 / 1000 + 600  # passes during the tests below

SENTINEL_EMAIL = "sentinel.person@leak-example.test"
SENTINEL_TOKEN = "sk-ant-oat01-" + "Qz9" * 15
SENTINEL_PATH = "/home/sentinel/.claude/credentials.json"
SENTINEL_URL = "https://leak-example.test/oauth?code=SENTINELCODE"
SENTINEL_ERROR = "401 Unauthorized: sign in again SENTINELERR"
SENTINEL_PREVIEW = "Claude: switched to b. b has 90% of the 5-hour window left SENTINELPREVIEW"
SENTINEL_ORG = "Sentinel Org SENTINELORG"
SENTINELS = (SENTINEL_EMAIL, SENTINEL_TOKEN, "Qz9Qz9", SENTINEL_PATH, "/home/sentinel", SENTINEL_URL,
             "leak-example", "SENTINELCODE", SENTINEL_ERROR, "SENTINELERR", "Unauthorized", SENTINEL_PREVIEW,
             "SENTINELPREVIEW", SENTINEL_ORG, "SENTINELORG")


def claude_row(alias, *, active=False, used_5h=0.25, used_7d=0.5, reset_5h=FUTURE_S, reset_7d=FUTURE_S,
               email=None, error=None, limited=False, status_="allowed", **extra):
    usage = None if error else {"status": status_, "available": True, "limited": limited, "used_5h": used_5h,
                                "reset_5h": reset_5h, "used_7d": used_7d, "reset_7d": reset_7d, "scoped": [],
                                "blocked_models": [], "plan": "max"}
    return {"alias": alias, "email": email or f"{alias}@example.invalid", "org": None, "plan": "max",
            "plan_label": "Max 20×", "is_active": active, "error": error, "usage": usage, **extra}


def claude(*rows, **extra):
    return {"generated_at": T0 / 1000, "accounts": list(rows), "error": None, **extra}


def codex_row(alias, *, active=False, used=0.4, blocked=False, error=None, **extra):
    usage = {"usable": not blocked, "blocked": blocked, "error": error, "worst_used": used, "reset": FUTURE_S,
             "windows": [] if error else [{"label": "codex primary weekly", "used": used, "reset": FUTURE_S}]}
    return {"provider": "codex", "alias": alias, "is_active": active, "saved": True, "email": f"{alias}@example.invalid",
            "plan": "plus", "account_id": f"acct-{alias}", "usage": usage, **extra}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(status, "janitor_enabled", lambda: True)


def provider(payload, name):
    return next(p for p in payload["providers"] if p["provider"] == name)


def account(payload, name, alias):
    return next(a for a in provider(payload, name)["accounts"] if a["alias"] == alias)


def window(acct, kind):
    return next(w for w in acct["windows"] if w["kind"] == kind)


def lane(name, cadence_s, at=T0):
    status.record_action(name, {"action": "noop", "active": "a", "remaining": 75, "rung": 25}, mode="automatic",
                         switched=None, now=at, floor_pct=25, cadence_s=cadence_s, next_due_at=at + cadence_s * 1000)


# --- privacy -----------------------------------------------------------------

def test_no_sentinel_reaches_the_payload_the_store_or_the_logs(enabled, capsys):
    status.record_read("claude", claude(
        claude_row(SENTINEL_EMAIL, active=True, email=SENTINEL_EMAIL, org=SENTINEL_ORG,
                   usage_note=SENTINEL_URL, preview=SENTINEL_PREVIEW, token=SENTINEL_TOKEN),
        claude_row("work", error=SENTINEL_ERROR, plan=SENTINEL_TOKEN, plan_label=SENTINEL_PATH),
        claude_row(SENTINEL_TOKEN), claude_row(SENTINEL_PATH), claude_row(SENTINEL_URL),
        preview=SENTINEL_PREVIEW, error=SENTINEL_ERROR), T0)
    status.record_read("codex", {"accounts": [codex_row("p", active=True, error=SENTINEL_ERROR,
                                                        api_key=SENTINEL_TOKEN, home=SENTINEL_PATH)]}, T0)
    status.record_read("codex", "not a payload " + SENTINEL_PREVIEW, T0 + 1)
    status.record_failure("claude", RuntimeError(SENTINEL_ERROR), now=T0 + 2)
    status.record_action("claude", {"action": "switch", "active": SENTINEL_EMAIL, "target": SENTINEL_PATH,
                                    "remaining": 5, "target_remaining": 90, "rung": 25, "reason": SENTINEL_PREVIEW},
                         mode="automatic", switched=True, now=T0 + 3, floor_pct=25, cadence_s=300)
    served = json.dumps(status.payload(served_at=T0 + 4), ensure_ascii=False)
    stored = json.dumps({k: settings_store.get(k) for k in settings_store.keys_with_prefix("hotseat-status.")},
                        ensure_ascii=False)
    logs = capsys.readouterr()
    for sentinel in SENTINELS:
        assert sentinel not in served
        assert sentinel not in stored
        assert sentinel not in logs.out + logs.err
    aliases = [a["alias"] for a in provider(json.loads(served), "claude")["accounts"]]
    assert aliases[0] == "s…@l…" and "work" in aliases and "account" in aliases
    assert "hotseatStatusReadRejected provider=codex code=parse_failed" in logs.err


def test_internal_identity_keys_are_never_served(enabled):
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    stored = json.loads(settings_store.get(status.SNAPSHOT_PREFIX + "claude"))
    key = stored["accounts"][0]["identity"]
    served = json.dumps(status.payload(served_at=T0))
    assert key not in served and stored["accounts"][0]["tag"] not in served
    assert '"identity"' not in served and '"tag"' not in served


# --- classification ----------------------------------------------------------

def test_exhausted_only_from_limited_rejected_or_blocked(enabled):
    status.record_read("claude", claude(claude_row("lim", limited=True, used_5h=1.0),
                                        claude_row("rej", status_="rejected", used_5h=None, used_7d=None),
                                        claude_row("full", used_5h=1.0)), T0)
    status.record_read("codex", {"accounts": [codex_row("blk", blocked=True, used=1.0)]}, T0)
    served = status.payload(served_at=T0)
    assert account(served, "claude", "lim")["state"] == "exhausted"
    assert account(served, "claude", "rej")["state"] == "exhausted"
    assert account(served, "claude", "full")["state"] == "ok"  # a number at 100% is not a typed limit
    assert account(served, "codex", "blk")["state"] == "exhausted"
    assert account(served, "codex", "blk")["error_code"] == "exhausted"


def test_codex_unusable_without_a_block_is_unknown(enabled):
    row = codex_row("u", used=0.3)
    row["usage"]["usable"] = False
    status.record_read("codex", {"accounts": [row]}, T0)
    acct = account(status.payload(served_at=T0), "codex", "u")
    assert acct["state"] == "unknown" and acct["error_code"] is None
    assert window(acct, "7d")["used_ratio"] == 0.3


@pytest.mark.parametrize("reason, stays_exhausted", [("no credits", True), ("spend cap", True),
                                                      ("something new", True), ("", False)])
def test_a_blocked_account_whose_reset_will_not_unblock_it_stays_exhausted(enabled, reason, stays_exhausted):
    row = codex_row("n", blocked=True, used=1.0)
    row["usage"]["no_reset_reason"] = reason
    row["usage"]["windows"][0]["reset"] = PAST_S
    status.record_read("codex", {"accounts": [row]}, T0)
    stored = json.loads(settings_store.get(status.SNAPSHOT_PREFIX + "codex"))["accounts"][0]
    assert stored["no_reset"] == {"no credits": "no_credits", "spend cap": "spend_cap",
                                  "something new": "other", "": None}[reason]
    acct = account(status.payload(served_at=int(PAST_S * 1000) + 1), "codex", "n")
    assert acct["state"] == ("exhausted" if stays_exhausted else "unknown")
    assert "something new" not in json.dumps(status.payload(served_at=T0))


@pytest.mark.parametrize("reason", ["no credits", "spend cap", "something new"])
def test_a_reset_that_will_not_unblock_is_never_advertised(enabled, reason):
    row = codex_row("n", active=True, blocked=True, used=1.0)
    row["usage"]["no_reset_reason"] = reason
    status.record_read("codex", {"accounts": [row, codex_row("ok", used=0.4)]}, T0)
    for served_at in (T0 + 1000, int(FUTURE_S * 1000) + 1):  # before and after the advertised reset
        served = status.payload(served_at=served_at)
        n = account(served, "codex", "n")
        assert n["state"] == "exhausted" and n["error_code"] == "exhausted"
        assert all(w["resets_at"] is None for w in n["windows"])
        assert window(account(served, "codex", "ok"), "7d")["resets_at"] == int(FUTURE_S * 1000)
        assert reason not in json.dumps(served) and "no_reset" not in json.dumps(served)
    assert window(account(status.payload(served_at=T0 + 1000), "codex", "n"), "7d")["used_ratio"] == 1.0


def test_hotseat_float_seconds_become_epoch_milliseconds(enabled):
    status.record_read("claude", claude(claude_row("a", reset_5h=1_800_000_123.456)), T0)
    assert window(account(status.payload(served_at=T0), "claude", "a"), "5h")["resets_at"] == 1_800_000_123_456


def test_free_text_errors_are_unknown_provider_error_never_auth_error(enabled):
    status.record_read("claude", claude(claude_row("a", error="401 Unauthorized, please sign in (auth expired)")), T0)
    status.record_read("codex", {"accounts": [codex_row("p", error="token revoked: login required")]}, T0)
    served = status.payload(served_at=T0)
    for name, alias in (("claude", "a"), ("codex", "p")):
        acct = account(served, name, alias)
        assert acct["state"] == "unknown" and acct["error_code"] == "provider_error"
    assert "auth_error" not in json.dumps(served)


def test_auth_error_only_from_a_structured_field(enabled):
    status.record_read("claude", claude(claude_row("a", error="opaque", error_code="auth_error",
                                                    org_uuid="PRIVATE_ORG_UUID_SENTINEL")), T0)
    status.record_read("codex", {"accounts": [codex_row("p", error_code="auth_error")]}, T0)
    served = status.payload(served_at=T0)
    assert account(served, "claude", "a")["state"] == "auth_error"
    assert account(served, "codex", "p")["error_code"] == "auth_error"
    assert "PRIVATE_ORG_UUID_SENTINEL" not in json.dumps(served)
    assert "org_uuid" not in json.dumps(served)


def test_a_missing_number_is_null_never_zero(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=None, used_7d=0.3),
                                        claude_row("b", used_5h="12", used_7d=float("nan"))), T0)
    served = status.payload(served_at=T0)
    a = account(served, "claude", "a")
    assert window(a, "5h")["used_ratio"] is None and window(a, "5h")["remaining_ratio"] is None
    assert window(a, "7d")["used_ratio"] == 0.3 and window(a, "7d")["remaining_ratio"] == 0.7
    b = account(served, "claude", "b")
    assert b["state"] == "unknown" and b["last_good_observed_at"] is None
    assert all(w["used_ratio"] is None and w["remaining_ratio"] is None for w in b["windows"])
    assert "_pct" not in json.dumps(served)


def test_ratios_are_fractions_and_timestamps_milliseconds(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.25)), T0)
    w = window(account(status.payload(served_at=T0), "claude", "a"), "5h")
    assert (w["used_ratio"], w["remaining_ratio"]) == (0.25, 0.75)
    assert w["resets_at"] == int(FUTURE_S * 1000) and w["observed_at"] == T0 and w["source"] == "hotseat-cli"


# --- inventory -----------------------------------------------------------------

def test_empty_inventory_is_not_a_missing_snapshot(enabled):
    status.record_read("claude", claude(), T0)
    served = status.payload(served_at=T0)
    assert provider(served, "claude") | {"accounts": None} == provider(served, "claude") | {
        "inventory": "empty", "read_state": "ok", "observed_at": T0, "accounts": None}
    never = provider(served, "codex")
    assert never["inventory"] == "unavailable" and never["read_state"] == "never"
    assert never["observed_at"] is None and never["stale"] is True and never["accounts"] == []


def test_muse_is_unavailable_not_empty(enabled):
    muse = provider(status.payload(served_at=T0), "muse")
    assert muse["inventory"] == "unavailable" and muse["read_state"] == "never" and muse["accounts"] == []


# --- freshness -----------------------------------------------------------------

@pytest.mark.parametrize("cadence_s, fresh_until_s", [(300, 600), (900, 1800), (60, 600)])
def test_stale_after_twice_the_cadence_with_a_600_second_floor(enabled, cadence_s, fresh_until_s):
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    lane("claude", cadence_s)
    assert provider(status.payload(served_at=T0 + fresh_until_s * 1000), "claude")["stale"] is False
    late = status.payload(served_at=T0 + fresh_until_s * 1000 + 1)
    assert provider(late, "claude")["stale"] is True
    assert window(account(late, "claude", "a"), "5h")["stale"] is True


def test_serving_never_refreshes_observed_at(enabled):
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    first = status.payload(served_at=T0 + 1000)
    second = status.payload(served_at=T0 + 9000)
    assert second["served_at"] == T0 + 9000
    assert provider(first, "claude")["observed_at"] == provider(second, "claude")["observed_at"] == T0


@pytest.mark.parametrize("exc, code", [(RuntimeError("hotseat exited 1"), "cli_failed"),
                                       (subprocess.TimeoutExpired(["hotseat"], 60), "timeout"),
                                       (ValueError("Expecting value"), "parse_failed"),
                                       (KeyError("x"), "provider_error")])
def test_a_failed_read_is_stale_and_keeps_the_last_good_values(enabled, exc, code):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.25)), T0)
    status.record_failure("claude", exc, now=T0 + 1000)
    served = status.payload(served_at=T0 + 2000)
    claude_status = provider(served, "claude")
    assert claude_status["read_state"] == "failed" and claude_status["error_code"] == code
    assert claude_status["stale"] is True and claude_status["observed_at"] == T0
    a = account(served, "claude", "a")
    assert window(a, "5h")["used_ratio"] == 0.25 and window(a, "5h")["stale"] is True
    assert a["last_good_observed_at"] == T0
    assert served["actions"] == [] and served["switcher"]["lanes"] == []  # a failure is not a receipt (C)


def test_a_read_failure_after_a_real_action_leaves_actions_identical(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9), claude_row("b", used_5h=0.1)), T0)
    status.record_action("claude", {"action": "switch", "active": "a", "target": "b", "remaining": 10,
                                    "target_remaining": 90, "rung": 25}, mode="automatic", switched=True,
                         now=T0, floor_pct=25, cadence_s=300, next_due_at=T0 + 300_000)
    before = status.payload(served_at=T0 + 1000)
    status.record_failure("claude", subprocess.TimeoutExpired(["hotseat"], 60), now=T0 + 300_000)
    after = status.payload(served_at=T0 + 1000)
    assert json.dumps(after["actions"]) == json.dumps(before["actions"])
    assert after["switcher"] == before["switcher"]


def test_an_error_row_keeps_its_own_last_good_values_stale(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.25)), T0)
    status.record_read("claude", claude(claude_row("a", active=True, error="boom")), T0 + 60_000)
    a = account(status.payload(served_at=T0 + 61_000), "claude", "a")
    assert a["state"] == "unknown" and a["error_code"] == "provider_error"
    assert window(a, "5h")["used_ratio"] == 0.25 and window(a, "5h")["stale"] is True
    assert a["last_good_observed_at"] == T0


def test_a_disabled_janitor_serves_its_last_sample_stale(monkeypatch):
    monkeypatch.setattr(status, "janitor_enabled", lambda: False)
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    lane("claude", 300)
    served = status.payload(served_at=T0 + 1000)
    assert served["switcher"]["enabled"] is False and served["switcher"]["lanes"][0]["next_due_at"] is None
    claude_status = provider(served, "claude")
    assert claude_status["stale"] is True and claude_status["error_code"] == "disabled"
    assert window(account(served, "claude", "a"), "5h")["used_ratio"] == 0.25


def test_without_a_switcher_nothing_counts_as_enabled():
    assert status.janitor_enabled() is False


def test_a_past_reset_without_a_fresh_sample_is_unknown_not_full(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9, reset_5h=PAST_S),
                                        claude_row("x", limited=True, used_5h=1.0, reset_5h=PAST_S)), T0)
    served = status.payload(served_at=int(PAST_S * 1000) + 1)
    w = window(account(served, "claude", "a"), "5h")
    assert w["used_ratio"] is None and w["remaining_ratio"] is None and w["stale"] is True
    assert w["resets_at"] == int(PAST_S * 1000)
    assert window(account(served, "claude", "a"), "7d")["used_ratio"] == 0.5
    assert account(served, "claude", "x")["state"] == "unknown"


# --- identity binding ----------------------------------------------------------

def test_a_concurrent_switch_never_relabels_an_old_read(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9), claude_row("b", used_5h=0.1)), T0)
    status.record_action("claude", {"action": "switch", "active": "a", "target": "b", "remaining": 10,
                                    "target_remaining": 90, "rung": 25}, mode="automatic", switched=True,
                         now=T0 + 1000, floor_pct=25, cadence_s=300)
    # A read that began before the switch and finished after it is older than what is stored.
    status.record_read("claude", claude(claude_row("a", used_5h=0.95), claude_row("b", active=True, used_5h=0.2)),
                       T0 - 5000)
    served = status.payload(served_at=T0 + 2000)
    assert account(served, "claude", "a")["active_default"] is True
    assert account(served, "claude", "b")["active_default"] is False
    assert window(account(served, "claude", "a"), "5h")["used_ratio"] == 0.9
    assert served["actions"][0] | {"at": None} == {"provider": "claude", "kind": "switched", "at": None,
                                                    "reason_code": "below_floor", "from_alias": "a", "to_alias": "b"}


def test_last_good_values_follow_the_identity_not_the_alias(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.25, email="one@example.invalid")), T0)
    status.record_read("claude", claude(claude_row("a", active=True, error="boom", email="two@example.invalid")),
                       T0 + 1000)
    a = account(status.payload(served_at=T0 + 2000), "claude", "a")
    assert a["windows"] == [] and a["last_good_observed_at"] is None


def test_actions_are_closed_codes(enabled):
    base = {"active": "a", "remaining": 5, "target": "b", "target_remaining": 90}
    cases = [({"action": "switch", **base, "rung": 25}, "notify", None, ("would_switch", "below_floor")),
             ({"action": "switch", **base, "rung": 10}, "automatic", True, ("switched", "below_floor")),
             ({"action": "switch", **base, "rung": 25}, "automatic", False, ("failed", "switch_failed")),
             ({"action": "noop", "active": "a", "remaining": 75, "rung": 25}, "automatic", None, ("noop", "already_ok")),
             ({"action": "exhausted", "active": "a", "remaining": 0}, "automatic", None, ("exhausted", "no_better_account")),
             ({"action": "skip", "reason": "a: quota unknown"}, "automatic", None, ("skipped", "quota_unknown")),
             ({"action": "skip", "reason": "Hotseat reports no account in use"}, "automatic", None, ("skipped", "no_active_account"))]
    seen = set()
    for decision, mode, switched, expected in cases:
        status.record_action("claude", decision, mode=mode, switched=switched, now=T0, floor_pct=25, cadence_s=300)
        action = status.payload(served_at=T0)["actions"][0]
        assert (action["kind"], action["reason_code"]) == expected
        assert set(action) == {"provider", "kind", "at", "reason_code", "from_alias", "to_alias"}
        seen.add(action["reason_code"])
    assert seen == status.REASON_CODES == {"no_active_account", "quota_unknown", "already_ok", "below_floor",
                                           "switch_failed", "no_better_account"}


def test_payload_has_the_contract_shape(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, org_uuid="PRIVATE_ORG_UUID_SENTINEL", error_code=None)), T0)
    lane("claude", 300)
    served = status.payload(served_at=T0)
    assert served["schema"] == "clarp.hotseat-status/1"
    assert set(served) == {"schema", "served_at", "source", "switcher", "actions", "providers"}
    assert served["source"]["cli_version"] is None and served["source"]["cost_note"]
    assert served["switcher"] | {"lanes": None} == {"kind": "deterministic", "model": None, "enabled": True, "lanes": None}
    assert served["switcher"]["lanes"] == [{"provider": "claude", "mode": "automatic", "floor_pct": 25, "cadence_s": 300,
                                            "last_run_at": T0, "next_due_at": T0 + 300_000}]
    assert [p["provider"] for p in served["providers"]] == ["claude", "codex", "muse"]
    assert set(provider(served, "claude")) == {"provider", "inventory", "observed_at", "stale", "partial", "read_state",
                                               "error_code", "cadence_s", "accounts"}
    acct = account(served, "claude", "a")
    assert set(acct) == {"alias", "label", "plan", "active_default", "state", "error_code",
                         "last_good_observed_at", "windows"}
    assert acct["plan"] == "Max" and acct["label"] == "Max 20×"
    assert set(acct["windows"][0]) == {"kind", "label", "used_ratio", "remaining_ratio", "resets_at",
                                       "observed_at", "stale", "source"}


def test_collections_are_bounded(enabled):
    status.record_read("claude", claude(*[claude_row(f"acct{i}") for i in range(status.MAX_ACCOUNTS + 5)]), T0)
    claude_status = provider(status.payload(served_at=T0), "claude")
    assert len(claude_status["accounts"]) == status.MAX_ACCOUNTS and claude_status["partial"] is True


# --- the route -----------------------------------------------------------------

def _handler(path):
    import server as host
    handler = host.Handler.__new__(host.Handler)
    handler.path = path
    handler._request_device_scope = "limited"
    handler._request_auth_validated = True
    handler._request_principal = "synthetic-device"
    handler.sent = []
    handler._send = lambda code, body=b"", *_a, **_k: handler.sent.append((code, body))
    handler._json_error = lambda code, message: handler.sent.append((code, message))
    handler._auth_failure_retry = lambda: 0
    return handler


@pytest.fixture
def no_processes(monkeypatch):
    import os

    def refuse(*_a, **_k):
        raise AssertionError("GET /hotseat/status must not start a process")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "check_output", refuse)
    monkeypatch.setattr(os, "system", refuse)


def test_the_route_refuses_without_auth(no_processes):
    handler = _handler("/hotseat/status")
    handler._authorized = lambda: False
    handler.do_GET()
    assert handler.sent == [(401, "unauthorized")]


def test_the_route_serves_the_allowlisted_snapshot_without_a_process(enabled, no_processes, monkeypatch):
    monkeypatch.setattr(status, "now_ms", lambda: T0)
    status.record_read("claude", claude(claude_row(SENTINEL_EMAIL, active=True, email=SENTINEL_EMAIL)), T0)
    handler = _handler("/hotseat/status")
    handler._authorized = lambda: True
    handler.do_GET()
    [(code, body)] = handler.sent
    served = json.loads(body)
    assert code == 200 and served["schema"] == "clarp.hotseat-status/1"
    assert provider(served, "claude")["accounts"][0]["alias"] == "s…@l…"
    assert SENTINEL_EMAIL not in body.decode()
    # Repair journal contents are private, even if malformed or hostile. GET
    # does not consult that key, run reconciliation or expose new status keys.
    from lib import janitor_hotseat
    settings_store.set_text(janitor_hotseat.REPAIR_STATE_KEY, SENTINEL_TOKEN)
    original_get = settings_store.get
    def no_repair_journal(key):
        assert key != janitor_hotseat.REPAIR_STATE_KEY, "GET consulted private repair state"
        return original_get(key)
    monkeypatch.setattr(settings_store, "get", no_repair_journal)
    handler.sent.clear()
    handler.do_GET()
    assert handler.sent == [(200, body)]


def test_the_feature_is_advertised_with_its_number():
    from lib import server_identity
    assert server_identity.feature_advertised("accounts_hotseat")
    assert server_identity.FEATURE_CONTRACTS["accounts_hotseat"] <= server_identity.HOST_CONTRACT


# --- the hooks never reach into the Janitor ------------------------------------

def test_read_accounts_returns_the_same_parse_when_recording_fails(monkeypatch, capsys):
    from lib import janitor_hotseat

    class Proc:
        returncode = 0
        stdout = json.dumps(claude(claude_row("a", active=True, used_5h=0.5)))
        stderr = ""
    monkeypatch.setattr(janitor_hotseat.subprocess, "run", lambda *_a, **_k: Proc())
    expected = janitor_hotseat.parse_claude(json.loads(Proc.stdout))
    assert janitor_hotseat.read_accounts("hotseat", "claude") == expected
    assert provider(status.payload(served_at=T0), "claude")["read_state"] == "ok"

    def explode(*_a, **_k):
        raise RuntimeError(SENTINEL_ERROR)
    monkeypatch.setattr(status, "parse", explode)
    assert janitor_hotseat.read_accounts("hotseat", "claude") == expected
    err = capsys.readouterr().err
    assert "hotseatStatusRecordFailed site=record_read error=RuntimeError" in err
    assert "SENTINELERR" not in err


def test_the_switcher_runs_unchanged_when_every_receipt_fails(monkeypatch, capsys):
    from lib import agents, janitor_builtins, janitors
    from lib import janitor_autonomy as service
    from lib import janitor_hotseat
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    agents.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    cfg = janitors.create("hotseat", template_id="account-hotseat")
    owner = janitors.set_enabled("hotseat", cfg["revision"], True)
    service.setup()

    def explode(*_a, **_k):
        raise RuntimeError(SENTINEL_ERROR)
    monkeypatch.setattr(status, "record_action", explode)
    monkeypatch.setattr(status, "record_failure", explode)
    switches, sent = [], []

    def read(command, name):
        if name == "codex":
            raise RuntimeError("hotseat exited 1")
        return janitor_hotseat.parse_claude(claude(claude_row("a", active=True, used_5h=0.9),
                                                   claude_row("b", used_5h=0.1)))
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: sent.append(p) or {"sent": 1}, hotseat_read=read,
                                      hotseat_switch=lambda c, p, alias: switches.append(alias) or True)
    worker.hotseat_once()
    assert switches == ["b"] and len(sent) == 1 and sent[0]["preview"].startswith("Claude: switched to b")
    outcomes = sorted(r["outcome"] for r in janitors.list_runs(owner["session"]))
    assert outcomes == ["completed", "failed"]
    err = capsys.readouterr().err
    assert "hotseatStatusReceiptFailed site=action error=RuntimeError" in err
    assert "hotseatStatusReceiptFailed site=read_failure error=RuntimeError" in err
    assert "SENTINELERR" not in err


def test_a_disabled_switcher_records_nothing(monkeypatch):
    from lib import agents, janitor_builtins, janitors
    from lib import janitor_autonomy as service
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    agents.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    janitors.create("hotseat", template_id="account-hotseat")  # created paused
    service.setup()
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: {"sent": 1},
                                      hotseat_read=lambda *_: pytest.fail("read"),
                                      hotseat_switch=lambda *_: pytest.fail("switched"))
    worker.hotseat_once()
    assert settings_store.keys_with_prefix("hotseat-status.") == []
    served = status.payload()
    assert served["switcher"]["enabled"] is False and served["actions"] == []
    assert {p["provider"]: p["error_code"] for p in served["providers"]} == {"claude": "disabled", "codex": "disabled",
                                                                             "muse": None}


@pytest.mark.parametrize("fails", ["begin_run", "claim_run"])
def test_a_begin_or_claim_failure_records_nothing(monkeypatch, fails):
    """Contract 0b8c0d2d section C (janitor_autonomy.py:266-267): a run that cannot begin or be
    claimed reads nothing, writes no receipt and leaves the snapshot byte-identical."""
    from lib import agents, janitor_builtins, janitors
    from lib import janitor_autonomy as service
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    agents.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    cfg = janitors.create("hotseat", template_id="account-hotseat")
    janitors.set_enabled("hotseat", cfg["revision"], True)
    service.setup()
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9), claude_row("b", used_5h=0.1)), T0)
    status.record_action("claude", {"action": "noop", "active": "a", "remaining": 75, "rung": 25}, mode="automatic",
                         switched=None, now=T0, floor_pct=25, cadence_s=300, next_due_at=T0 + 300_000)
    before = {k: settings_store.get(k) for k in settings_store.keys_with_prefix("hotseat-status.")}
    if fails == "begin_run":
        monkeypatch.setattr(service.janitor_builtins, "begin_run", lambda *_a, **_k: None)
    else:
        monkeypatch.setattr(service.janitor_builtins, "claim_run", lambda *_a, **_k: False)
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: {"sent": 1},
                                      hotseat_read=lambda *_: pytest.fail("read"),
                                      hotseat_switch=lambda *_: pytest.fail("switched"))
    worker.hotseat_once()
    after = {k: settings_store.get(k) for k in settings_store.keys_with_prefix("hotseat-status.")}
    assert after == before


def test_the_full_precedence_chain_with_a_synthetic_usage_worker_sample(enabled):
    """Contract 0b8c0d2d D: newer observed_at for the same identity, then the highest used_ratio
    (null loses), then hotseat-cli before usage-worker, then the lexically lower label."""
    def w(used, source, label="5-hour"):
        return {"kind": "5h", "label": label, "used": used, "resets_at": None, "observed_at": T0, "source": source}
    # Same observed_at (one read): the highest used wins, whatever the source.
    assert status._unique([w(0.4, "hotseat-cli"), w(0.6, "usage-worker")])[0] == [w(0.6, "usage-worker")]
    # Null loses, even against hotseat-cli.
    assert status._unique([w(None, "hotseat-cli"), w(0.1, "usage-worker")])[0] == [w(0.1, "usage-worker")]
    # Equal used: hotseat-cli before usage-worker, in either input order.
    for pair in ([w(0.5, "usage-worker"), w(0.5, "hotseat-cli")], [w(0.5, "hotseat-cli"), w(0.5, "usage-worker")]):
        kept, dropped = status._unique(pair)
        assert kept == [w(0.5, "hotseat-cli")] and dropped
    # Equal used and source: the lexically lower label.
    assert status._unique([w(0.5, "usage-worker", "b"), w(0.5, "usage-worker", "a")])[0] == [w(0.5, "usage-worker", "a")]
    # Newer observed_at first: a newer, less constraining read of the same identity wins over the older one.
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9)), T0)
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.2)), T0 + 60_000)
    assert window(account(status.payload(served_at=T0 + 61_000), "claude", "a"), "5h")["used_ratio"] == 0.2


def test_one_window_per_kind_highest_used_wins_and_partial_is_set(enabled):
    row = claude_row("a", active=True)
    row["usage"]["scoped"] = [{"model": "claude-opus-4", "used": 0.2, "reset": FUTURE_S},
                              {"model": "opus-legacy", "used": 0.6, "reset": FUTURE_S}]
    status.record_read("claude", claude(row), T0)
    p = codex_row("p", active=True, used=0.3)
    p["usage"]["windows"] += [{"label": "codex primary weekly", "used": None, "reset": FUTURE_S},
                              {"label": "codex primary weekly", "used": 0.7, "reset": FUTURE_S}]
    status.record_read("codex", {"accounts": [p]}, T0)
    served = status.payload(served_at=T0)
    a = account(served, "claude", "a")
    assert [w["kind"] for w in a["windows"]] == ["5h", "7d", "model:opus"]
    assert window(a, "model:opus")["used_ratio"] == 0.6
    assert window(account(served, "codex", "p"), "7d")["used_ratio"] == 0.7
    assert provider(served, "claude")["partial"] is True and provider(served, "codex")["partial"] is True


# Contract 0b8c0d2d section C: exactly six, 1:1 with decide()'s branches.
CLOSED_REASON_CODES = {"no_active_account", "quota_unknown", "already_ok", "below_floor", "switch_failed",
                       "no_better_account"}
ERROR_ONLY_CODES = {"disabled", "timeout", "cli_failed", "parse_failed", "provider_error"}


def test_read_failures_and_disabled_are_error_codes_never_receipts(enabled):
    """C: a read failure updates only read_state/error_code; those codes are never a reason_code."""
    assert status.REASON_CODES == CLOSED_REASON_CODES and not status.REASON_CODES & ERROR_ONLY_CODES
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    for exc in (subprocess.TimeoutExpired(["hotseat"], 60), RuntimeError("exit 1"), ValueError("x"), KeyError("x")):
        status.record_failure("claude", exc, now=T0 + 1000)
        assert status.payload(served_at=T0 + 2000)["actions"] == []
    status.record_action("claude", {"action": "skip", "reason": "something decide() never writes"}, mode="automatic",
                         switched=None, now=T0, floor_pct=25, cadence_s=300)
    assert status.payload(served_at=T0)["actions"] == []  # an unknown branch writes no receipt, never free text


def test_every_emitted_reason_code_is_in_the_set_of_six(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9), claude_row("b", used_5h=0.1)), T0)
    switch = {"action": "switch", "active": "a", "remaining": 10, "target": "b", "target_remaining": 90, "rung": 25}
    decisions = [switch, {**switch, "rung": 10}, {"action": "noop", "active": "a", "remaining": 75, "rung": 25},
                 {"action": "exhausted", "active": "a", "remaining": 0},
                 {"action": "skip", "reason": "Hotseat reports no account in use"},
                 {"action": "skip", "reason": "a: quota unknown"},
                 {"action": "skip", "reason": SENTINEL_PREVIEW}, {"action": "teleport"}, {}]
    emitted = []
    for decision in decisions:
        for mode in ("automatic", "notify"):
            for switched in (True, False, None):
                status.record_action("claude", decision, mode=mode, switched=switched, now=T0, floor_pct=25,
                                     cadence_s=300)
                emitted += [a["reason_code"] for a in status.payload(served_at=T0)["actions"]]
    assert emitted and set(emitted) <= CLOSED_REASON_CODES
    assert set(emitted) == status.REASON_CODES


def test_a_sample_without_observed_at_is_served_null_and_stale(enabled):
    """D2: no observed_at is ineligible for precedence; as the only sample of its kind it is
    emitted with observed_at null and stale true."""
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    key = status.SNAPSHOT_PREFIX + "claude"
    stored = json.loads(settings_store.get(key))
    stored["accounts"][0]["windows"][0].pop("observed_at")
    settings_store.set_text(key, json.dumps(stored))
    w = window(account(status.payload(served_at=T0 + 1000), "claude", "a"), "5h")
    assert w["observed_at"] is None and w["stale"] is True
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=None)), T0 + 2000)
    w = window(account(status.payload(served_at=T0 + 3000), "claude", "a"), "5h")
    assert w["used_ratio"] is None and w["observed_at"] == T0 + 2000  # it never displaced the newer read's slot


def test_a_newer_sample_of_the_same_identity_wins_over_a_more_constraining_older_one(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.9)), T0)
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.2)), T0 + 60_000)
    w = window(account(status.payload(served_at=T0 + 61_000), "claude", "a"), "5h")
    assert (w["used_ratio"], w["observed_at"]) == (0.2, T0 + 60_000)
    assert provider(status.payload(served_at=T0 + 61_000), "claude")["partial"] is False  # not a duplicate


def test_duplicate_and_unknown_windows_map_to_unique_known_kinds(enabled):
    p = codex_row("p", active=True, used=0.3)
    p["usage"]["windows"] += [{"label": "codex secondary 5-hour", "used": 0.6, "reset": FUTURE_S},
                              {"label": "codex secondary 5-hour", "used": 0.8, "reset": FUTURE_S},
                              {"label": "codex primary 2-day", "used": 0.99, "reset": FUTURE_S},
                              {"label": "zebra weekly", "used": 0.99, "reset": FUTURE_S},
                              {"label": "", "used": 0.99, "reset": FUTURE_S}]
    row = claude_row("a", active=True)
    row["usage"]["scoped"] = [{"model": "claude-mystery-9", "used": 0.99, "reset": FUTURE_S}]
    status.record_read("codex", {"accounts": [p]}, T0)
    status.record_read("claude", claude(row), T0)
    served = status.payload(served_at=T0)
    windows = account(served, "codex", "p")["windows"]
    assert [(w["kind"], w["label"], w["used_ratio"]) for w in windows] == [("7d", "weekly", 0.3), ("5h", "5-hour", 0.8)]
    assert [w["kind"] for w in account(served, "claude", "a")["windows"]] == ["5h", "7d"]
    assert provider(served, "codex")["partial"] is True  # a duplicate was dropped
    assert provider(served, "claude")["partial"] is False  # an unknown name is not a duplicate
    assert "0.99" not in json.dumps(served)


def test_a_newer_null_keeps_the_last_good_value_with_its_own_freshness(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.25, used_7d=0.5)), T0)
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.3, used_7d=None)), T0 + 500_000)
    a = account(status.payload(served_at=T0 + 550_000), "claude", "a")
    assert window(a, "5h")["used_ratio"] == 0.3 and window(a, "5h")["observed_at"] == T0 + 500_000
    assert window(a, "7d")["used_ratio"] == 0.5 and window(a, "7d")["observed_at"] == T0
    assert window(a, "7d")["stale"] is False  # 550 s old, under the 600 s floor
    a = account(status.payload(served_at=T0 + 600_001), "claude", "a")
    assert window(a, "7d")["stale"] is True and window(a, "5h")["stale"] is False
    assert a["last_good_observed_at"] == T0 + 500_000


def test_the_switcher_records_its_receipts(monkeypatch, enabled):
    from lib import agents, janitor_builtins, janitors
    from lib import janitor_autonomy as service
    from lib import janitor_hotseat
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    agents.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    cfg = janitors.create("hotseat", template_id="account-hotseat")
    janitors.set_enabled("hotseat", cfg["revision"], True)
    service.setup()

    failing = []

    def read(command, name):
        if name in failing:
            raise subprocess.TimeoutExpired(["hotseat"], 150)
        payload = (claude(claude_row("a", active=True, used_5h=0.9), claude_row("b", used_5h=0.1)) if name == "claude"
                   else {"accounts": [codex_row("p", active=True, used=0.5)]})
        status.record_read(name, payload)  # what read_accounts does after json.loads
        return (janitor_hotseat.parse_claude if name == "claude" else janitor_hotseat.parse_codex)(payload)
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: {"sent": 1}, hotseat_read=read,
                                      hotseat_switch=lambda *_: True)
    worker.hotseat_once()
    served = status.payload()
    actions = {a["provider"]: a for a in served["actions"]}
    assert (actions["claude"]["kind"], actions["claude"]["from_alias"], actions["claude"]["to_alias"]) == ("switched", "a", "b")
    assert (actions["codex"]["kind"], actions["codex"]["reason_code"]) == ("noop", "already_ok")
    failing.append("codex")
    key = "janitor-adaptive.hotseat-switcher.codex"
    lane_state = json.loads(settings_store.get_text(key, default="") or "{}")
    lane_state["next_run_at"] = 0
    settings_store.set_text(key, json.dumps(lane_state))
    worker.hotseat_once()
    after = status.payload()
    assert after["actions"] == served["actions"]  # the failed read wrote no receipt
    assert provider(after, "codex")["error_code"] == "timeout" and provider(after, "codex")["read_state"] == "failed"
    lanes = {lane["provider"]: lane for lane in served["switcher"]["lanes"]}
    assert lanes["claude"]["cadence_s"] == 300 and lanes["codex"]["cadence_s"] == 900
    assert lanes["claude"]["floor_pct"] == 25 and lanes["codex"]["floor_pct"] == 10
    assert lanes["claude"]["next_due_at"] > lanes["claude"]["last_run_at"]


# --- shared wire fixtures (the same files Arnold's iOS tests decode) -------------

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "hotseat_status"
FAILURES = {"timeout": lambda: subprocess.TimeoutExpired(["hotseat"], 60), "cli_failed": lambda: RuntimeError("exit 1"),
            "parse_failed": lambda: ValueError("bad json"), "provider_error": lambda: KeyError("x")}


def _cases():
    return sorted(p.name[:-len(".input.json")] for p in FIXTURES.glob("*.input.json"))


def test_every_fixture_has_its_wire_file():
    assert _cases() and all((FIXTURES / f"{case}.wire.json").exists() for case in _cases())


@pytest.mark.parametrize("case", _cases())
def test_shared_wire_fixture(monkeypatch, case):
    scenario = json.loads((FIXTURES / f"{case}.input.json").read_text())
    monkeypatch.setattr(status, "janitor_enabled", lambda: scenario["janitor_enabled"])
    for step in scenario["steps"]:
        if step["op"] == "read":
            status.record_read(step["provider"], step["payload"], step["received_at"])
        elif step["op"] == "action":
            status.record_action(step["provider"], step["decision"], mode=step["mode"], switched=step["switched"],
                                 now=step["now"], floor_pct=step["floor_pct"], cadence_s=step["cadence_s"],
                                 next_due_at=step["next_due_at"])
        else:
            status.record_failure(step["provider"], FAILURES[step["error"]](), now=step["now"])
    expected = json.loads((FIXTURES / f"{case}.wire.json").read_text())
    assert status.payload(served_at=scenario["served_at"]) == expected


def _wire(path):
    return json.loads(path.read_text())


def test_client_negative_files_are_labelled_and_never_produced_by_the_host():
    negatives = sorted(FIXTURES.glob("*-CLIENT-NEGATIVE.wire.json"))
    assert {p.name for p in negatives} == {"F17-unknown-reason-code-CLIENT-NEGATIVE.wire.json",
                                           "F18c-duplicate-kind-CLIENT-NEGATIVE.wire.json"}
    for path in negatives:
        assert _wire(path)["client_negative"]
        assert not (FIXTURES / path.name.replace("-CLIENT-NEGATIVE.wire.json", ".input.json")).exists()
    assert {a["reason_code"] for a in _wire(negatives[0])["actions"]} - CLOSED_REASON_CODES
    kinds = [w["kind"] for w in _wire(negatives[1])["providers"][0]["accounts"][0]["windows"]]
    assert len(kinds) != len(set(kinds))


@pytest.mark.parametrize("case", _cases())
def test_positive_wire_files_keep_the_closed_codes_and_unique_kinds(case):
    served = _wire(FIXTURES / f"{case}.wire.json")
    assert "client_negative" not in served
    assert {a["reason_code"] for a in served["actions"]} <= CLOSED_REASON_CODES
    for p in served["providers"]:
        for acct in p["accounts"]:
            kinds = [w["kind"] for w in acct["windows"]]
            assert len(kinds) == len(set(kinds)), (case, acct["alias"])


# --- B review fff49208, F1: an error row contributes no samples ----------------------------

LATER_RESET_S = FUTURE_S + 7 * 24 * 3600


def _codex_error_row(alias, *, used, reset_s, kind, account_id=None):
    """A same-alias Codex row whose read failed but still carries a numeric (cached) window."""
    row = codex_row(alias, active=True, used=used)
    row["usage"]["windows"] = [{"label": "codex primary weekly", "used": used, "reset": reset_s}]
    row["usage"]["worst_used"] = used
    if account_id is not None:
        row["account_id"] = account_id
    if kind == "usage_error":
        row["usage"]["error"] = SENTINEL_ERROR + " " + SENTINEL_TOKEN
    elif kind == "row_auth":
        row["error_code"] = "auth_error"
    else:  # usage-level typed auth
        row["usage"]["error_code"] = "auth_error"
    return row


@pytest.mark.parametrize("kind, state, code", [("usage_error", "unknown", "provider_error"),
                                               ("row_auth", "auth_error", "auth_error"),
                                               ("usage_auth", "auth_error", "auth_error")])
def test_a_codex_error_row_with_numeric_windows_keeps_the_same_identity_last_good(enabled, capsys, kind, state, code):
    status.record_read("codex", {"accounts": [codex_row("p", active=True, used=0.25)]}, T0)
    status.record_read("codex", {"accounts": [_codex_error_row("p", used=0.9, reset_s=LATER_RESET_S, kind=kind)]},
                       T0 + 60_000)
    served = status.payload(served_at=T0 + 61_000)
    p = account(served, "codex", "p")
    assert (p["state"], p["error_code"]) == (state, code)
    assert p["last_good_observed_at"] == T0
    w = window(p, "7d")
    assert (w["used_ratio"], w["resets_at"], w["observed_at"], w["stale"]) == (0.25, int(FUTURE_S * 1000), T0, True)
    assert [x["kind"] for x in p["windows"]] == ["7d"]
    stored = json.dumps({k: settings_store.get(k) for k in settings_store.keys_with_prefix("hotseat-status.")})
    logs = capsys.readouterr()
    for blob in (json.dumps(served), stored, logs.out + logs.err):
        assert "SENTINELERR" not in blob and "Qz9Qz9" not in blob
    assert '"used":0.9' not in stored and "0.9," not in json.dumps(served)  # the error row's number is no sample
    # A healthy newer read of the same identity still wins.
    status.record_read("codex", {"accounts": [codex_row("p", active=True, used=0.4)]}, T0 + 120_000)
    w = window(account(status.payload(served_at=T0 + 121_000), "codex", "p"), "7d")
    assert (w["used_ratio"], w["observed_at"], w["stale"]) == (0.4, T0 + 120_000, False)


@pytest.mark.parametrize("kind", ["usage_error", "row_auth", "usage_auth"])
def test_a_codex_error_row_without_a_previous_good_value_invents_none(enabled, kind):
    status.record_read("codex", {"accounts": [_codex_error_row("p", used=0.9, reset_s=LATER_RESET_S, kind=kind)]}, T0)
    p = account(status.payload(served_at=T0 + 1000), "codex", "p")
    assert p["windows"] == [] and p["last_good_observed_at"] is None


@pytest.mark.parametrize("kind", ["usage_error", "row_auth", "usage_auth"])
def test_a_codex_error_row_of_another_identity_inherits_nothing(enabled, kind):
    status.record_read("codex", {"accounts": [codex_row("p", active=True, used=0.25)]}, T0)
    other = _codex_error_row("p", used=0.9, reset_s=LATER_RESET_S, kind=kind, account_id="acct-someone-else")
    status.record_read("codex", {"accounts": [other]}, T0 + 60_000)
    p = account(status.payload(served_at=T0 + 61_000), "codex", "p")
    assert p["windows"] == [] and p["last_good_observed_at"] is None


def _stored_no_reset(alias, provider="codex"):
    accounts = json.loads(settings_store.get(status.SNAPSHOT_PREFIX + provider))["accounts"]
    return next(a.get("no_reset") for a in accounts if a["alias"] == alias)


@pytest.mark.parametrize("prior_hint, error_hint, served_reset", [
    (None, "no credits", int(FUTURE_S * 1000)),  # the failed row's hint must not hide the good reset
    ("no credits", None, None),                  # a missing hint must not expose a withheld reset
    ("spend cap", "no credits", None),           # conflicting hints: the last-good semantics stay
])
def test_a_failed_row_keeps_the_last_good_no_reset_semantics(enabled, prior_hint, error_hint, served_reset):
    good = codex_row("p", active=True, used=0.25)
    if prior_hint:
        good["usage"]["no_reset_reason"] = prior_hint
    status.record_read("codex", {"accounts": [good]}, T0)
    bad = _codex_error_row("p", used=0.9, reset_s=LATER_RESET_S, kind="usage_error")
    if error_hint:
        bad["usage"]["no_reset_reason"] = error_hint
    status.record_read("codex", {"accounts": [bad]}, T0 + 60_000)
    w = window(account(status.payload(served_at=T0 + 61_000), "codex", "p"), "7d")
    assert (w["used_ratio"], w["resets_at"], w["observed_at"], w["stale"]) == (0.25, served_reset, T0, True)
    assert _stored_no_reset("p") == {None: None, "no credits": "no_credits", "spend cap": "spend_cap"}[prior_hint]


@pytest.mark.parametrize("other_identity", [False, True])
def test_a_failed_row_without_a_same_identity_prior_carries_no_hint(enabled, other_identity):
    if other_identity:
        status.record_read("codex", {"accounts": [codex_row("p", active=True, used=0.25)]}, T0)
    bad = _codex_error_row("p", used=0.9, reset_s=LATER_RESET_S, kind="usage_error",
                           account_id="acct-someone-else" if other_identity else None)
    bad["usage"]["no_reset_reason"] = "no credits"
    status.record_read("codex", {"accounts": [bad]}, T0 + 60_000)
    p = account(status.payload(served_at=T0 + 61_000), "codex", "p")
    assert p["windows"] == [] and p["last_good_observed_at"] is None
    assert _stored_no_reset("p") is None


def test_a_healthy_row_still_sets_its_own_no_reset_hint(enabled):
    status.record_read("codex", {"accounts": [codex_row("p", active=True, used=0.25)]}, T0)
    healthy = codex_row("p", active=True, blocked=True, used=1.0)
    healthy["usage"]["no_reset_reason"] = "no credits"
    status.record_read("codex", {"accounts": [healthy]}, T0 + 60_000)
    p = account(status.payload(served_at=T0 + 61_000), "codex", "p")
    assert p["state"] == "exhausted" and window(p, "7d")["resets_at"] is None
    assert _stored_no_reset("p") == "no_credits"


def test_a_claude_typed_auth_row_with_numeric_usage_keeps_the_last_good(enabled):
    status.record_read("claude", claude(claude_row("a", active=True, used_5h=0.25)), T0)
    row = claude_row("a", active=True, used_5h=0.9, used_7d=0.95, reset_5h=LATER_RESET_S, error_code="auth_error")
    status.record_read("claude", claude(row), T0 + 60_000)
    a = account(status.payload(served_at=T0 + 61_000), "claude", "a")
    assert (a["state"], a["error_code"], a["last_good_observed_at"]) == ("auth_error", "auth_error", T0)
    w = window(a, "5h")
    assert (w["used_ratio"], w["resets_at"], w["observed_at"], w["stale"]) == (0.25, int(FUTURE_S * 1000), T0, True)


# --- B review fff49208, F2: a failing diagnostic sink never reaches the switcher ----------------
# No-DB evidence: a broken log sink falls back to the structured eventlog (the second half log()
# itself writes), never to a settings/state write. The read-only GET writes nothing on any fault.

SETTINGS_WRITERS = ("set_text", "set_text_if_blank", "set_int", "set_bool", "increment", "delete")


@pytest.fixture
def broken_status_sink(monkeypatch):
    """stderr fails for the status hooks' own events only; the Janitor's logging is untouched."""
    from lib import log as log_module
    real = log_module.log

    def sink(event, detail=""):
        if event.startswith("hotseatStatus"):
            raise BrokenPipeError(32, "Broken pipe " + SENTINEL_ERROR)
        return real(event, detail)
    monkeypatch.setattr(log_module, "log", sink)
    monkeypatch.setattr(status, "log", sink)


@pytest.fixture
def status_eventlog(monkeypatch):
    """Records what the status hooks hand to the structured eventlog; `fail` makes that fail too."""
    from lib import log as log_module
    real = log_module._eventlog_emit
    seen, mode = [], {"fail": False}

    def emit(event, level, detail):
        if event.startswith("hotseatStatus"):
            if mode["fail"]:
                raise OSError(5, "eventlog down " + SENTINEL_PATH)
            seen.append((event, level, detail))
            return None
        return real(event, level, detail)
    monkeypatch.setattr(log_module, "_eventlog_emit", emit)
    return seen, mode


def _fail_store(monkeypatch, *, keys):
    real = settings_store.set_text

    def set_text(key, value, **kwargs):
        if any(key.startswith(k) for k in keys):
            raise OSError(28, "No space left on device " + SENTINEL_PATH)
        return real(key, value, **kwargs)
    monkeypatch.setattr(settings_store, "set_text", set_text)


@pytest.mark.parametrize("eventlog", ["returns", "raises"])
def test_diagnose_reports_an_attempt_never_a_delivery(monkeypatch, eventlog):
    """Contract-review C4: after a log-sink failure the eventlog fallback is only an attempt (its helper
    swallows its own sink errors), so the result never claims the event was stored or accepted."""
    from lib import log as log_module

    def broken(event, detail=""):
        raise BrokenPipeError(32, "Broken pipe " + SENTINEL_ERROR)
    monkeypatch.setattr(status, "log", broken)
    seen = []

    def emit(event, level, detail):
        if eventlog == "raises":
            raise OSError(5, "down")
        seen.append((event, level, detail))
    monkeypatch.setattr(log_module, "_eventlog_emit", emit)
    result = status.diagnose("hotseatStatusRecordFailed", "site=record_read error=OSError")
    assert result == ("eventlog_attempted" if eventlog == "returns" else "dropped")
    assert result not in ("stored", "accepted", "delivered", "eventlog")
    if eventlog == "returns":
        assert seen == [("hotseatStatusRecordFailed", "warning",
                         "site=record_read error=OSError sink_error=BrokenPipeError")]


def _codes_only(seen):
    blob = json.dumps(seen)
    assert seen and "SENTINEL" not in blob and "Broken pipe" not in blob and "/home/" not in blob
    return blob


def _no_diagnostic_state():
    assert [k for k in settings_store.keys_with_prefix("hotseat-status.") if "diagnostic" in k] == []


def _read_accounts_with(monkeypatch, stdout):
    from lib import janitor_hotseat

    class Proc:
        returncode = 0
        stderr = ""
    Proc.stdout = stdout
    monkeypatch.setattr(janitor_hotseat.subprocess, "run", lambda *_a, **_k: Proc())
    return janitor_hotseat.read_accounts("hotseat", "claude")


def test_read_accounts_survives_a_persistence_failure_and_a_broken_diagnostic_sink(monkeypatch, broken_status_sink,
                                                                                    status_eventlog):
    from lib import janitor_hotseat
    seen, _ = status_eventlog
    payload = claude(claude_row("a", active=True, used_5h=0.5))
    _fail_store(monkeypatch, keys=("hotseat-status.",))
    assert _read_accounts_with(monkeypatch, json.dumps(payload)) == janitor_hotseat.parse_claude(payload)
    assert settings_store.keys_with_prefix("hotseat-status.") in ([], ["hotseat-status.salt.v1"])
    blob = _codes_only(seen)
    assert "site=record_read" in blob and "OSError" in blob and "sink_error=BrokenPipeError" in blob


def test_read_accounts_survives_a_rejected_payload_and_a_broken_diagnostic_sink(monkeypatch, broken_status_sink,
                                                                                status_eventlog):
    from lib import janitor_hotseat
    seen, _ = status_eventlog
    assert _read_accounts_with(monkeypatch, "[]") == janitor_hotseat.parse_claude([]) == []
    snapshot = json.loads(settings_store.get(status.SNAPSHOT_PREFIX + "claude"))
    assert (snapshot["read_state"], snapshot["error_code"]) == ("failed", "parse_failed")
    blob = _codes_only(seen)
    assert "code=parse_failed" in blob and "sink_error=BrokenPipeError" in blob
    _no_diagnostic_state()


def test_read_accounts_survives_when_every_diagnostic_channel_fails(monkeypatch, broken_status_sink, status_eventlog):
    from lib import janitor_hotseat
    _, mode = status_eventlog
    mode["fail"] = True
    payload = claude(claude_row("a", active=True, used_5h=0.5))
    _fail_store(monkeypatch, keys=(status.SNAPSHOT_PREFIX,))
    assert _read_accounts_with(monkeypatch, json.dumps(payload)) == janitor_hotseat.parse_claude(payload)
    _no_diagnostic_state()


def _switcher(monkeypatch):
    from lib import agents, janitor_builtins, janitors
    from lib import janitor_autonomy as service
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    agents.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    cfg = janitors.create("hotseat", template_id="account-hotseat")
    owner = janitors.set_enabled("hotseat", cfg["revision"], True)
    service.setup()
    return service, owner


@pytest.mark.parametrize("fallback", ["works", "fails"])
def test_receipt_failures_with_a_broken_diagnostic_sink_leave_the_switcher_unchanged(monkeypatch, broken_status_sink,
                                                                                       status_eventlog, fallback):
    from lib import janitor_hotseat, janitors
    seen, mode = status_eventlog
    mode["fail"] = fallback == "fails"
    service, owner = _switcher(monkeypatch)

    def explode(*_a, **_k):
        raise RuntimeError(SENTINEL_ERROR)
    monkeypatch.setattr(status, "record_action", explode)
    monkeypatch.setattr(status, "record_failure", explode)
    switches, sent = [], []

    def read(command, name):
        if name == "codex":
            raise RuntimeError("hotseat exited 1")
        return janitor_hotseat.parse_claude(claude(claude_row("a", active=True, used_5h=0.9),
                                                   claude_row("b", used_5h=0.1)))
    worker = service.AutonomyJanitors(lambda *_: True, lambda p: sent.append(p) or {"sent": 1}, hotseat_read=read,
                                      hotseat_switch=lambda c, p, alias: switches.append(alias) or True)
    worker.hotseat_once()
    assert switches == ["b"] and len(sent) == 1 and sent[0]["preview"].startswith("Claude: switched to b")
    assert sorted(r["outcome"] for r in janitors.list_runs(owner["session"])) == ["completed", "failed"]
    for lane in ("claude", "codex"):
        state = json.loads(settings_store.get_text(f"janitor-adaptive.hotseat-switcher.{lane}", default="") or "{}")
        assert state.get("next_run_at", 0) > 0
    _no_diagnostic_state()
    if fallback == "works":
        blob = _codes_only(seen)
        assert "site=action" in blob and "site=read_failure" in blob and "RuntimeError" in blob
    else:
        assert seen == []


def test_the_read_only_route_writes_nothing_even_on_a_log_fault(monkeypatch, no_processes):
    """A corrupt stored snapshot, a registry fault and a broken log sink: GET still answers from what
    is stored and never writes settings/state."""
    from lib import janitor_store
    from lib import log as log_module
    status.record_read("claude", claude(claude_row("a", active=True)), T0)
    settings_store.set_text(status.SNAPSHOT_PREFIX + "codex", "{not json")

    def registry_fault(*_a, **_k):
        raise OSError(5, "registry unreadable " + SENTINEL_PATH)
    monkeypatch.setattr(janitor_store, "janitor_agent_ids", registry_fault)
    real_log = log_module.log

    def sink(event, detail=""):
        if event.startswith("hotseatStatus"):
            raise BrokenPipeError(32, "Broken pipe " + SENTINEL_ERROR)
        return real_log(event, detail)
    monkeypatch.setattr(log_module, "log", sink)
    monkeypatch.setattr(status, "log", sink)
    seen = []
    monkeypatch.setattr(log_module, "_eventlog_emit", lambda event, level, detail: seen.append((event, level, detail)))

    def refuse(name):
        def write(*_a, **_k):
            pytest.fail(f"GET wrote settings via {name}")
        return write
    for writer in SETTINGS_WRITERS:
        monkeypatch.setattr(settings_store, writer, refuse(writer))
    handler = _handler("/hotseat/status")  # the authenticated route, not just the builder
    handler._authorized = lambda: True
    handler.do_GET()
    [(code, body)] = handler.sent
    assert code == 200
    served = json.loads(body)
    assert served["schema"] == "clarp.hotseat-status/1" and served["switcher"]["enabled"] is False
    assert provider(served, "claude")["accounts"][0]["alias"] == "a"
    assert provider(served, "codex")["read_state"] == "never"
    blob = _codes_only([s for s in seen if s[0].startswith("hotseatStatus")])
    assert "site=load" in blob and "site=janitor_enabled" in blob and "sink_error=BrokenPipeError" in blob
