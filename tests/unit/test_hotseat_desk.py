"""Tests for the Hotseat TUI desk (`lib.hotseat_desk` and `/hotseat/*` routes)."""
from __future__ import annotations

import json
import subprocess
import threading

import pytest

import server as host
from lib import agents as agents_db
from lib import hotseat_desk as desk
from lib import hotseat_status as status
from lib import settings_store, turn_queue

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

    result = desk.payload(refresh=True, command="hotseat", which=lambda _: "/usr/bin/hotseat", runner=runner)
    assert result["schema"] == "clarp.hotseat-desk/1"
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
    cached = desk.payload(
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
    before = desk.payload(refresh=True, command="hotseat", which=lambda _: "/usr/bin/hotseat", runner=runner)
    assert before["accounts"][0]["state"] == "exhausted"
    assert before["accounts"][0]["windows"][0]["remaining_ratio"] == 0.0

    # Serve after SOON_RESET_S has passed without a new refresh: ai1 becomes unknown (not 0%),
    # whereas no-credits-acct stays exhausted because no_reset_reason blocks reset recovery.
    after_ms = int(SOON_RESET_S * 1000) + 10_000
    after = desk.payload(refresh=False, served_at=after_ms, command="hotseat", which=lambda _: "/usr/bin/hotseat")
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

    results: list[dict] = []
    t1 = threading.Thread(target=lambda: results.append(desk.refresh_desk(command="hotseat", runner=slow_runner)))
    t1.start()
    assert entered.wait(timeout=5)

    t2 = threading.Thread(target=lambda: results.append(desk.refresh_desk(command="hotseat", runner=slow_runner)))
    t2.start()
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert tui_calls == 1
    assert len(results) == 2
    assert all(len(r["accounts"]) == 3 for r in results)


def test_switch_default_validates_inputs_updates_receipt_and_refreshes(monkeypatch):
    monkeypatch.setattr(desk, "now_ms", lambda: T0_MS)
    tui_p, list_p, work_p = _sample_payloads()
    runner = _fake_runner_factory(tui_p, list_p, work_p)
    desk.payload(refresh=True, command="hotseat", runner=runner)

    assert desk.switch_default("bogus", "ai12")[0] == 400
    assert desk.switch_default("claude", "../bad")[0] == 400

    code_fail, body_fail = desk.switch_default(
        "claude", "ai12", command="hotseat", switch_fn=lambda *_a: False, runner=runner
    )
    assert code_fail == 409 and body_fail["error"] == "switch_failed"

    switched_calls: list[tuple[str, str, str]] = []
    code_ok, body_ok = desk.switch_default(
        "claude",
        "ai12",
        command="hotseat",
        switch_fn=lambda cmd, prov, alias: switched_calls.append((cmd, prov, alias)) or True,
        runner=runner,
    )
    assert code_ok == 200
    assert body_ok["switched"] is True
    assert switched_calls == [("hotseat", "claude", "ai12")]
    receipt = json.loads(settings_store.get("hotseat-switcher.claude.last-switch"))
    assert receipt == {"from": "ai13", "to": "ai12", "at": T0_MS}


def test_resume_work_handles_continue_paused_refusal_unpause_and_native_commands():
    # Native session returns CLI command without executing anything.
    code_nat, body_nat = desk.resume_work("0195abcd", kind="native", backend="codex")
    assert code_nat == 200
    assert body_nat["continued"] is False
    assert body_nat["resume_command"] == "codex resume 0195abcd"

    # Unknown Clarp session -> 404.
    assert desk.resume_work("missing-agent")[0] == 404

    # Real Clarp agent: continue sends automation prompt; paused queue refuses continue until unpaused.
    agent_id = agents_db.create_agent(
        persona="Worker", voice_id="", cwd="/tmp", session="worker-1", backend="claude"
    )

    sent: list[tuple[str, str]] = []
    code_cont, body_cont = desk.resume_work(
        "worker-1",
        send_prompt=lambda session, text: sent.append((session, text)) or {"ok": True},
    )
    assert code_cont == 200 and body_cont["continued"] is True
    assert sent == [("worker-1", desk.RESUME_PROMPT)]

    turn_queue.set_paused(agent_id, True)
    code_paused, body_paused = desk.resume_work(
        "worker-1",
        send_prompt=lambda *_a: {"ok": True},
    )
    assert code_paused == 409 and body_paused["error"] == "queue_paused"

    code_unpause, body_unpause = desk.resume_work("worker-1", action="unpause")
    assert code_unpause == 200
    assert body_unpause["queue_paused"] is False
    assert turn_queue.state(agent_id)["paused"] is False


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
