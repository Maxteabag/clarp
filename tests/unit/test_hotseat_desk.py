"""Tests for the Hotseat TUI desk (`lib.hotseat_desk` and `/hotseat/*` routes)."""
from __future__ import annotations

import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

import server as host
from lib import agents as agents_db
from lib import hotseat_desk as desk
from lib import hotseat_status as status
from lib import janitor_autonomy, janitor_builtins, janitors, settings_store, turn_queue
from lib.protocol import AgentState

T0_MS = 1_800_000_000_000
FUTURE_S = T0_MS / 1000 + 4 * 3600
SOON_RESET_S = T0_MS / 1000 + 300


def _fake_runner_factory(tui_payload: dict, list_payload: dict, work_payload: dict, *, calls: list[list[str]] | None = None, barrier: threading.Barrier | None = None):
    def runner(argv, **_kwargs):
        args = list(argv[1:])
        if calls is not None:
            calls.append(args)
        if args == ["tui", "--snapshot"]:
            if barrier is not None:
                barrier.wait(timeout=5)
            out = json.dumps(tui_payload)
        elif args == ["list", "--json"]:
            out = json.dumps(list_payload)
        elif args == ["bridge", "work"]:
            out = json.dumps(work_payload)
        else:
            raise AssertionError(f"unexpected hotseat args: {args}")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    return runner


def _sample_payloads():
    tui_payload = {
        "generated_at": T0_MS / 1000,
        "sessions": 3,
        "errors": [],
        "accounts": [
            {
                "provider": "claude",
                "alias": "ai13",
                "display_alias": "ai13",
                "aliases": ["ai13"],
                "email": "ai13@example.invalid",
                "plan": "Team 5×",
                "rate_limit_tier": "default_claude_max_5x",
                "workspace": "ExampleOrg",
                "active": True,
                "saved": True,
                "can_switch": True,
                "can_launch": True,
                "limited": False,
                "cached": False,
                "warning": "",
                "profile_notes": [],
                "reset_credits": None,
                "reset_credits_error": "",
                "windows": [
                    {"label": "All models · 5-hour", "used": 0.25, "reset": FUTURE_S},
                    {"label": "All models · weekly", "used": 0.4, "reset": FUTURE_S},
                    {"label": "Fable · weekly", "used": 0.6, "reset": FUTURE_S},
                ],
                "error": "",
                "checked_at": T0_MS / 1000,
                "no_reset_reason": "",
            },
            {
                "provider": "claude",
                "alias": "ai8",
                "display_alias": "ai8",
                "aliases": ["ai8"],
                "email": "ai8@example.invalid",
                "plan": "Team 5×",
                "rate_limit_tier": "default_claude_max_5x",
                "workspace": "ExampleOrg",
                "active": False,
                "saved": True,
                "can_switch": True,
                "can_launch": True,
                "limited": False,
                "cached": False,
                "warning": "",
                "profile_notes": ["Access token expired; sign-in needed"],
                "reset_credits": None,
                "reset_credits_error": "",
                "windows": [],
                "error": "Access token expired; refresh token revoked, sign-in needed",
                "checked_at": 0,
                "no_reset_reason": "",
            },
            {
                "provider": "codex",
                "alias": "codex-main",
                "display_alias": "codex-main",
                "aliases": ["codex-main"],
                "email": "codex@example.invalid",
                "plan": "self_serve_business_prolite",
                "rate_limit_tier": "",
                "workspace": "ws-123",
                "active": True,
                "saved": True,
                "can_switch": True,
                "can_launch": True,
                "limited": False,
                "cached": False,
                "warning": "",
                "profile_notes": ["OpenAI reports no separate Spark allowance for this account."],
                "reset_credits": 2,
                "reset_credits_error": "",
                "windows": [
                    {"label": "codex primary weekly", "used": 0.1, "reset": FUTURE_S},
                ],
                "error": "",
                "checked_at": T0_MS / 1000,
                "no_reset_reason": "",
            },
        ],
    }
    list_payload = {
        "generated_at": T0_MS / 1000,
        "accounts": [
            {
                "alias": "ai13",
                "email": "ai13@example.invalid",
                "org": "ExampleOrg",
                "plan": "team",
                "plan_label": "Team 5×",
                "rate_limit_tier": "default_claude_max_5x",
                "is_active": True,
                "access_expires_at": T0_MS + 6 * 3600 * 1000,
                "refresh_expires_at": T0_MS + 25 * 86400 * 1000,
                "access_hours_left": 6.0,
                "signin_days_left": 25.0,
                "signin_due_soon": False,
                "error": None,
                "usage": {
                    "status": "allowed",
                    "available": True,
                    "limited": False,
                    "used_5h": 0.25,
                    "reset_5h": FUTURE_S,
                    "used_7d": 0.4,
                    "reset_7d": FUTURE_S,
                    "scoped": [{"model": "Fable", "used": 0.6, "reset": FUTURE_S, "exhausted": False}],
                    "blocked_models": [],
                    "extra_usage_enabled": False,
                    "extra_usage_reason": "out_of_credits",
                },
            },
            {
                "alias": "ai8",
                "email": "ai8@example.invalid",
                "org": "ExampleOrg",
                "plan": "team",
                "plan_label": "Team 5×",
                "is_active": False,
                "error": "Access token expired",
                "error_code": "auth_error",
                "usage": None,
            },
        ],
        "codex": {
            "quota_read": True,
            "quota_age_s": 0,
            "accounts": [
                {
                    "provider": "codex",
                    "alias": "codex-main",
                    "is_active": True,
                    "saved": True,
                    "email": "codex@example.invalid",
                    "plan": "plus",
                    "account_id": "ws-123",
                    "token_expires_at": int(FUTURE_S),
                    "token_hours_left": 4.0,
                    "api_key_present": False,
                    "usage": {
                        "usable": True,
                        "blocked": False,
                        "error": None,
                        "worst_used": 0.1,
                        "windows": [{"label": "codex primary weekly", "used": 0.1, "reset": FUTURE_S}],
                    },
                }
            ],
        },
        "extensions": {
            "clarp": {
                "agents": {
                    "total": 12,
                    "live": 2,
                    "claude_backed": 10,
                    "by_backend": {"claude": 10, "codex": 2},
                    "live_by_account": {"ai13": 2},
                    "agents": [
                        {"session": "alpha", "would_use": "ai13"},
                        {"session": "beta", "would_use": "ai13"},
                        {"session": "gamma", "would_use": "ai8"},
                    ],
                },
                "stopped": [
                    {
                        "kind": "clarp",
                        "cause": "usage_limit",
                        "pending": 0,
                        "id": "koko",
                        "name": "Koko",
                        "backend": "claude",
                        "model": "claude-fable-4-7",
                        "cwd": "/work/repo",
                        "last_message": "Hit usage limit",
                        "stopped_at": T0_MS / 1000 - 120,
                        "readiness": {"ready": True, "hard": None, "warning": "ai13 could serve it"},
                    }
                ],
            }
        },
    }
    work_payload = {
        "items": [
            {
                "id": "thread-001",
                "provider": "codex",
                "title": "Fix parser",
                "state": "failed",
                "updated": T0_MS / 1000 - 60,
                "cwd": "/work/repo",
                "live": False,
                "can_resume": True,
                "can_reboot": False,
                "reason": "",
            }
        ]
    }
    return tui_payload, list_payload, work_payload


def test_refresh_merges_tui_list_and_work_and_syncs_hotseat_status(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    calls: list[list[str]] = []
    tui_p, list_p, work_p = _sample_payloads()
    runner = _fake_runner_factory(tui_p, list_p, work_p, calls=calls)

    d = desk.HotseatDesk()
    result = d.payload(refresh=True, command="hotseat", which=lambda _: "/usr/bin/hotseat", runner=runner)
    assert result["schema"] == "clarp.hotseat-desk/1"
    assert result["refresh_throttled"] is False
    assert result["next_refresh_at"] == T0_MS + desk.MIN_REFRESH_INTERVAL_MS
    assert result["stale"] is False
    assert result["error"] is None
    assert result["sessions"] == 3
    assert result["clarp_agents"]["live_by_account"] == {"ai13": 2}
    assert result["clarp_agents"]["would_use_by_account"] == {"ai13": 2, "ai8": 1}

    by_alias = {a["alias"]: a for a in result["accounts"]}
    ai13 = by_alias["ai13"]
    assert ai13["active"] is True
    assert ai13["email"] == "ai13@example.invalid"
    assert ai13["workspace"] == "ExampleOrg"
    assert ai13["access_hours_left"] == 6.0
    assert ai13["signin_days_left"] == 25.0
    assert ai13["live_sessions"] == 2
    assert ai13["would_use_sessions"] == 2
    assert [w["short_label"] for w in ai13["windows"]] == ["5h", "Weekly", "Fable"]
    assert [w["remaining_ratio"] for w in ai13["windows"]] == [0.75, 0.6, 0.4]

    ai8 = by_alias["ai8"]
    assert ai8["state"] == "auth_error"
    assert ai8["error_code"] == "auth_error"
    assert ai8["windows"] == []

    codex = by_alias["codex-main"]
    assert codex["reset_credits"] == 2
    assert codex["token_hours_left"] == 4.0
    assert codex["windows"][0]["remaining_ratio"] == 0.9

    assert len(result["stopped_work"]) == 1
    assert result["stopped_work"][0]["id"] == "koko"
    assert result["stopped_work"][0]["readiness"]["ready"] is True
    assert result["recent_work"][0]["resume_command"] == "codex resume thread-001"

    # Cached read (`refresh=False`) must NOT run any subprocess.
    calls.clear()
    cached = d.payload(
        refresh=False,
        served_at=T0_MS + 5_000,
        command="hotseat",
        which=lambda _: "/usr/bin/hotseat",
        runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("refresh=False must not run CLI")),
    )
    assert calls == []
    assert cached["stale"] is False
    assert len(cached["accounts"]) == 3

    # `hotseat_status.payload()` was also updated by the live refresh.
    status_body = status.payload(served_at=T0_MS + 5_000)
    claude_prov = next(p for p in status_body["providers"] if p["provider"] == "claude")
    assert claude_prov["inventory"] == "ok"
    assert any(a["alias"] == "ai13" and a["active_default"] for a in claude_prov["accounts"])


def test_expired_reset_window_turns_unknown_instead_of_exhausted(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    tui_p = {
        "generated_at": T0_MS / 1000,
        "sessions": 0,
        "errors": [],
        "accounts": [
            {
                "provider": "claude",
                "alias": "ai1",
                "active": False,
                "saved": True,
                "can_switch": True,
                "limited": True,
                "windows": [{"label": "All models · 5-hour", "used": 1.0, "reset": SOON_RESET_S}],
                "error": "",
                "checked_at": T0_MS / 1000,
                "no_reset_reason": "",
            },
            {
                "provider": "codex",
                "alias": "no-credits-acct",
                "active": False,
                "saved": True,
                "can_switch": True,
                "limited": True,
                "windows": [{"label": "codex primary weekly", "used": 1.0, "reset": SOON_RESET_S}],
                "error": "",
                "checked_at": T0_MS / 1000,
                "no_reset_reason": "no credits",
            },
        ],
    }
    runner = _fake_runner_factory(tui_p, {"accounts": []}, {"items": []})
    d = desk.HotseatDesk()
    before = d.payload(refresh=True, command="hotseat", which=lambda _: "/usr/bin/hotseat", runner=runner)
    assert before["accounts"][0]["state"] == "exhausted"
    assert before["accounts"][0]["windows"][0]["remaining_ratio"] == 0.0

    # Serve after SOON_RESET_S has passed without a new refresh: ai1 becomes unknown (not 0%),
    # whereas no-credits-acct stays exhausted because no_reset_reason blocks reset recovery.
    after_ms = int(SOON_RESET_S * 1000) + 10_000
    after = d.payload(refresh=False, served_at=after_ms, command="hotseat", which=lambda _: "/usr/bin/hotseat")
    ai1 = next(a for a in after["accounts"] if a["alias"] == "ai1")
    assert ai1["state"] == "unknown"
    assert ai1["limited"] is False
    assert ai1["windows"][0]["used_ratio"] is None
    assert ai1["windows"][0]["remaining_ratio"] is None
    assert ai1["windows"][0]["stale"] is True

    locked = next(a for a in after["accounts"] if a["alias"] == "no-credits-acct")
    assert locked["state"] == "exhausted"
    assert locked["windows"][0]["remaining_ratio"] == 0.0


def test_concurrent_refresh_is_single_flight(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    tui_calls = 0
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    tui_p, list_p, work_p = _sample_payloads()

    def slow_runner(argv, **_kwargs):
        nonlocal tui_calls
        args = list(argv[1:])
        if args == ["tui", "--snapshot"]:
            with lock:
                tui_calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(tui_p), stderr="")
        if args == ["list", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(list_p), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(work_p), stderr="")

    d = desk.HotseatDesk()
    results: list[dict] = []

    def read():
        results.append(d.payload(refresh=True, command="hotseat", runner=slow_runner))

    t1 = threading.Thread(target=read)
    t1.start()
    assert entered.wait(timeout=5)
    assert d.payload(command="hotseat")["refreshing"] is True

    t2 = threading.Thread(target=read)
    t2.start()
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert tui_calls == 1
    assert len(results) == 2
    assert all(len(r["accounts"]) == 3 for r in results)
    assert d.payload(command="hotseat")["refreshing"] is False


# --- account state: account limits and model limits are different facts -------

def _claude_row(alias, *, five_hour, weekly, fable, limited=False, active=False, extra_usage=False):
    """A Claude account as `tui --snapshot` and `list --json` report it, labels as Hotseat writes them."""
    tui_row = {
        "provider": "claude", "alias": alias, "display_alias": alias, "aliases": [alias], "active": active,
        "saved": True, "can_switch": True, "can_launch": True, "limited": limited, "cached": False,
        "windows": [{"label": "All models · 5-hour", "used": five_hour, "reset": FUTURE_S},
                    {"label": "All models · weekly", "used": weekly, "reset": FUTURE_S},
                    {"label": "Fable · weekly", "used": fable, "reset": FUTURE_S}],
        "error": "", "checked_at": T0_MS / 1000, "no_reset_reason": "",
    }
    list_row = {
        "alias": alias, "is_active": active, "error": None,
        "usage": {"status": "allowed", "available": True, "limited": limited,
                  "used_5h": five_hour, "reset_5h": FUTURE_S, "used_7d": weekly, "reset_7d": FUTURE_S,
                  "scoped": [{"model": "Fable", "used": fable, "reset": FUTURE_S, "exhausted": fable >= 1.0}],
                  "blocked_models": ["Fable"] if fable >= 1.0 else [],
                  "extra_usage_enabled": extra_usage},
    }
    return tui_row, list_row


def _codex_row(alias, windows, *, blocked=False, no_reset_reason=""):
    tui_row = {"provider": "codex", "alias": alias, "active": False, "saved": True, "can_switch": True,
               "limited": False, "windows": [{"label": label, "used": used, "reset": FUTURE_S}
                                             for label, used in windows],
               "error": "", "checked_at": T0_MS / 1000, "no_reset_reason": no_reset_reason}
    usage = {"usable": not blocked, "blocked": blocked, "error": None,
             "windows": [{"label": label, "used": used, "reset": FUTURE_S} for label, used in windows]}
    if no_reset_reason:
        usage["no_reset_reason"] = no_reset_reason  # Hotseat omits the key when it is empty
    list_row = {"provider": "codex", "alias": alias, "is_active": False, "usage": usage}
    return tui_row, list_row


def _desk_of(claude=(), codex=(), monkeypatch=None):
    tui_p = {"sessions": 0, "errors": [], "accounts": [t for t, _ in (*claude, *codex)]}
    list_p = {"accounts": [l for _, l in claude], "codex": {"quota_read": True, "accounts": [l for _, l in codex]}}
    d = desk.HotseatDesk()
    body = d.payload(refresh=True, command="hotseat", runner=_fake_runner_factory(tui_p, list_p, {"items": []}))
    return d, {a["alias"]: a for a in body["accounts"]}


def test_a_full_model_window_does_not_make_the_account_exhausted(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    _d, accounts = _desk_of(claude=[
        _claude_row("main", five_hour=0.42, weekly=0.30, fable=1.0, active=True),
        _claude_row("spent", five_hour=1.0, weekly=0.30, fable=0.10, limited=True),
        _claude_row("weekly-out", five_hour=0.05, weekly=1.0, fable=0.10),
        _claude_row("overage", five_hour=1.0, weekly=0.30, fable=0.10, extra_usage=True),
    ])

    main = accounts["main"]
    assert (main["state"], main["error_code"], main["limited"]) == ("ok", None, False)
    windows = {w["kind"]: w for w in main["windows"]}
    assert (windows["5h"]["remaining_ratio"], windows["7d"]["remaining_ratio"]) == (0.58, 0.7)
    # The model limit stays in the payload as its own fact: which model, and until when.
    assert windows["model:fable"]["label"] == "Fable · weekly" and windows["model:fable"]["short_label"] == "Fable"
    assert (windows["model:fable"]["used_ratio"], windows["model:fable"]["remaining_ratio"]) == (1.0, 0.0)
    assert windows["model:fable"]["resets_at"] == int(FUTURE_S * 1000)
    assert main["blocked_models"] == ["Fable"]

    assert (accounts["spent"]["state"], accounts["spent"]["error_code"]) == ("exhausted", "exhausted")
    assert accounts["weekly-out"]["state"] == "exhausted", "an account window at its limit, without the flag"
    assert accounts["overage"]["state"] == "ok", "a full 5-hour window Hotseat says extra usage covers"


def test_codex_is_exhausted_by_its_own_windows_or_hotseats_block_not_by_a_model_limit(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    _d, accounts = _desk_of(codex=[
        _codex_row("month-out", [("codex primary weekly", 0.20), ("codex primary 30-day", 1.0)], blocked=True),
        _codex_row("spark-out", [("codex primary weekly", 0.20), ("codex_spark primary weekly", 1.0)]),
        _codex_row("no-flag", [("codex primary weekly", 1.0)]),
        _codex_row("other-limit", [("codex primary weekly", 0.20), ("gpt-5.3-codex primary weekly", 1.0)]),
    ])

    assert accounts["month-out"]["state"] == "exhausted"
    assert [w["kind"] for w in accounts["month-out"]["windows"]] == ["7d", "30d"]
    assert (accounts["spark-out"]["state"], accounts["spark-out"]["limited"]) == ("ok", False)
    spark = next(w for w in accounts["spark-out"]["windows"] if w["kind"] == "model:spark")
    assert (spark["remaining_ratio"], spark["resets_at"]) == (0.0, int(FUTURE_S * 1000))
    assert accounts["no-flag"]["state"] == "exhausted"
    # A limit that is not the provider's own is not an account window, whatever its span.
    other = accounts["other-limit"]
    assert other["state"] == "ok"
    assert [w["kind"] in desk.ACCOUNT_WINDOW_KINDS for w in other["windows"]] == [True, False]
    assert other["windows"][1]["remaining_ratio"] == 0.0


def test_a_passed_account_reset_is_not_kept_exhausted_by_a_full_model_window(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    tui_row, list_row = _claude_row("main", five_hour=1.0, weekly=0.30, fable=1.0, limited=True)
    tui_row["windows"][0]["reset"] = SOON_RESET_S
    d, accounts = _desk_of(claude=[(tui_row, list_row)])
    assert accounts["main"]["state"] == "exhausted"

    later = d.payload(served_at=int(SOON_RESET_S * 1000) + 10_000, command="hotseat")["accounts"][0]

    assert (later["state"], later["limited"], later["error_code"]) == ("unknown", False, None)
    windows = {w["kind"]: w for w in later["windows"]}
    assert windows["5h"]["used_ratio"] is None and windows["5h"]["stale"] is True
    assert windows["model:fable"]["used_ratio"] == 1.0, "the model limit is still there, as itself"


# --- live reads: a minimum interval ------------------------------------------

def test_a_live_read_inside_the_interval_serves_the_stored_snapshot(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    calls: list[list[str]] = []
    runner = _fake_runner_factory(*_sample_payloads(), calls=calls)
    d = desk.HotseatDesk()

    first = d.payload(refresh=True, command="hotseat", runner=runner)
    clock["now"] = T0_MS + desk.MIN_REFRESH_INTERVAL_MS - 1
    held = d.payload(refresh=True, command="hotseat", runner=runner)

    assert len(calls) == 3, "one live read is three CLI calls; the second request ran none"
    assert first["refresh_throttled"] is False and held["refresh_throttled"] is True
    assert held["observed_at"] == T0_MS and len(held["accounts"]) == 3
    assert held["next_refresh_at"] == first["next_refresh_at"] == T0_MS + desk.MIN_REFRESH_INTERVAL_MS
    assert d.payload(command="hotseat")["refresh_throttled"] is False, "only a request that asked is throttled"

    clock["now"] = held["next_refresh_at"]
    allowed = d.payload(refresh=True, command="hotseat", runner=runner)
    assert len(calls) == 6 and allowed["refresh_throttled"] is False
    assert allowed["observed_at"] == clock["now"]


def test_a_failed_live_read_also_starts_the_interval(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    attempts: list[int] = []

    def broken(argv, **_kwargs):
        attempts.append(1)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    d = desk.HotseatDesk()
    failed = d.payload(refresh=True, command="hotseat", runner=broken)
    ran = len(attempts)
    again = d.payload(refresh=True, command="hotseat", runner=broken)

    assert failed["error"] == "cli_failed" and failed["refresh_throttled"] is False
    assert again["refresh_throttled"] is True and len(attempts) == ran
    assert again["next_refresh_at"] == T0_MS + desk.MIN_REFRESH_INTERVAL_MS


def test_the_interval_survives_a_restart_of_the_host(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    calls: list[list[str]] = []
    runner = _fake_runner_factory(*_sample_payloads(), calls=calls)
    desk.HotseatDesk().payload(refresh=True, command="hotseat", runner=runner)

    clock["now"] = T0_MS + 5_000
    restarted = desk.HotseatDesk().payload(refresh=True, command="hotseat", runner=runner)

    assert len(calls) == 3 and restarted["refresh_throttled"] is True
    assert restarted["next_refresh_at"] == T0_MS + desk.MIN_REFRESH_INTERVAL_MS


# --- the switcher's scheduled read keeps the stored desk fresh ------------------

MINUTE = 60_000


def _lists(claude=(), codex=(), stopped=None, agents=None):
    """(tui, list, codex) payloads of one moment, consistent as Hotseat's own are."""
    tui_p = {"sessions": 2, "errors": [], "accounts": [t for t, _ in (*claude, *codex)]}
    list_p = {"accounts": [l for _, l in claude],
              "codex": {"quota_read": True, "accounts": [l for _, l in codex]}}
    if stopped is not None or agents is not None:
        list_p["extensions"] = {"clarp": {"agents": agents or {"total": 0, "live": 0}, "stopped": stopped or []}}
    return tui_p, list_p, {"accounts": [l for _, l in codex]}


def _stop(session, stopped_at_s, ready=True):
    return {"kind": "clarp", "cause": "usage_limit", "id": session, "name": session, "backend": "claude",
            "stopped_at": stopped_at_s, "readiness": {"ready": ready, "hard": None, "warning": None}}


def _live(d, tui_p, list_p):
    return d.payload(refresh=True, command="hotseat",
                     runner=_fake_runner_factory(tui_p, list_p, {"items": [{"id": "t1", "provider": "codex"}]}))


def _no_cli(*_a, **_k):
    raise AssertionError("a plain read of the desk runs nothing")


def test_the_switchers_read_refreshes_the_stored_desk_without_a_live_read(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    main = _claude_row("main", five_hour=0.20, weekly=0.30, fable=0.50, active=True)
    main[0].update(reset_credits=3, display_alias="Main", email="main@example.invalid", plan="Team 5×")
    spare = _claude_row("spare", five_hour=0.10, weekly=0.10, fable=0.10)
    cx = _codex_row("cx", [("codex primary weekly", 0.25)])
    tui_p, list_p, _ = _lists([main, spare], [cx], stopped=[_stop("koko", T0_MS / 1000 - 60)],
                              agents={"total": 4, "live": 1, "live_by_account": {"main": 1}})
    d = desk.HotseatDesk()
    _live(d, tui_p, list_p)

    # Eleven minutes on with nobody refreshing: the stored desk is stale, as before.
    clock["now"] = T0_MS + 11 * MINUTE
    assert d.payload(command="hotseat", runner=_no_cli)["stale"] is True

    # The switcher's scheduled `hotseat list --json`: main went on working and hit
    # Fable's limit, koko was continued and luna stopped, the default moved to spare.
    later = [_claude_row("main", five_hour=0.60, weekly=0.35, fable=1.0),
             _claude_row("spare", five_hour=0.15, weekly=0.10, fable=0.10, active=True)]
    _, switcher_list, _ = _lists(later, [], stopped=[_stop("luna", clock["now"] / 1000 - 30)],
                                 agents={"total": 4, "live": 2, "live_by_account": {"spare": 2}})
    assert d.absorb_switcher_read("claude", switcher_list, started_at=clock["now"]) is True

    clock["now"] += 1000
    body = d.payload(command="hotseat", runner=_no_cli)
    accounts = {a["alias"]: a for a in body["accounts"]}
    assert body["stale"] is False and body["observed_at"] == T0_MS + 11 * MINUTE
    main_now = accounts["main"]
    assert (main_now["state"], main_now["active"], main_now["blocked_models"]) == ("ok", False, ["Fable"])
    assert {w["kind"]: w["remaining_ratio"] for w in main_now["windows"]} == {"5h": 0.4, "7d": 0.65, "model:fable": 0.0}
    assert all(w["observed_at"] == body["observed_at"] and w["stale"] is False for w in main_now["windows"])
    assert main_now["checked_at"] == body["observed_at"] and main_now["live_sessions"] == 0
    assert (accounts["spare"]["active"], accounts["spare"]["live_sessions"]) == (True, 2)
    # What only `tui --snapshot` knows stays as the last live read left it.
    assert (main_now["reset_credits"], main_now["display_alias"], main_now["email"], main_now["plan"],
            main_now["can_switch"]) == (3, "Main", "main@example.invalid", "Team 5×", True)
    assert [item["id"] for item in body["stopped_work"]] == ["luna"]
    assert body["clarp_agents"]["live_by_account"] == {"spare": 2}
    assert [item["id"] for item in body["recent_work"]] == ["t1"] and body["sessions"] == 2
    # Codex was not in this read: its row keeps its own age and says so, window by window.
    assert accounts["cx"]["checked_at"] == T0_MS
    assert [(w["observed_at"], w["stale"]) for w in accounts["cx"]["windows"]] == [(T0_MS, True)]
    # The minimum interval for a client's live read counts from this read too.
    assert body["next_refresh_at"] == body["observed_at"] + desk.MIN_REFRESH_INTERVAL_MS


def test_a_desk_refreshed_by_the_switcher_equals_one_read_live_at_the_same_moment(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    old = [_claude_row("main", five_hour=0.20, weekly=0.30, fable=0.50, active=True),
           _claude_row("spare", five_hour=0.10, weekly=0.10, fable=0.10),
           _claude_row("spent", five_hour=0.80, weekly=0.10, fable=0.10)]
    old_cx = [_codex_row("cx", [("codex primary weekly", 0.25), ("codex primary 30-day", 0.5)])]
    new = [_claude_row("main", five_hour=0.60, weekly=0.35, fable=1.0),
           _claude_row("spare", five_hour=0.15, weekly=0.10, fable=0.10, active=True),
           _claude_row("spent", five_hour=1.0, weekly=0.10, fable=0.10, limited=True)]
    new_cx = [_codex_row("cx", [("codex primary weekly", 0.40), ("codex primary 30-day", 1.0)], blocked=True)]
    stopped = [_stop("koko", T0_MS / 1000 - 60, ready=False)]
    agents = {"total": 3, "live": 1, "live_by_account": {"spare": 1}, "agents": [{"would_use": "spare"}]}

    live = desk.HotseatDesk()
    expected = _live(live, *_lists(new, new_cx, stopped=stopped, agents=agents)[:2])

    settings_store.set_text(desk.SNAPSHOT_KEY, "")
    absorbed = desk.HotseatDesk()
    _live(absorbed, *_lists(old, old_cx, stopped=[], agents={"total": 3, "live": 0})[:2])
    _, new_list, new_codex = _lists(new, new_cx, stopped=stopped, agents=agents)
    assert absorbed.absorb_switcher_read("claude", new_list, started_at=T0_MS)
    assert absorbed.absorb_switcher_read("codex", new_codex, started_at=T0_MS)
    got = absorbed.payload(command="hotseat", runner=_no_cli)

    for key in ("accounts", "stopped_work", "clarp_agents", "observed_at", "stale"):
        assert got[key] == expected[key], key


def test_the_switchers_codex_read_refreshes_only_codex_rows(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    d = desk.HotseatDesk()
    tui_p, list_p, _ = _lists([_claude_row("main", five_hour=0.2, weekly=0.3, fable=0.5, active=True)],
                              [_codex_row("cx", [("codex primary weekly", 0.25)])])
    _live(d, tui_p, list_p)
    clock["now"] = T0_MS + 4 * MINUTE
    _, _, codex_read = _lists([], [_codex_row("cx", [("codex primary weekly", 1.0)], blocked=True,
                                              no_reset_reason="no credits")])

    assert d.absorb_switcher_read("codex", codex_read, started_at=clock["now"]) is True

    body = d.payload(command="hotseat", runner=_no_cli)
    accounts = {a["alias"]: a for a in body["accounts"]}
    assert (accounts["cx"]["state"], accounts["cx"]["no_reset_reason"]) == ("exhausted", "no credits")
    assert accounts["cx"]["windows"][0]["observed_at"] == clock["now"]
    assert accounts["main"]["checked_at"] == T0_MS
    assert body["observed_at"] == T0_MS, "only the read that carries the whole account list moves the desk's time"


def test_the_switchers_read_neither_invents_nor_removes_accounts_and_needs_a_stored_desk(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    d = desk.HotseatDesk()
    _, switcher_list, _ = _lists([_claude_row("main", five_hour=0.6, weekly=0.3, fable=0.5, active=True),
                                  _claude_row("brand-new", five_hour=0.0, weekly=0.0, fable=0.0)])
    assert d.absorb_switcher_read("claude", switcher_list, started_at=T0_MS) is False, "no desk has been read yet"
    assert settings_store.get(desk.SNAPSHOT_KEY) in (None, "")

    tui_p, list_p, _ = _lists([_claude_row("main", five_hour=0.2, weekly=0.3, fable=0.5, active=True),
                               _claude_row("only-on-desk", five_hour=0.3, weekly=0.3, fable=0.3)],
                              stopped=[_stop("koko", T0_MS / 1000 - 60)])
    _live(d, tui_p, list_p)
    assert d.absorb_switcher_read("claude", switcher_list, started_at=T0_MS) is True

    body = d.payload(command="hotseat", runner=_no_cli)
    assert [a["alias"] for a in body["accounts"]] == ["main", "only-on-desk"]
    assert body["accounts"][0]["windows"][0]["remaining_ratio"] == 0.4
    assert body["accounts"][1]["windows"][0]["remaining_ratio"] == 0.7
    assert [item["id"] for item in body["stopped_work"]] == ["koko"], (
        "a read without the Clarp extension says nothing about stopped work")


def test_a_read_that_began_before_a_switch_does_not_undo_the_account_in_use(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    d = desk.HotseatDesk()
    rows = [_claude_row("ai13", five_hour=0.2, weekly=0.3, fable=0.5, active=True),
            _claude_row("ai8", five_hour=0.1, weekly=0.1, fable=0.1)]
    tui_p, list_p, _ = _lists(rows)
    _live(d, tui_p, list_p)
    read_started = T0_MS + 1000
    clock["now"] = T0_MS + 2000
    hotseat = _Hotseat("ai13")
    assert hotseat.switch_default(d, "claude", "ai8")[1]["switched"] is True

    clock["now"] = T0_MS + 3000
    assert d.absorb_switcher_read("claude", list_p, started_at=read_started) is True

    accounts = {a["alias"]: a for a in d.payload(command="hotseat", runner=_no_cli)["accounts"]}
    assert (accounts["ai13"]["active"], accounts["ai8"]["active"]) == (False, True)

    clock["now"] = T0_MS + 5 * MINUTE
    _, after, _ = _lists([_claude_row("ai13", five_hour=0.2, weekly=0.3, fable=0.5, active=True),
                          _claude_row("ai8", five_hour=0.1, weekly=0.1, fable=0.1)])
    assert d.absorb_switcher_read("claude", after, started_at=clock["now"]) is True
    accounts = {a["alias"]: a for a in d.payload(command="hotseat", runner=_no_cli)["accounts"]}
    assert accounts["ai13"]["active"] is True, "a read that began after the switch is believed"


def test_the_switchers_read_never_raises_clears_an_old_read_error_and_respects_the_off_switch(monkeypatch):
    clock = {"now": T0_MS}
    monkeypatch.setattr(desk, "now_ms", lambda: clock["now"])
    d = desk.HotseatDesk()
    rows = [_claude_row("main", five_hour=0.2, weekly=0.3, fable=0.5, active=True)]
    tui_p, list_p, _ = _lists(rows)
    _live(d, tui_p, list_p)
    before = settings_store.get(desk.SNAPSHOT_KEY)
    for junk in (None, [], "x", {"accounts": "nope"}, {"accounts": [{"alias": "main", "usage": "?"}, 7]}):
        d.absorb_switcher_read("claude", junk, started_at=T0_MS)
    assert d.absorb_switcher_read("muse", list_p, started_at=T0_MS) is False
    assert json.loads(settings_store.get(desk.SNAPSHOT_KEY))["accounts"][0]["alias"] == "main"

    settings_store.set_text(desk.SNAPSHOT_KEY, before)
    settings_store.set_text(desk.ERROR_KEY, "timeout")
    clock["now"] = T0_MS + MINUTE
    assert d.absorb_switcher_read("claude", list_p, started_at=clock["now"]) is True
    body = d.payload(command="hotseat", runner=_no_cli)
    assert body["error"] is None and body["stale"] is False

    settings_store.set_text(desk.SNAPSHOT_KEY, before)
    _desk_setting(monkeypatch, False)
    assert d.absorb_switcher_read("claude", list_p, started_at=clock["now"]) is False
    assert settings_store.get(desk.SNAPSHOT_KEY) == before


def test_the_switcher_hands_every_scheduled_read_to_the_desk(monkeypatch):
    from lib import janitor_hotseat
    seen: list[tuple] = []
    list_payload = {"accounts": [{"alias": "a", "is_active": True, "error": None,
                                  "usage": {"status": "allowed", "limited": False, "used_5h": 0.1, "used_7d": 0.1}}]}

    def run(command, args, timeout):
        return subprocess.CompletedProcess([command, *args], 0, stdout=json.dumps(list_payload), stderr="")

    monkeypatch.setattr(janitor_hotseat, "_run", run)
    monkeypatch.setattr(janitor_hotseat, "now_ms", lambda: T0_MS, raising=False)

    def observe(provider, payload, *, started_at):
        seen.append((provider, payload, started_at))

    accounts = janitor_hotseat.read_accounts("hotseat", "claude", observe=observe)
    assert accounts[0]["alias"] == "a" and seen == [("claude", list_payload, T0_MS)]

    def broken(provider, payload, *, started_at):
        raise RuntimeError("desk store unavailable")

    assert janitor_hotseat.read_accounts("hotseat", "claude", observe=broken)[0]["alias"] == "a", (
        "the switcher's read never fails because of the desk")
    assert janitor_hotseat.read_accounts("hotseat", "claude")[0]["alias"] == "a"

    worker = janitor_autonomy.AutonomyJanitors(lambda *_: True, lambda p: {"sent": 1}, hotseat_observe=observe)
    seen.clear()
    assert worker.hotseat_read("hotseat", "claude")[0]["alias"] == "a"
    assert [(provider, started) for provider, _payload, started in seen] == [("claude", T0_MS)]


# --- switching -----------------------------------------------------------------

class _Hotseat:
    """Hotseat as the desk drives it: who is active, and a switch that changes it."""

    def __init__(self, active: str = "ai13", *, confirm: bool = True):
        self.active = {"claude": active, "codex": "codex-main"}
        self.confirm = confirm
        self.switches: list[tuple[str, str, str]] = []
        self.reads: list[str] = []
        self.hooks: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def read_active(self, command, provider):
        self.reads.append(provider)
        return self.active[provider]

    def switch(self, command, provider, alias):
        self.switches.append((command, provider, alias))
        self.entered.set()
        assert self.release.wait(timeout=5)
        if self.confirm:
            self.active[provider] = alias
        return self.confirm

    def switch_default(self, d, provider, alias, **kwargs):
        return d.switch_default(
            provider, alias, command="hotseat", switch_fn=self.switch, read_active=self.read_active,
            credentials_changed=self.hooks.append, refresh_after=False, **kwargs)


def _last_switch(provider: str = "claude"):
    raw = settings_store.get(f"hotseat-switcher.{provider}.last-switch")
    return json.loads(raw) if raw else None


def test_switch_validates_before_it_asks_hotseat_anything(monkeypatch):
    hotseat = _Hotseat()
    d = desk.HotseatDesk()

    assert hotseat.switch_default(d, "bogus", "ai12")[0] == 400
    assert hotseat.switch_default(d, "muse", "ai12")[1]["error"] == "invalid_provider"
    for alias in ("--help", "-x", "a b", "a;rm", "../bad", "", "x" * 200):
        status, body = hotseat.switch_default(d, "claude", alias)
        assert (status, body["error"]) == (400, "invalid_alias"), alias
    assert hotseat.reads == [] and hotseat.switches == []


def test_switch_records_the_true_origin_tells_the_backend_and_marks_the_stored_desk(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    runner = _fake_runner_factory(*_sample_payloads())
    d = desk.HotseatDesk()
    d.payload(refresh=True, command="hotseat", runner=runner)
    hotseat = _Hotseat("ai13")

    status, body = hotseat.switch_default(d, "claude", "ai8")

    assert status == 200
    assert {k: body[k] for k in ("ok", "switched", "provider", "alias", "from")} == {
        "ok": True, "switched": True, "provider": "claude", "alias": "ai8", "from": "ai13"}
    assert hotseat.switches == [("hotseat", "claude", "ai8")] and hotseat.hooks == ["claude"]
    assert _last_switch() == {"from": "ai13", "to": "ai8", "at": T0_MS}
    active = {a["alias"]: a["active"] for a in body["desk"]["accounts"] if a["provider"] == "claude"}
    assert active == {"ai13": False, "ai8": True}
    assert body["desk"]["switches"] == [{
        "provider": "claude", "alias": "ai8", "from": "ai13", "state": "switched",
        "requested_at": T0_MS, "finished_at": T0_MS}]
    assert desk.HotseatDesk().payload(command="hotseat")["switches"] == body["desk"]["switches"], (
        "the record of how the switch ended outlives the process")


def test_switch_to_the_active_alias_runs_nothing_and_keeps_the_flip_back_record():
    record = json.dumps({"from": "ai12", "to": "ai13", "at": 1})
    settings_store.set_text("hotseat-switcher.claude.last-switch", record)
    hotseat = _Hotseat("ai13")
    d = desk.HotseatDesk()

    status, body = hotseat.switch_default(d, "claude", "ai13")

    assert status == 200 and body["ok"] is True and body["switched"] is False
    assert body["reason"] == "already_active" and body["from"] == "ai13" and body["message"]
    assert hotseat.switches == [] and hotseat.hooks == []
    assert settings_store.get("hotseat-switcher.claude.last-switch") == record
    assert body["desk"]["switches"][0]["state"] == "already_active"


def test_a_switch_hotseat_does_not_confirm_is_refused_without_a_record_or_a_hook():
    hotseat = _Hotseat("ai13", confirm=False)
    d = desk.HotseatDesk()

    status, body = hotseat.switch_default(d, "claude", "ai12")

    assert status == 409 and body["error"] == "switch_failed" and body["from"] == "ai13"
    assert _last_switch() is None and hotseat.hooks == []
    assert body["desk"]["switches"][0]["state"] == "failed"

    def explode(*_a):
        raise OSError("no hotseat")

    status, body = d.switch_default("claude", "ai12", command="hotseat", switch_fn=explode,
                                    read_active=hotseat.read_active, credentials_changed=hotseat.hooks.append,
                                    refresh_after=False)
    assert status == 409 and body["error"] == "switch_failed" and _last_switch() is None


def test_switch_is_refused_when_the_active_account_cannot_be_read():
    hotseat = _Hotseat()
    d = desk.HotseatDesk()

    def unreadable(command, provider):
        raise RuntimeError("hotseat list failed")

    status, body = d.switch_default("claude", "ai12", command="hotseat", switch_fn=hotseat.switch,
                                    read_active=unreadable, refresh_after=False)

    assert status == 503 and body["error"] == "hotseat_unavailable" and body["message"]
    assert hotseat.switches == [] and _last_switch() is None
    assert body["desk"]["switches"][0]["state"] == "unavailable"


def test_the_active_alias_is_read_from_hotseat_not_from_the_stored_desk():
    seen: list[list[str]] = []

    def runner(argv, **_kwargs):
        seen.append(list(argv[1:]))
        if argv[1:] == ["list", "--json"]:
            out = {"accounts": [{"alias": "a", "is_active": False}, {"alias": "b", "is_active": True}]}
        else:
            out = {"accounts": [{"alias": "p", "is_active": False}, {"alias": "q", "is_active": True},
                                {"alias": "r", "is_active": False}]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")

    assert desk.read_active_alias("hotseat", "claude", runner=runner) == "b"
    assert desk.read_active_alias("hotseat", "codex", runner=runner) == "q"
    assert seen == [["list", "--json"], ["codex", "--no-quota", "--json"]]
    assert desk.read_active_alias(
        "hotseat", "claude",
        runner=lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout='{"accounts": []}', stderr="")) is None
    with pytest.raises(RuntimeError):
        desk.read_active_alias(
            "hotseat", "claude",
            runner=lambda argv, **_k: subprocess.CompletedProcess(argv, 1, stdout="", stderr="nope"))


def test_a_second_switch_waits_for_the_first_and_then_finds_nothing_to_do():
    hotseat = _Hotseat("ai13")
    hotseat.release.clear()
    d = desk.HotseatDesk()
    answers: list[tuple[int, dict]] = []
    first = threading.Thread(target=lambda: answers.append(hotseat.switch_default(d, "claude", "ai12")))
    first.start()
    assert hotseat.entered.wait(timeout=5)
    second = threading.Thread(target=lambda: answers.append(hotseat.switch_default(d, "claude", "ai12")))
    second.start()
    second.join(timeout=0.3)
    assert second.is_alive() and hotseat.reads == ["claude"], "the second has not even read while the first runs"

    hotseat.release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(hotseat.switches) == 1 and hotseat.reads == ["claude", "claude"]
    assert sorted((status, body["switched"], body.get("reason")) for status, body in answers) == [
        (200, False, "already_active"), (200, True, None)]
    assert _last_switch()["from"] == "ai13" and _last_switch()["to"] == "ai12"


def test_switches_of_different_providers_do_not_wait_for_each_other():
    hotseat = _Hotseat("ai13")
    hotseat.release.clear()
    d = desk.HotseatDesk()
    slow = threading.Thread(target=lambda: hotseat.switch_default(d, "claude", "ai12"))
    slow.start()
    assert hotseat.entered.wait(timeout=5)
    assert d.switch_lock("claude").locked() and not d.switch_lock("codex").locked()
    hotseat.release.set()
    slow.join(timeout=5)
    assert not d.switch_lock("claude").locked()


def test_a_switch_that_outlasts_the_budget_answers_pending_and_the_desk_tells_the_rest(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    hotseat = _Hotseat("ai13")
    hotseat.release.clear()
    d = desk.HotseatDesk()

    status, body = hotseat.switch_default(d, "claude", "ai12", budget_s=0.05)

    assert status == 202
    assert {k: body[k] for k in ("ok", "switched", "pending", "reason", "provider", "alias")} == {
        "ok": True, "switched": False, "pending": True, "reason": "switch_in_progress",
        "provider": "claude", "alias": "ai12"}
    assert body["message"]
    assert body["desk"]["switches"] == [{"provider": "claude", "alias": "ai12", "from": "ai13",
                                         "state": "running", "requested_at": T0_MS, "finished_at": None}]
    assert _last_switch() is None

    hotseat.release.set()
    for _ in range(200):
        later = d.payload(command="hotseat")["switches"]
        if later and later[0]["state"] != "running" and hotseat.hooks:
            break
        threading.Event().wait(0.01)
    assert later == [{"provider": "claude", "alias": "ai12", "from": "ai13", "state": "switched",
                      "requested_at": T0_MS, "finished_at": T0_MS}]
    assert _last_switch() == {"from": "ai13", "to": "ai12", "at": T0_MS} and hotseat.hooks == ["claude"]


def test_the_live_read_after_a_switch_does_not_hold_the_answer_and_ignores_the_interval(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    tui_p, list_p, work_p = _sample_payloads()
    reading = threading.Event()
    release = threading.Event()
    tui_calls: list[int] = []

    def runner(argv, **_kwargs):
        args = list(argv[1:])
        if args == ["tui", "--snapshot"]:
            tui_calls.append(1)
            if len(tui_calls) > 1:
                reading.set()
                assert release.wait(timeout=5)
            out = tui_p
        else:
            out = list_p if args == ["list", "--json"] else work_p
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")

    d = desk.HotseatDesk()
    d.payload(refresh=True, command="hotseat", runner=runner)
    hotseat = _Hotseat("ai13")

    status, body = d.switch_default("claude", "ai8", command="hotseat", switch_fn=hotseat.switch,
                                    read_active=hotseat.read_active, credentials_changed=hotseat.hooks.append,
                                    runner=runner)

    assert status == 200 and body["switched"] is True, "answered while the live read is still held"
    assert reading.wait(timeout=5), "a switch is followed by a live read although the interval has not passed"
    assert d.payload(command="hotseat")["refreshing"] is True
    release.set()
    for _ in range(200):
        if not d.payload(command="hotseat")["refreshing"]:
            break
        threading.Event().wait(0.01)
    assert len(tui_calls) == 2 and d.payload(command="hotseat")["refreshing"] is False


def test_a_failed_live_read_after_a_switch_leaves_the_confirmed_account_marked(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    tui_p, list_p, work_p = _sample_payloads()
    reads: list[int] = []

    def runner(argv, **_kwargs):
        args = list(argv[1:])
        if args == ["tui", "--snapshot"]:
            reads.append(1)
            if len(reads) > 1:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="provider down")
            out = tui_p
        else:
            out = list_p if args == ["list", "--json"] else work_p
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")

    d = desk.HotseatDesk()
    d.payload(refresh=True, command="hotseat", runner=runner)
    hotseat = _Hotseat("ai13")
    status_code, _body = d.switch_default("claude", "ai8", command="hotseat", switch_fn=hotseat.switch,
                                          read_active=hotseat.read_active,
                                          credentials_changed=hotseat.hooks.append, runner=runner)
    assert status_code == 200
    for _ in range(300):
        later = d.payload(command="hotseat")
        if later["error"] and not later["refreshing"]:
            break
        threading.Event().wait(0.01)

    assert later["error"] == "cli_failed" and len(reads) == 2
    active = {a["alias"]: a["active"] for a in later["accounts"] if a["provider"] == "claude"}
    assert active == {"ai13": False, "ai8": True}
    assert later["switches"][0]["state"] == "switched"


# --- the automatic switcher shares the lock ---------------------------------------

@pytest.fixture
def switcher(monkeypatch):
    """An enabled automatic Hotseat switcher whose reading says: switch a -> b."""
    monkeypatch.setattr(janitor_autonomy.backends, "active_handles", lambda *a: [])
    janitor_builtins.ensure_builtins()
    agents_db.create_agent(persona="Hotseat switcher", voice_id="v", cwd="/tmp", session="hotseat", backend="codex")
    created = janitors.create("hotseat", template_id="account-hotseat")
    owner = janitors.set_enabled("hotseat", created["revision"], True)
    janitor_autonomy.setup()
    calls: list[tuple] = []

    def read(command, provider):
        calls.append(("read", provider))
        if provider == "claude":
            return [{"alias": "a", "active": True, "remaining": 10, "secondary": 100},
                    {"alias": "b", "active": False, "remaining": 90, "secondary": 100}]
        return [{"alias": "p", "active": True, "remaining": 50, "secondary": None}]

    return owner, read, calls


def test_the_automatic_switch_holds_the_desks_lock_while_it_runs(switcher):
    owner, read, calls = switcher
    d = desk.HotseatDesk()
    held: list[bool] = []

    def switch(command, provider, alias):
        held.append(d.switch_lock(provider).locked())
        return True

    worker = janitor_autonomy.AutonomyJanitors(lambda *_: True, lambda p: {"sent": 1}, hotseat_read=read,
                                               hotseat_switch=switch, hotseat_switch_lock=d.switch_lock)
    worker.hotseat_once()

    assert held == [True] and not d.switch_lock("claude").locked()
    assert _last_switch()["to"] == "b"


def test_the_automatic_switch_waits_for_a_desk_switch_and_then_stands_down(switcher):
    owner, read, calls = switcher
    d = desk.HotseatDesk()
    switched: list[str] = []
    sent: list[dict] = []
    worker = janitor_autonomy.AutonomyJanitors(
        lambda *_: True, lambda p: sent.append(p) or {"sent": 1}, hotseat_read=read,
        hotseat_switch=lambda c, p, alias: switched.append(alias) or True, hotseat_switch_lock=d.switch_lock)
    d.switch_lock("claude").acquire()
    try:
        run = threading.Thread(target=worker.hotseat_once)
        run.start()
        run.join(timeout=0.5)
        assert run.is_alive() and switched == [], "the switcher waits while a desk switch holds the lock"
        # The desk switch finishes: the owner chose c, which the switcher's reading knew nothing about.
        desk_record = json.dumps({"from": "a", "to": "c", "at": T0_MS})
        settings_store.set_text("hotseat-switcher.claude.last-switch", desk_record)
    finally:
        d.switch_lock("claude").release()
    run.join(timeout=10)

    assert not run.is_alive() and switched == []
    assert settings_store.get("hotseat-switcher.claude.last-switch") == desk_record
    assert sent == [], "no notification about a switch that did not happen"
    assert status.payload()["actions"] == [] or all(
        a["provider"] != "claude" for a in status.payload()["actions"])
    runs = [r for r in janitors.list_runs(owner["session"]) if (r["demand_result"] or {}).get("reason") == "superseded by another switch"]
    assert len(runs) == 1 and runs[0]["outcome"] == "completed"


def test_a_scheduled_run_refreshes_the_desk_and_an_automatic_switch_names_the_new_account(switcher, monkeypatch):
    owner, _read, _calls = switcher
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    d = desk.HotseatDesk()
    before = [_claude_row("a", five_hour=0.20, weekly=0.10, fable=0.10, active=True),
              _claude_row("b", five_hour=0.10, weekly=0.10, fable=0.10)]
    tui_p, list_p, _ = _lists(before)
    _live(d, tui_p, list_p)
    # The switcher's own read, through the real reader: a is nearly spent, b has room.
    _, now_list, _ = _lists([_claude_row("a", five_hour=0.90, weekly=0.10, fable=0.10, active=True),
                             _claude_row("b", five_hour=0.10, weekly=0.10, fable=0.10)])
    codex_list = {"accounts": [{"alias": "p", "is_active": True, "usage": {"usable": True, "blocked": False,
                                                                           "error": None, "worst_used": 0.5}}]}
    from lib import janitor_hotseat

    def run(command, args, timeout):
        out = now_list if args == ["list", "--json"] else codex_list
        return subprocess.CompletedProcess([command, *args], 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(janitor_hotseat, "_run", run)
    switched: list[str] = []
    worker = janitor_autonomy.AutonomyJanitors(
        lambda *_: True, lambda p: {"sent": 1}, hotseat_switch=lambda c, p, alias: switched.append(alias) or True,
        hotseat_switch_lock=d.switch_lock, hotseat_observe=d.absorb_switcher_read, hotseat_switched=d.mark_switched)

    worker.hotseat_once()

    assert switched == ["b"]
    accounts = {a["alias"]: a for a in d.payload(command="hotseat", runner=_no_cli)["accounts"]}
    assert accounts["a"]["windows"][0]["remaining_ratio"] == 0.1, "the run's own read reached the desk"
    assert (accounts["a"]["active"], accounts["b"]["active"]) == (False, True), "and so did its switch"


# --- continuing stopped work -----------------------------------------------------

def _stopped(session: str, *, stopped_at_ms: int | None = T0_MS - 120_000, readiness="ready", kind="clarp") -> None:
    """Put one row in the stored desk's stopped-work list, as a live read would."""
    row = {"kind": kind, "cause": "usage_limit", "pending": 0, "id": session, "name": session,
           "backend": "claude", "stopped_at": stopped_at_ms,
           "readiness": {"ready": True, "hard": None, "warning": None} if readiness == "ready" else readiness}
    settings_store.set_text(desk.SNAPSHOT_KEY, json.dumps(
        {"v": 1, "observed_at": T0_MS, "accounts": [], "stopped_work": [row]}))


class _Dispatcher:
    """The send path as the desk sees it: one turn per message id, like /send."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.turns: list[str] = []
        self.ok = True

    def __call__(self, session, text, client_msg_id):
        self.sent.append((session, text, client_msg_id))
        if not self.ok:
            return {"ok": False, "error": "503: could not durably queue message"}
        if client_msg_id not in self.turns:
            self.turns.append(client_msg_id)
            agent = agents_db.get_by_session(session)
            agents_db.record_user_message(
                agent_id=agent["agent_id"], backend_session_id=agent.get("backend_session_id") or "bsid-1",
                text=text, client_msg_id=client_msg_id, origin="automation", sender_agent_id=None,
                prompt_admission_id="", trace_id="t-" + client_msg_id)
        return {"ok": True}


def _worker(session: str = "worker-1") -> str:
    return agents_db.create_agent(persona="Worker", voice_id="", cwd="/tmp", session=session, backend="claude")


def test_native_work_gets_its_command_and_nothing_runs():
    d = desk.HotseatDesk()
    code_nat, body_nat = d.resume_work("0195abcd", kind="native", backend="codex")
    assert code_nat == 200
    assert body_nat["continued"] is False
    assert body_nat["resume_command"] == "codex resume 0195abcd"
    assert d.resume_work("0195abcd", kind="native")[1]["resume_command"] == "claude --resume 0195abcd"
    assert d.resume_work("missing-agent")[0] == 404
    assert d.resume_work("--help")[1]["error"] == "invalid_id"
    assert d.resume_work("x", kind="other")[1]["error"] == "invalid_kind"
    assert d.resume_work("x", action="restart")[1]["error"] == "invalid_action"


def test_continue_sends_one_prompt_under_the_id_of_the_stop():
    agent_id = _worker()
    _stopped("worker-1")
    send = _Dispatcher()
    d = desk.HotseatDesk()

    first = d.resume_work("worker-1", send_prompt=send)
    second = d.resume_work("worker-1", send_prompt=send)
    by_agent_id = d.resume_work(agent_id, send_prompt=send)

    stop_id = f"hotseat-resume-{agent_id}-{T0_MS - 120_000}"
    assert first == (200, {"ok": True, "id": "worker-1", "kind": "clarp", "action": "continue",
                           "continued": True, "already_sent": False, "stopped_at": T0_MS - 120_000})
    assert second[0] == by_agent_id[0] == 200
    assert second[1]["already_sent"] is True and by_agent_id[1]["already_sent"] is True
    assert send.sent == [("worker-1", desk.RESUME_PROMPT, stop_id)] * 3, (
        "every request about one stop is the same send; the dispatcher's retry protection decides")
    assert send.turns == [stop_id]


def test_a_prompt_the_dispatcher_refused_can_be_tried_again():
    _worker()
    _stopped("worker-1")
    send = _Dispatcher()
    send.ok = False
    d = desk.HotseatDesk()

    status, body = d.resume_work("worker-1", send_prompt=send)
    assert status == 502 and body["error"] == "prompt_failed" and body["message"]
    assert "could not durably queue" in body["detail"]

    send.ok = True
    status, body = d.resume_work("worker-1", send_prompt=send)
    assert status == 200 and body["already_sent"] is False and len(send.turns) == 1
    assert d.resume_work("worker-1", send_prompt=None)[0] == 503


@pytest.mark.parametrize("readiness,sentence", [
    ({"ready": False, "hard": "ai13 is still rate limited", "warning": None},
     "Worker cannot continue yet: ai13 is still rate limited."),
    ({"ready": False, "hard": None, "warning": "cannot verify Fable quota; per-model limits are unavailable"},
     "Worker cannot continue yet: cannot verify Fable quota; per-model limits are unavailable."),
    ({"ready": False, "hard": None, "warning": None}, "Worker cannot continue yet."),
    (None, "Worker cannot continue yet."),
])
def test_continue_is_refused_until_readiness_says_ready(readiness, sentence):
    _worker()
    _stopped("worker-1", readiness=readiness)
    send = _Dispatcher()

    status, body = desk.HotseatDesk().resume_work("worker-1", send_prompt=send)

    assert (status, body) == (409, {"ok": False, "error": "not_ready", "message": sentence})
    assert send.sent == []


def test_continue_is_refused_for_an_agent_the_desk_does_not_list_as_stopped():
    _worker()
    _worker("worker-2")
    send = _Dispatcher()
    d = desk.HotseatDesk()

    assert d.resume_work("worker-1", send_prompt=send)[1]["error"] == "not_stopped", "no stored desk at all"
    _stopped("worker-2")
    status, body = d.resume_work("worker-1", send_prompt=send)
    assert status == 409 and body["error"] == "not_stopped" and body["message"].startswith("Worker is not in the list")
    _stopped("worker-1", kind="native")
    assert d.resume_work("worker-1", send_prompt=send)[1]["error"] == "not_stopped"
    _stopped("worker-1", stopped_at_ms=None)
    assert d.resume_work("worker-1", send_prompt=send)[1]["error"] == "not_stopped", "a stop without a time has no id"
    assert send.sent == []


def test_continue_is_refused_once_the_agent_ran_or_runs_again():
    agent_id = _worker()
    send = _Dispatcher()
    d = desk.HotseatDesk()
    _stopped("worker-1", stopped_at_ms=agents_db.now_ms() + 60_000)
    agents_db.record_state(agent_id, AgentState.DONE)
    assert d.resume_work("worker-1", send_prompt=send)[0] == 200, "a turn that ended before the stop does not count"

    _stopped("worker-1", stopped_at_ms=agents_db.now_ms() - 120_000)
    status, body = d.resume_work("worker-1", send_prompt=send)
    assert status == 409 and body["error"] == "not_stopped" and "finished a turn since it stopped" in body["message"]

    other = _worker("worker-2")
    _stopped("worker-2")
    agents_db.record_state(other, AgentState.THINKING)
    status, body = d.resume_work("worker-2", send_prompt=send)
    assert (status, body["error"], body["message"]) == (409, "agent_busy", "Worker is working now.")
    assert len(send.sent) == 1


def test_archived_and_janitor_agents_are_refused_for_both_actions():
    from lib import db
    archived = _worker("worker-1")
    janitor = _worker("worker-2")
    con = db.conn()
    con.execute("UPDATE agents SET archived_at = ? WHERE agent_id = ?", (T0_MS, archived))
    con.execute("UPDATE agents SET is_janitor = 1 WHERE agent_id = ?", (janitor,))
    con.commit()
    turn_queue.set_paused(archived, True)
    turn_queue.set_paused(janitor, True)
    send = _Dispatcher()
    unpaused: list[str] = []
    d = desk.HotseatDesk()

    for action in ("continue", "unpause"):
        _stopped("worker-1")
        status, body = d.resume_work("worker-1", action=action, send_prompt=send,
                                     unpause_fn=lambda a, s: unpaused.append(a) or {})
        assert (status, body["error"]) == (409, "agent_archived") and body["message"].startswith("Worker is archived")
        _stopped("worker-2")
        status, body = d.resume_work("worker-2", action=action, send_prompt=send,
                                     unpause_fn=lambda a, s: unpaused.append(a) or {})
        assert (status, body["error"]) == (409, "janitor_inspection_only") and "Janitor" in body["message"]
    assert send.sent == [] and unpaused == []
    assert turn_queue.is_paused(archived) and turn_queue.is_paused(janitor)


def test_a_paused_queue_refuses_continue_until_it_is_unpaused():
    agent_id = _worker()
    _stopped("worker-1")
    send = _Dispatcher()
    d = desk.HotseatDesk()
    turn_queue.set_paused(agent_id, True)

    status, body = d.resume_work("worker-1", send_prompt=send)
    assert status == 409 and body["error"] == "queue_paused" and "queue is paused" in body["message"]
    assert send.sent == []

    code_unpause, body_unpause = d.resume_work("worker-1", action="unpause")
    assert code_unpause == 200
    assert body_unpause["queue_paused"] is False
    assert turn_queue.state(agent_id)["paused"] is False
    assert d.resume_work("worker-1", send_prompt=send)[0] == 200


# --- the handler's own part -------------------------------------------------------

def _handler(ctx):
    handler = host.Handler.__new__(host.Handler)
    handler.server = SimpleNamespace(ctx=ctx)
    return handler


def test_unpause_hands_the_waiting_turns_to_the_runtime_when_there_is_one(monkeypatch):
    agent_id = _worker()
    recovered: list[str] = []
    broadcasts: list[tuple[str, str]] = []
    runtime = SimpleNamespace(recover_queued=lambda: recovered.append("runtime"))
    handler = _handler(SimpleNamespace(runtime_client=runtime))
    handler._broadcast_turn_queue = lambda a, s: broadcasts.append((a, s))
    monkeypatch.setattr(host.TurnDispatchService, "recover_queued",
                        lambda self, *a, **k: recovered.append("in-process"))
    turn_queue.set_paused(agent_id, True)

    state = handler._hotseat_unpause_queue(agent_id, "worker-1")
    handler._hotseat_unpause_queue(agent_id, "worker-1")

    assert state["paused"] is False and recovered == ["runtime"], "once, and by the process that owns turns"
    assert broadcasts == [(agent_id, "worker-1")] * 2

    handler = _handler(SimpleNamespace(runtime_client=None))
    handler._broadcast_turn_queue = lambda a, s: None
    turn_queue.set_paused(agent_id, True)
    handler._hotseat_unpause_queue(agent_id, "worker-1")
    assert recovered == ["runtime", "in-process"]


def test_the_handler_sends_the_prompt_through_the_dispatcher_as_automation(monkeypatch):
    commands: list = []

    def submit(self, command):
        commands.append(command)
        return SimpleNamespace(queued=False)

    monkeypatch.setattr(host.TurnDispatchService, "submit", submit)
    handler = _handler(SimpleNamespace(runtime_client=None, auth_token="admin-secret"))

    assert handler._hotseat_send_prompt("worker-1", "go on", "hotseat-resume-a-1") == {"ok": True, "queued": False}
    command = commands[0]
    assert (command.text, command.forced_session, command.requested_session) == ("go on", "worker-1", "worker-1")
    assert (command.client_msg_id, command.origin) == ("hotseat-resume-a-1", "automation")
    assert command.synthesize_audio is False and command.queue_if_busy is False
    assert command.prompt_admission is None and command.trace_id

    def refuse(self, command):
        raise host.DispatchError(503, "runtime unavailable")

    monkeypatch.setattr(host.TurnDispatchService, "submit", refuse)
    assert handler._hotseat_send_prompt("worker-1", "go on", "x") == {"ok": False, "error": "503: runtime unavailable"}


# --- the off-switch ------------------------------------------------------------------

def _desk_setting(monkeypatch, enabled: bool) -> None:
    import dataclasses
    from lib import config
    current = config.load()
    monkeypatch.setattr(config, "load", lambda *a, **k: dataclasses.replace(current, hotseat_desk_enabled=enabled))


def test_the_desk_is_on_unless_the_config_turns_it_off(tmp_path):
    from lib import config
    path = tmp_path / "config.toml"
    try:
        for text, expected in (("", True), ("[hotseat]\ndesk = true\n", True), ("[hotseat]\ndesk = false\n", False),
                               ('[hotseat]\ndesk = "no"\n', True), ("[hotseat]\n", True)):
            path.write_text(text)
            config.reset_cache()
            assert config.load(path).hotseat_desk_enabled is expected, text
    finally:
        config.reset_cache()


def test_turning_the_desk_off_in_the_file_takes_effect_on_a_running_host(tmp_path, monkeypatch):
    from lib import config, server_identity
    path = tmp_path / "config.toml"
    path.write_text("[hotseat]\ndesk = true\n")
    monkeypatch.setattr(config, "RELOAD_CHECK_INTERVAL_SEC", 0.0)
    try:
        config.reset_cache()
        assert config.load(path).hotseat_desk_enabled is True
        assert "hotseat_desk" in server_identity.capabilities()["features"]

        path.write_text("# switched off by the owner\n[hotseat]\ndesk = false\n")

        assert server_identity.hotseat_desk_enabled() is False, "no restart, no cache reset"
        assert "hotseat_desk" not in server_identity.capabilities()["features"]
    finally:
        config.reset_cache()


def test_a_host_with_the_desk_off_does_not_advertise_it(monkeypatch):
    from lib import server_identity
    desk_features = {"hotseat_desk", "hotseat_desk_actions"}
    assert desk_features <= set(server_identity.capabilities()["features"])
    assert desk_features <= set(server_identity.contract()["features"])

    _desk_setting(monkeypatch, False)

    assert not desk_features & set(server_identity.capabilities()["features"])
    assert not desk_features & set(server_identity.contract()["features"])
    assert "accounts_hotseat" in server_identity.capabilities()["features"], "the read-only view stays"
    assert server_identity.contract()["host"] == server_identity.HOST_CONTRACT


@pytest.mark.parametrize("name", ["_handle_hotseat_desk", "_handle_hotseat_switch", "_handle_hotseat_resume"])
def test_the_three_routes_answer_like_unknown_routes_while_the_desk_is_off(monkeypatch, name):
    _desk_setting(monkeypatch, False)

    class NoDesk:
        def __getattr__(self, attribute):
            raise AssertionError(f"the desk must not be asked for {attribute} while it is off")

    handler = _handler(SimpleNamespace(hotseat_desk=NoDesk()))
    answers: list[tuple] = []
    handler._send = lambda code, body=b"", *a, **k: answers.append((code, body))
    handler._read_json = lambda: pytest.fail("the body is not read")
    getattr(handler, name)()
    assert answers == [(404, b"not found")]


@pytest.mark.parametrize("method,name", [("GET", "_handle_hotseat_desk"), ("POST", "_handle_hotseat_switch"),
                                         ("POST", "_handle_hotseat_resume")])
def test_a_host_without_a_desk_object_answers_503(method, name):
    handler = _handler(SimpleNamespace(hotseat_desk=None))
    answers: list[int] = []
    handler._json_error = lambda code, _msg: answers.append(code)
    getattr(handler, name)()
    assert answers == [503]


@pytest.mark.parametrize(
    "method,path,limited_status",
    [
        ("GET", "/hotseat/status", 200),
        ("GET", "/hotseat/desk", 403),
        ("POST", "/hotseat/switch", 403),
        ("POST", "/hotseat/resume", 403),
    ],
)
def test_hotseat_desk_routes_require_full_device_scope_while_status_stays_readable(method, path, limited_status):
    handler = host.Handler.__new__(host.Handler)
    handler.path = path
    handler._request_device_scope = "limited"
    handler._request_auth_validated = True
    handler._request_principal = "limited-device"
    handler._authorized = lambda: True
    responses: list[int] = []
    handler._json_error = lambda code, _msg: responses.append(code)
    handler._send = lambda code, *_a, **_k: responses.append(code)
    getattr(handler, f"do_{method}")()
    assert responses == [limited_status]
