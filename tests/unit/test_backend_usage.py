from __future__ import annotations

import json
import threading

import os
import pytest

from lib import backend_usage
from tests.patching import patch_shared


@pytest.fixture(autouse=True)
def _fake_clis_are_this_tests_own(fake_backend_cli, tmp_path):
    """This module's fake provider CLIs are files it writes under tmp_path:
    the explicit opt-in of tests/conftest.py's real-CLI guard."""
    fake_backend_cli.dir(tmp_path)


@pytest.fixture(autouse=True)
def _no_recent_agy_attempt(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_AGY_LAST_ATTEMPT_MS", [0], raising=False)


def test_backend_usage_response_filters_legacy_non_quota_windows():
    """_quota_windows still guards the Codex row, which is endpoint-shaped."""
    backend_usage._upsert(
        backend_usage.CODEX,
        used_percentage=23,
        resets_at="2026-06-26T16:30:00Z",
        source="codex-usage-endpoint",
        raw={
            "windows": {
                "five_hour": {
                    "used_percentage": 23,
                    "resets_at": "2026-06-26T16:30:00Z",
                },
                "seven_day": {
                    "used_percentage": 15,
                    "resets_at": "2026-07-02T15:00:00Z",
                },
                "window": {"used_percentage": 3, "resets_at": ""},
            }
        },
    )

    codex = next(row for row in
                 backend_usage.get_backend_usage(refresh_codex=False)["backends"]
                 if row["backend"] == "codex")
    assert set(codex["windows"]) == {"five_hour", "seven_day"}


def test_structured_claude_canonicalizes_primary_secondary_aliases():
    # Seeded directly: the statusline capture that used to write this row is
    # gone (a statusline never runs under `-p`). This still pins the window
    # canonicalization _structured_provider does on whatever wrote the row.
    backend_usage._upsert(
        backend_usage.CLAUDE,
        used_percentage=31,
        resets_at="2030-01-01T05:00:00Z",
        source="seeded",
        raw={"windows": {
            "primary": {"used_percentage": 31,
                        "resets_at": "2030-01-01T05:00:00Z"},
            "secondary": {"used_percentage": 12,
                          "resets_at": "2030-01-07T00:00:00Z"},
        }},
    )
    windows = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["claude"]["windows"]
    assert [(window["kind"], window["used_percentage"]) for window in windows] \
        == [("five_hour", 31), ("seven_day", 12)]


@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
@pytest.mark.parametrize("utilization", [0, 0.5, 1, 2, 37, 100])
def test_claude_oauth_usage_normalizes_windows(monkeypatch, window, utilization):
    # The endpoint's utilization is already a percent (1 means 1 %), so only a
    # full window may mark Claude exhausted for every Claude agent.
    other = 12.0
    body = json.dumps({
        "five_hour": {
            "utilization": utilization if window == "five_hour" else other,
            "resets_at": "2030-01-01T05:00:00Z"},
        "seven_day_oauth_apps": {
            "utilization": utilization if window == "seven_day" else other,
            "resets_at": "2030-01-07T00:00:00Z"},
    }).encode()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return body

    patch_shared(monkeypatch, backend_usage, "_read_claude_auth",
                        lambda: ("token", 0, "max_20x"))
    monkeypatch.setattr(backend_usage.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response())

    result = backend_usage.fetch_claude_usage()
    assert result["windows"][window]["used_percentage"] == utilization
    assert result["windows"]["seven_day"]["window_minutes"] == 10_080
    snapshot = backend_usage.get_backend_usage(refresh_codex=False)["providers"]["claude"]
    assert [entry["kind"] for entry in snapshot["windows"]] == ["five_hour", "seven_day"]
    exhausted = backend_usage.exhausted_backends()
    if utilization >= 100:
        assert exhausted[backend_usage.CLAUDE]["window"] == window
    else:
        assert backend_usage.CLAUDE not in exhausted


def test_codex_direct_failure_uses_app_server_fallback(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_claude_needs_refresh", lambda: False)
    patch_shared(monkeypatch, backend_usage, "_codex_needs_refresh", lambda: True)
    patch_shared(monkeypatch, backend_usage, "fetch_codex_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("direct down")))
    called = []
    patch_shared(monkeypatch, backend_usage, "fetch_codex_usage_app_server",
                        lambda: called.append(True) or {"limit_events": []})

    backend_usage.get_backend_usage()
    assert called == [True]


def test_codex_usage_fetch_uses_chatgpt_auth_and_wham_path(tmp_path, monkeypatch):
    seen: dict[str, str] = {}
    body = json.dumps({
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 42, "limit_window_seconds": 18_000,
                "reset_at": 1_900_000_000,
            },
            "secondary_window": {
                "used_percent": 5, "limit_window_seconds": 604_800,
                "reset_at": 1_900_604_800,
            },
        },
    }).encode()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return body

    def open_request(request, **_kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization") or ""
        seen["account"] = request.get_header("Chatgpt-account-id") or ""
        return Response()

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "token-123", "account_id": "account-123"},
    }))
    (codex_home / "config.toml").write_text(
        'chatgpt_base_url = "https://chatgpt.com/backend-api/"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(backend_usage.urllib.request, "urlopen", open_request)

    snapshot = backend_usage.fetch_codex_usage()

    assert seen["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert seen["authorization"] == "Bearer token-123"
    assert seen["account"] == "account-123"
    assert snapshot["used_percentage"] == 42.0
    assert snapshot["windows"]["secondary"]["used_percentage"] == 5.0

    result = backend_usage.get_backend_usage(refresh_codex=False)
    codex = result["backends"][1]
    assert codex["backend"] == "codex"
    assert codex["used_percentage"] == 42.0
    assert codex["source"] == "codex-usage-endpoint"


def test_codex_usage_unknown_without_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    # The app-server fallback must not reach the user's installed codex: an
    # inert stand-in (registered with this module's tmp_path) fails instead.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = backend_usage.get_backend_usage(refresh_codex=True)
    codex = result["backends"][1]
    assert codex["backend"] == "codex"
    assert codex["freshness"] == "unknown"
    assert "No such file" in codex["error"] or "auth" in codex["error"]


def test_codex_usage_force_refresh_bypasses_fresh_cache(monkeypatch):
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        return {}

    patch_shared(monkeypatch, backend_usage, "fetch_codex_usage", fake_fetch)
    patch_shared(monkeypatch, backend_usage, "_codex_needs_refresh", lambda: False)

    backend_usage.get_backend_usage(refresh_codex=True)
    assert calls == 0

    backend_usage.get_backend_usage(refresh_codex=True, force_codex=True)
    assert calls == 1


def test_fresh_legacy_row_does_not_suppress_first_structured_refresh(monkeypatch):
    now = 1_800_000_000_000
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now)
    backend_usage._upsert(
        backend_usage.CODEX, used_percentage=20, resets_at="",
        source="legacy", fetched_at=now,
        raw={"windows": {"primary": {"used_percentage": 20}}})
    assert backend_usage._codex_needs_refresh() is True

    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(20), identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")
    assert backend_usage._codex_needs_refresh() is False


def _codex_rate_limits(used: float, *, resets_at: int = 1_900_000_000):
    return {
        "rateLimits": {
            "primary": {
                "usedPercent": used,
                "windowDurationMins": 300,
                "resetsAt": resets_at,
            }
        }
    }


def test_structured_codex_usage_is_computer_scoped_and_others_unknown(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(24), identity=("auth-a", "account-a"))

    result = backend_usage.get_backend_usage(refresh_codex=False)
    assert result["schema_version"] == 1
    assert result["capability_catalog_schema_version"] == 2
    codex = result["providers"]["codex"]
    assert codex["provider_instance_id"] == f"{result['computer_id']}:codex"
    assert codex["freshness"] == "fresh"
    assert codex["windows"][0]["kind"] == "five_hour"
    assert codex["windows"][0]["used_percentage"] == 24
    assert codex["windows"][0]["limit"]["state"] == "normal"
    for provider_id in ("claude", "agy"):
        assert result["providers"][provider_id]["freshness"] == "unknown"
        assert result["providers"][provider_id]["windows"] == []


def test_warning_hard_limit_recovery_and_dedupe_reuse_episode(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(82), identity=("auth-a", "account-a"))
    assert [event["kind"] for event in warning["limit_events"]] == ["warning"]
    warning_event = warning["limit_events"][0]
    assert warning_event["threshold_id"] == "five_hour_80_percent"

    repeated = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(83), identity=("auth-a", "account-a"))
    assert repeated["limit_events"] == []

    now[0] += 1
    hard = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(100), identity=("auth-a", "account-a"))
    assert hard["limit_events"][0]["kind"] == "hard_limit"
    assert hard["limit_events"][0]["episode_id"] == warning_event["episode_id"]

    now[0] += 1
    recovered = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(20), identity=("auth-a", "account-a"))
    assert recovered["limit_events"][0]["kind"] == "recovered"
    assert recovered["limit_events"][0]["episode_id"] == warning_event["episode_id"]


def test_transitions_back_to_prior_kind_emit_new_event(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(82), identity=("auth-a", "account-a"))
    now[0] += 1
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(100), identity=("auth-a", "account-a"))
    now[0] += 1
    repeated_warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    assert [event["kind"] for event in repeated_warning["limit_events"]] \
        == ["warning"]
    window = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]
    assert window["limit"]["state"] == "warning"
    now[0] += 1
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(100), identity=("auth-a", "account-a"))
    window = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]
    assert window["limit"]["state"] == "hard_limit"

def test_reset_passage_becomes_stale_unknown_without_recovery(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    reset_seconds = now[0] // 1000 + 1
    hard = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(100, resets_at=reset_seconds),
        identity=("auth-a", "account-a"))
    episode_id = hard["limit_events"][0]["episode_id"]

    now[0] += 2_000
    response = backend_usage.get_backend_usage(refresh_codex=False)
    assert [event["kind"] for event in response["limit_events"]] == ["unknown"]
    assert response["limit_events"][0]["episode_id"] == episode_id
    window = response["providers"]["codex"]["windows"][0]
    assert window["freshness"] == "stale"
    assert window["limit"]["state"] == "unknown"
    assert backend_usage.get_backend_usage(refresh_codex=False)["limit_events"] == []


def test_failed_refresh_preserves_last_observation_for_age_decay(monkeypatch):
    backend_usage._upsert(
        backend_usage.CODEX, used_percentage=33,
        resets_at="2026-06-26T05:00:00Z", source="codex-usage-endpoint",
        fetched_at=backend_usage._now_ms(),
        raw={"windows": {"primary": {"used_percentage": 33}}})
    backend_usage._record_unknown(
        backend_usage.CODEX, "codex-usage-endpoint", "temporary failure")
    legacy = backend_usage.get_backend_usage(refresh_codex=False)["backends"][1]
    assert legacy["used_percentage"] == 33
    assert legacy["error"] == "temporary failure"


def test_sparse_secondary_update_does_not_refresh_absent_five_hour(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(25), identity=("auth-a", "account-a"))
    original = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]

    now[0] += backend_usage.CODEX_FRESH_MS + 1
    sparse = backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "secondary": {
                "usedPercent": 50,
                "windowDurationMins": 10_080,
                "resetsAt": 1_900_000_000,
            }
        }
    }, identity=("auth-a", "account-a"))
    assert sparse["limit_events"] == []
    current = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]
    five_hour = next(
        window for window in current["windows"] if window["kind"] == "five_hour")
    weekly = next(
        window for window in current["windows"] if window["kind"] == "seven_day")
    assert five_hour["observed_at"] == original["observed_at"]
    assert five_hour["freshness"] == "stale"
    assert weekly["freshness"] == "fresh"
    assert current["freshness"] == "stale"


def test_sparse_notification_does_not_suppress_full_refresh(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "secondary": {
                "usedPercent": 50, "windowDurationMins": 10_080,
                "resetsAt": 1_900_000_000,
            }
        }
    }, identity=("auth-a", "account-a"))
    assert backend_usage._codex_needs_refresh() is True

    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(20), identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")
    assert backend_usage._codex_needs_refresh() is False


def test_classified_terminal_is_fresh_hard_limit_with_unknown_quantities(
    monkeypatch,
):
    patch_shared(monkeypatch,
        backend_usage, "_read_codex_auth", lambda: ("token", "account"))
    first = backend_usage.record_classified_usage_limit("codex")
    second = backend_usage.record_classified_usage_limit("codex")

    assert first["kind"] == "hard_limit"
    assert first["freshness"] == "fresh"
    assert first["used_percentage"] is None
    assert first["resets_at"] is None
    assert first["_new"] is True
    assert second["provider_limit_event_id"] == first["provider_limit_event_id"]
    assert second["episode_id"] == first["episode_id"]
    assert second["_new"] is False
    assert second["dedupe_key"] == first["dedupe_key"]
    structured = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]
    unknown_window = next(
        window for window in structured["windows"] if window["kind"] == "unknown")
    assert unknown_window["limit"]["state"] == "hard_limit"
    assert unknown_window["used_percentage"] is None


def test_app_server_read_selects_evidenced_300_minute_limit(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    snapshot = backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "primary": {
                "usedPercent": 60, "windowDurationMins": 10_080,
                "resetsAt": 1_900_000_000,
            }
        },
        "rateLimitsByLimitId": {
            "weekly": {
                "primary": {
                    "usedPercent": 60, "windowDurationMins": 10_080,
                    "resetsAt": 1_900_000_000,
                }
            },
            "codex": {
                "primary": {
                    "usedPercent": 41, "windowDurationMins": 300,
                    "resetsAt": 1_850_000_000,
                },
                "secondary": {
                    "usedPercent": 60, "windowDurationMins": 10_080,
                    "resetsAt": 1_900_000_000,
                },
            },
        },
    }, identity=("auth-a", "account-a"))
    assert snapshot["windows"]["primary"]["window_minutes"] == 300
    structured = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]
    assert structured["windows"][0]["used_percentage"] == 41
    assert [window["kind"] for window in structured["windows"]] \
        == ["five_hour", "seven_day"]


def test_app_server_read_prefers_explicit_codex_bucket(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    snapshot = backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "primary": {"usedPercent": 99, "windowDurationMins": 300,
                        "resetsAt": 1_900_000_000}
        },
        "rateLimitsByLimitId": {
            "other": {
                "primary": {"usedPercent": 88, "windowDurationMins": 300,
                            "resetsAt": 1_900_000_000}
            },
            "codex": {
                "primary": {"usedPercent": 22, "windowDurationMins": 300,
                            "resetsAt": 1_900_000_000}
            },
        },
    }, identity=("auth-a", "account-a"))
    assert snapshot["windows"]["primary"]["used_percentage"] == 22


def test_weekly_primary_only_is_persisted_structurally(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "primary": {
                "usedPercent": 55, "windowDurationMins": 10_080,
                "resetsAt": 1_900_000_000,
            }
        }
    }, identity=("auth-a", "account-a"))
    windows = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"]
    assert [(window["kind"], window["used_percentage"]) for window in windows] \
        == [("seven_day", 55)]


def test_five_hour_secondary_is_detected_by_duration(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "primary": {"usedPercent": 40, "windowDurationMins": 10_080,
                        "resetsAt": 1_900_000_000},
            "secondary": {"usedPercent": 30, "windowDurationMins": 300,
                          "resetsAt": 1_850_000_000},
        }
    }, identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")
    windows = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"]
    assert [(window["kind"], window["used_percentage"]) for window in windows] \
        == [("five_hour", 30), ("seven_day", 40)]


def test_sparse_update_never_merges_windows_across_auth_generations(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(25), identity=("auth-a", "account-a"))
    now[0] += 1
    backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "secondary": {
                "usedPercent": 70, "windowDurationMins": 10_080,
                "resetsAt": 1_900_000_000,
            }
        }
    }, identity=("auth-b", "account-b"))
    windows = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"]
    assert [(window["kind"], window["used_percentage"]) for window in windows] \
        == [("seven_day", 70)]


def test_weekly_only_new_auth_invalidates_old_limit_episode(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    episode_id = warning["limit_events"][0]["episode_id"]
    now[0] += 1
    changed = backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "secondary": {
                "usedPercent": 70, "windowDurationMins": 10_080,
                "resetsAt": 1_900_000_000,
            }
        }
    }, identity=("auth-b", "account-b"))
    assert [(event["kind"], event["episode_id"])
            for event in changed["limit_events"]] == [("unknown", episode_id)]


def test_invalid_new_auth_payload_does_not_invalidate_prior_episode(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    episode_id = warning["limit_events"][0]["episode_id"]
    with pytest.raises(RuntimeError, match="missing rate_limit"):
        backend_usage.capture_codex_rate_limits(
            {"rateLimitsByLimitId": {
                "one": {"primary": {"usedPercent": 1,
                                      "windowDurationMins": 300}},
                "two": {"primary": {"usedPercent": 2,
                                      "windowDurationMins": 300}},
            }}, identity=("auth-b", "account-b"))
    row = backend_usage.db.conn().execute(
        "SELECT status,current_kind FROM provider_limit_episodes "
        "WHERE episode_id=?", (episode_id,),).fetchone()
    assert (row["status"], row["current_kind"]) == ("open", "warning")


def test_authoritative_refresh_drops_omitted_legacy_windows(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits({
        "rate_limit": {
            "primary_window": {"used_percent": 10,
                               "limit_window_seconds": 18_000},
            "secondary_window": {"used_percent": 20,
                                 "limit_window_seconds": 604_800},
        }
    }, identity=("auth-a", "account-a"), source_detail="/wham/usage")
    backend_usage.capture_codex_rate_limits({
        "rate_limit": {
            "primary_window": {"used_percent": 11,
                               "limit_window_seconds": 18_000},
        }
    }, identity=("auth-a", "account-a"), source_detail="/wham/usage")
    legacy = backend_usage.get_backend_usage(refresh_codex=False)["backends"][1]
    assert set(legacy["windows"]) == {"primary"}
    structured = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"]
    assert [window["kind"] for window in structured] == ["five_hour"]


def test_authoritative_omission_invalidates_open_episode(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")
    episode_id = warning["limit_events"][0]["episode_id"]
    changed = backend_usage.capture_codex_rate_limits({
        "rateLimits": {
            "primary": {"usedPercent": 20, "windowDurationMins": 10_080,
                        "resetsAt": 1_900_000_000}
        }
    }, identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")
    assert [(event["kind"], event["episode_id"])
            for event in changed["limit_events"]] == [("unknown", episode_id)]
    row = backend_usage.db.conn().execute(
        "SELECT status,current_kind FROM provider_limit_episodes "
        "WHERE episode_id=?", (episode_id,),).fetchone()
    assert (row["status"], row["current_kind"]) == ("invalidated", "unknown")


def test_classified_terminal_does_not_reuse_stale_window(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(95, resets_at=1_900_000_000),
        identity=("auth-a", "account-a"))
    known_window_id = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]["window_id"]
    patch_shared(monkeypatch,
        backend_usage, "_read_codex_auth", lambda: ("token-a", "account-a"))
    patch_shared(monkeypatch,
        backend_usage, "_codex_identity",
        lambda _token, _account: ("auth-a", "account-a"))
    now[0] += backend_usage.CODEX_FRESH_MS + 1

    event = backend_usage.record_classified_usage_limit("codex")
    assert event["used_percentage"] is None
    assert event["resets_at"] is None
    assert event["window_id"] != known_window_id


def test_classified_hard_limit_decays_to_unknown_when_stale(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    patch_shared(monkeypatch,
        backend_usage, "_read_codex_auth", lambda: ("token", "account"))
    backend_usage.record_classified_usage_limit("codex")
    window = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]
    assert window["limit"]["state"] == "hard_limit"

    now[0] += backend_usage.CODEX_FRESH_MS + 1
    window = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]
    assert window["freshness"] == "stale"
    assert window["limit"]["state"] == "unknown"

    repeated = backend_usage.record_classified_usage_limit("codex")
    assert repeated["_new"] is False
    window = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]
    assert window["freshness"] == "fresh"
    assert window["limit"]["state"] == "hard_limit"


def test_unknown_terminal_does_not_rebind_five_hour_episode(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    warning_episode = warning["limit_events"][0]["episode_id"]
    patch_shared(monkeypatch,
        backend_usage, "_read_codex_auth", lambda: ("token-a", "account-a"))
    patch_shared(monkeypatch,
        backend_usage, "_codex_identity",
        lambda _token, _account: ("auth-a", "account-a"))
    terminal = backend_usage.record_classified_usage_limit("codex")
    assert terminal["episode_id"] != warning_episode
    windows = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"]
    states = {window["kind"]: window["limit"]["state"] for window in windows}
    assert states["five_hour"] == "warning"
    assert states["unknown"] == "hard_limit"

    recovered = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(20), identity=("auth-a", "account-a"))
    assert [event["episode_id"] for event in recovered["limit_events"]] \
        == [warning_episode]
    windows = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"]
    states = {window["kind"]: window["limit"]["state"] for window in windows}
    assert states["five_hour"] == "recovered"
    assert states["unknown"] == "hard_limit"


def test_unknown_auth_terminal_does_not_invalidate_known_generation(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    episode_id = warning["limit_events"][0]["episode_id"]

    def missing_auth():
        raise RuntimeError("auth temporarily unavailable")

    patch_shared(monkeypatch, backend_usage, "_read_codex_auth", missing_auth)
    terminal = backend_usage.record_classified_usage_limit("codex")
    assert terminal["_additional_events"] == []
    row = backend_usage.db.conn().execute(
        "SELECT status,current_kind FROM provider_limit_episodes "
        "WHERE episode_id=?", (episode_id,),).fetchone()
    assert (row["status"], row["current_kind"]) == ("open", "warning")


def test_identity_secret_initialization_is_atomic_across_threads():
    backend_usage.db.conn()  # complete one-time schema/WAL setup before racing
    barrier = threading.Barrier(6)
    values = []
    errors = []

    def read_secret():
        try:
            barrier.wait()
            values.append(backend_usage._identity_secret())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=read_secret) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert errors == []
    assert len(values) == 6
    assert len(set(values)) == 1


@pytest.mark.parametrize("base", [
    "http://chatgpt.com/backend-api",
    "https://chatgpt.com.attacker.example/backend-api",
    "https://user@chatgpt.com/backend-api",
    "https://chatgpt.com:444/backend-api",
    "https://example.com/backend-api",
    "https://chatgpt.com/other",
])
def test_codex_usage_url_rejects_unapproved_origins(base):
    with pytest.raises(RuntimeError):
        backend_usage._codex_usage_url(base)


def test_codex_usage_url_accepts_exact_approved_origins():
    assert backend_usage._codex_usage_url("https://chatgpt.com") \
        == "https://chatgpt.com/backend-api/wham/usage"
    assert backend_usage._codex_usage_url(
        "https://chat.openai.com:443/backend-api/") \
        == "https://chat.openai.com/backend-api/wham/usage"


_WEEKLY_SPENT = {
    "rateLimits": {
        "primary": {"usedPercent": 100, "windowDurationMins": 10_080,
                    "resetsAt": 1_900_000_000},
        "secondary": {"usedPercent": 12, "windowDurationMins": 300,
                      "resetsAt": 1_800_010_000},
    }
}


def test_spent_weekly_window_marks_codex_exhausted_until_its_reset(monkeypatch):
    """The case that prompted this: weekly credits gone, five-hour window fine.

    Weekly windows raise no limit event, so nothing told a client the next
    send would fail.
    """
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits(
        _WEEKLY_SPENT, identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")

    assert backend_usage.exhausted_backends() == {"codex": {
        "state": "exhausted", "provider_id": "codex",
        "reason": "usage_limit", "window": "seven_day",
        "resets_at": "2030-03-17T17:46:40Z",
        "observed_at": "2027-01-15T08:00:00Z",
    }}


def test_stale_evidence_never_claims_exhaustion(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits(
        _WEEKLY_SPENT, identity=("auth-a", "account-a"),
        source_detail="account/rateLimits/read")

    later = 1_800_000_000_000 + backend_usage.CODEX_FRESH_MS + 1
    assert backend_usage.exhausted_backends(later) == {}


def test_classified_out_of_credits_is_exhausted_without_reset(monkeypatch):
    patch_shared(monkeypatch,
        backend_usage, "_read_codex_auth", lambda: ("token", "account"))
    backend_usage.record_classified_usage_limit("codex")

    quota = backend_usage.exhausted_backends()["codex"]
    assert quota["window"] == "unknown"
    assert quota["resets_at"] is None


def test_healthy_providers_are_not_listed(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits({
        "rateLimits": {"primary": {
            "usedPercent": 55, "windowDurationMins": 10_080,
            "resetsAt": 1_900_000_000}},
    }, identity=("auth-a", "account-a"))

    assert backend_usage.exhausted_backends() == {}


def test_refresh_worker_announces_only_a_changed_quota_picture(monkeypatch):
    sent = []

    class Stream:
        def broadcast(self, event): sent.append(event)

    picture = {"codex": {"window": "seven_day", "resets_at": "R"}}
    patch_shared(monkeypatch,
        backend_usage, "get_backend_usage", lambda: {"limit_events": []})
    patch_shared(monkeypatch,
        backend_usage, "exhausted_backends", lambda: dict(picture))
    worker = backend_usage.UsageRefreshWorker(stream=Stream())

    worker.run_once()          # first look: clients load the snapshot anyway
    worker.run_once()          # unchanged
    assert sent == []
    picture.clear()
    worker.run_once()          # recovered
    assert sent == [{"type": "agent-roster", "kind": "backend-quota"}]


def _depleted(rate_limit):
    """The shape /wham/usage answered for a workspace without credits."""
    return {
        "plan_type": "self_serve_business_prolite",
        "rate_limit": rate_limit,
        "credits": {"has_credits": False, "unlimited": False, "balance": None},
        "rate_limit_reached_type": {
            "type": "workspace_member_credits_depleted", "details": None},
        "rate_limit_upsell": {"reset_at": 1_900_000_000},
    }


def test_depleted_credits_are_told_apart_from_a_spent_window(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800,
                           "reset_at": 1_900_000_000},
        "secondary_window": None,
    }), identity=("auth-a", "account-a"), source_detail="/wham/usage")

    quota = backend_usage.exhausted_backends()["codex"]
    assert quota["reason"] == "credits_depleted"
    assert quota["window"] == "seven_day"
    assert quota["resets_at"] == "2030-03-17T17:46:40Z"


def test_blocked_response_without_any_window_is_still_exhausted(monkeypatch):
    """This used to raise "no usable windows" and record nothing at all."""
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": None, "secondary_window": None,
    }), identity=("auth-a", "account-a"), source_detail="/wham/usage")

    assert backend_usage.exhausted_backends()["codex"] == {
        "state": "exhausted", "provider_id": "codex",
        "reason": "credits_depleted", "window": "unknown",
        "resets_at": "2030-03-17T17:46:40Z", "observed_at": None,
    }
    later = 1_800_000_000_000 + backend_usage.CODEX_FRESH_MS + 1
    assert backend_usage.exhausted_backends(later) == {}


def test_windowless_response_that_is_not_blocked_still_fails(monkeypatch):
    with pytest.raises(RuntimeError, match="no usable windows"):
        backend_usage.capture_codex_rate_limits(
            {"rate_limit": {"allowed": True, "primary_window": None}},
            identity=("auth-a", "account-a"), source_detail="/wham/usage")


def test_failed_turn_borrows_the_reset_time_the_provider_stated(monkeypatch):
    """A classified usage-limit failure knows no reset time on its own."""
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    patch_shared(monkeypatch, backend_usage, "_read_codex_auth", lambda: ("token", "account"))
    identity = backend_usage._codex_identity("token", "account")
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": None, "secondary_window": None,
    }), identity=identity, source_detail="/wham/usage")
    backend_usage.record_classified_usage_limit("codex")

    quota = backend_usage.exhausted_backends()["codex"]
    assert quota["reason"] == "credits_depleted"
    assert quota["resets_at"] == "2030-03-17T17:46:40Z"


def test_refilled_account_clears_the_block(monkeypatch):
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": None, "secondary_window": None,
    }), identity=("auth-a", "account-a"), source_detail="/wham/usage")
    backend_usage.capture_codex_rate_limits({"rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": {"used_percent": 3, "limit_window_seconds": 604_800,
                           "reset_at": 1_900_000_000},
    }}, identity=("auth-a", "account-a"), source_detail="/wham/usage")

    assert backend_usage.exhausted_backends() == {}


def test_reconciling_limits_with_nothing_expired_takes_no_write_lock():
    from lib import db
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        assert backend_usage._reconcile_expired_limits(db.now_ms()) == []
    finally:
        db.conn().set_trace_callback(None)
    assert statements and not [s for s in statements if s.startswith("BEGIN")]


def test_antigravity_quota_comes_from_its_own_usage_command(tmp_path, monkeypatch):
    import os, pathlib, stat, sys
    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures/agy/1.3.0-usage.json"
    fake = tmp_path / "bin/agy"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == "
                    f"['--output-format', 'json', '-p', '/usage']\n"
                    f"sys.stdout.write(open({str(fixture)!r}).read())\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLARP_AGY_BIN", str(fake))
    patch_shared(monkeypatch, backend_usage, "_claude_needs_refresh", lambda: False)
    patch_shared(monkeypatch, backend_usage, "_codex_needs_refresh", lambda: False)
    payload = backend_usage.get_backend_usage(refresh_codex=False)
    agy = payload["providers"]["agy"]
    assert agy["source"]["kind"] == "provider_reported"
    windows = {(w["label"], w["kind"]): w for w in agy["windows"]}
    assert set(windows) == {("Gemini Models", "seven_day"), ("Gemini Models", "five_hour"),
                            ("Claude and GPT models", "seven_day"),
                            ("Claude and GPT models", "five_hour")}
    weekly = windows[("Gemini Models", "seven_day")]
    assert weekly["scope"] == "model_group" and 0 <= weekly["used_percentage"] <= 100
    assert weekly["resets_at"].endswith("Z")
    # agy limits are per model group; one empty group does not mark agy exhausted.
    assert "agy" not in backend_usage.exhausted_backends()


def test_a_failed_antigravity_usage_check_keeps_the_last_reading(tmp_path, monkeypatch):
    import os, stat
    fake = tmp_path / "bin/agy"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'not signed in' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLARP_AGY_BIN", str(fake))
    patch_shared(monkeypatch, backend_usage, "_claude_needs_refresh", lambda: False)
    agy = backend_usage.get_backend_usage(refresh_codex=False)["providers"]["agy"]
    assert agy["windows"] == [] and agy["source"]["kind"] == "unavailable"
    assert "not signed in" in (backend_usage._row("agy") or {}).get("error", "")


def test_a_failed_antigravity_usage_check_is_not_rerun_on_every_request(tmp_path, monkeypatch):
    import stat
    runs = tmp_path / "runs"
    fake = tmp_path / "bin/agy"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho run >> {runs}\necho 'not signed in' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLARP_AGY_BIN", str(fake))
    patch_shared(monkeypatch, backend_usage, "_claude_needs_refresh", lambda: False)
    patch_shared(monkeypatch, backend_usage, "_codex_needs_refresh", lambda: False)
    for _ in range(3):
        backend_usage.get_backend_usage(refresh_codex=False)
    assert runs.read_text().count("run") == 1


def test_a_hung_antigravity_usage_check_ends_with_its_helpers(tmp_path, monkeypatch):
    import stat, time
    fake = tmp_path / "bin/agy"
    fake.parent.mkdir()
    # A helper that inherits the output pipes and outlives agy itself.
    fake.write_text("#!/bin/sh\nsleep 60 &\nsleep 60\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLARP_AGY_BIN", str(fake))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        backend_usage.fetch_agy_usage(timeout=0.5)
    assert time.monotonic() - started < 5


def test_an_unused_antigravity_window_keeps_its_id_as_its_reset_slides():
    def row(reset):
        return {"fetched_at": 1, "used_percentage": 0.0, "source": "agy-usage-command",
                "raw": json.dumps({"groups": [{"name": "Gemini Models", "buckets": [
                    {"id": "g-5h", "window": "5h", "remaining_fraction": 1.0,
                     "reset_time": reset}]}]})}
    first = backend_usage._agy_windows(row("2026-10-06T15:00:00Z"), "pi", 2)
    later = backend_usage._agy_windows(row("2026-10-06T15:05:00Z"), "pi", 2)
    assert first[0]["window_id"] == later[0]["window_id"]


def test_antigravitys_answer_stands_when_a_helper_keeps_its_pipes_open(tmp_path, monkeypatch):
    import pathlib, stat, sys
    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures/agy/1.3.0-usage.json"
    fake = tmp_path / "bin/agy"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\ncat {fixture}\nsleep 60 &\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLARP_AGY_BIN", str(fake))
    assert backend_usage.fetch_agy_usage(timeout=1.0)["groups"]


def _write_codex_auth(codex_home, token: str, account_id: str) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": token, "account_id": account_id},
    }))


def test_switching_codex_auth_profile_clears_stale_exhaustion_before_next_poll(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])

    _write_codex_auth(codex_home, "token-profile-a", "acct-a")
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800,
                           "reset_at": 1_900_000_000},
        "secondary_window": None,
    }), source_detail="/wham/usage")
    assert backend_usage.exhausted_backends()["codex"]["reason"] == "credits_depleted"
    assert backend_usage._codex_needs_refresh() is False

    # Hotseat switches auth.json to profile B before UsageRefreshWorker polls again.
    _write_codex_auth(codex_home, "token-profile-b", "acct-b")
    assert backend_usage.exhausted_backends() == {}
    assert backend_usage._codex_needs_refresh() is True


def test_authoritative_codex_refresh_resolves_classified_terminal_and_caches_weekly_only_plan(monkeypatch):
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    patch_shared(monkeypatch, backend_usage, "_read_codex_auth", lambda: ("token-a", "acct-a"))

    terminal = backend_usage.record_classified_usage_limit("codex")
    assert backend_usage.exhausted_backends()["codex"]["window"] == "unknown"

    now[0] += 1_000
    refreshed = backend_usage.capture_codex_rate_limits({
        "plan_type": "self_serve_business_prolite",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {"used_percent": 18, "limit_window_seconds": 604_800,
                               "reset_at": 1_900_000_000},
            "secondary_window": None,
        },
    }, source_detail="/wham/usage")

    assert any(
        ev["episode_id"] == terminal["episode_id"] and ev["kind"] == "recovered"
        for ev in refreshed["limit_events"]
    )
    assert backend_usage.exhausted_backends() == {}
    assert backend_usage._codex_needs_refresh() is False


def test_completed_codex_turn_reconciles_stale_exhaustion_until_new_failure(monkeypatch):
    from lib import agents, db, snapshot as snapshot_mod, turn_lifecycle

    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    monkeypatch.setattr(turn_lifecycle, "now_ms", lambda: now[0])
    patch_shared(monkeypatch, backend_usage, "_read_codex_auth", lambda: ("token-a", "acct-a"))

    aid = agents.create_agent(persona="Gordon", voice_id="v", cwd="/tmp", session="gpt-6-luna", backend="codex")
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800,
                           "reset_at": 1_900_000_000},
        "secondary_window": None,
    }), source_detail="/wham/usage")
    backend_usage.record_classified_usage_limit("codex")

    snap_before = snapshot_mod.build_agent_snapshot(None)
    row_before = next(r for r in snap_before["agents"] if r["agent_id"] == aid)
    assert row_before["backend_quota"]["state"] == "exhausted"

    # A live Codex turn succeeds 5 seconds later, proving the active credentials can serve turns.
    now[0] += 5_000
    agents.record_state(aid, "thinking", {"source": "user"})
    agents.record_state(aid, "done", {"source": "user"})

    assert backend_usage.exhausted_backends() == {}
    assert backend_usage._codex_needs_refresh() is True
    snap_after = snapshot_mod.build_agent_snapshot(None)
    row_after = next(r for r in snap_after["agents"] if r["agent_id"] == aid)
    assert row_after["backend_quota"] is None

    # A subsequent turn failing 200ms later (within the same wall-clock second) still reports live exhaustion.
    now[0] += 200
    backend_usage.record_classified_usage_limit("codex")
    assert "codex" in backend_usage.exhausted_backends()


def _write_claude_auth(
    claude_dir,
    token: str,
    *,
    config_file=None,
    account_uuid: str = "",
    org_uuid: str = "",
    email: str = "",
    profile_alias: str = "",
) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": 0,
            "subscriptionType": "max",
        }
    }))
    if config_file is not None and (account_uuid or org_uuid or email):
        config_file.write_text(json.dumps({
            "oauthAccount": {
                "accountUuid": account_uuid,
                "organizationUuid": org_uuid,
                "emailAddress": email,
            }
        }))
    if profile_alias:
        profiles = claude_dir / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / ".current_profile").write_text(profile_alias)


def _mock_claude_usage_response(monkeypatch, five_hour: float, seven_day: float = 20.0) -> None:
    body = json.dumps({
        "five_hour": {"utilization": five_hour, "resets_at": "2030-01-01T05:00:00Z"},
        "seven_day_oauth_apps": {"utilization": seven_day, "resets_at": "2030-01-07T00:00:00Z"},
    }).encode()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return body

    monkeypatch.setattr(
        backend_usage.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )


def test_same_account_token_refresh_keeps_codex_and_claude_windows_without_refetch(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    claude_dir = tmp_path / "claude"
    claude_cfg_file = tmp_path / "claude.json"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_FILE", str(claude_cfg_file))

    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])

    _write_codex_auth(codex_home, "tok-1", "acct-a")
    _write_claude_auth(
        claude_dir, "ctok-1",
        config_file=claude_cfg_file,
        account_uuid="u-1", org_uuid="o-1", email="a@example.com",
        profile_alias="ai10",
    )
    backend_usage.capture_codex_rate_limits({
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {"used_percent": 40, "limit_window_seconds": 18_000, "reset_at": 1_900_000_000},
            "secondary_window": {"used_percent": 60, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
        },
    }, source_detail="/wham/usage")
    _mock_claude_usage_response(monkeypatch, 30.0, 45.0)
    backend_usage.fetch_claude_usage()

    assert len(backend_usage._structured_provider("codex", now[0])["windows"]) == 2
    assert len(backend_usage._structured_provider("claude", now[0])["windows"]) == 2
    assert backend_usage._codex_needs_refresh() is False
    assert backend_usage._claude_needs_refresh() is False

    # Same accounts refresh their short-lived OAuth access tokens in place.
    _write_codex_auth(codex_home, "tok-2", "acct-a")
    _write_claude_auth(
        claude_dir, "ctok-2",
        config_file=claude_cfg_file,
        account_uuid="u-1", org_uuid="o-1", email="a@example.com",
        profile_alias="ai10",
    )
    assert len(backend_usage._structured_provider("codex", now[0])["windows"]) == 2
    assert len(backend_usage._structured_provider("claude", now[0])["windows"]) == 2
    assert backend_usage._codex_needs_refresh() is False
    assert backend_usage._claude_needs_refresh() is False

    # Bare .credentials.json without oauthAccount/profile marker also preserves windows across token refresh.
    bare_dir = tmp_path / "claude-bare"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(bare_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_FILE", raising=False)
    _write_claude_auth(bare_dir, "bare-1")
    backend_usage.fetch_claude_usage()
    _write_claude_auth(bare_dir, "bare-2")
    assert len(backend_usage._structured_provider("claude", now[0])["windows"]) == 2
    assert backend_usage._claude_needs_refresh() is False


def test_switching_claude_account_clears_stale_windows_and_exhaustion_immediately(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_cfg_file = tmp_path / "claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_FILE", str(claude_cfg_file))

    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])

    _write_claude_auth(
        claude_dir, "ctok-a",
        config_file=claude_cfg_file,
        account_uuid="u-a", org_uuid="o-a", email="a@example.com",
        profile_alias="ai9",
    )
    _mock_claude_usage_response(monkeypatch, 100.0, 80.0)
    backend_usage.fetch_claude_usage()

    assert "claude" in backend_usage.exhausted_backends()
    assert len(backend_usage._structured_provider("claude", now[0])["windows"]) == 2
    assert backend_usage._claude_needs_refresh() is False
    payload_text = json.dumps(backend_usage.get_backend_usage(refresh_codex=False))
    assert "account_scope_ref" not in payload_text
    assert "claude-account-" not in payload_text

    # Hotseat switches active Claude account to ai7 before next poll.
    _write_claude_auth(
        claude_dir, "ctok-b",
        config_file=claude_cfg_file,
        account_uuid="u-b", org_uuid="o-b", email="b@example.com",
        profile_alias="ai7",
    )
    assert backend_usage._structured_provider("claude", now[0])["windows"] == []
    assert "claude" not in backend_usage.exhausted_backends()
    assert backend_usage._claude_needs_refresh() is True


def test_switching_codex_back_to_previously_observed_account_triggers_refresh(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])

    # Observe healthy acct-b first so provider_usage_windows already has fresh rows for acct-b.
    _write_codex_auth(codex_home, "tok-b", "acct-b")
    backend_usage.capture_codex_rate_limits({
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {"used_percent": 20, "limit_window_seconds": 18_000, "reset_at": 1_900_000_000},
            "secondary_window": {"used_percent": 30, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
        },
    }, source_detail="/wham/usage")
    assert backend_usage._codex_needs_refresh() is False

    # Switch to exhausted acct-a and observe it, overwriting the single-row backend_usage CODEX entry.
    now[0] += 1_000
    _write_codex_auth(codex_home, "tok-a", "acct-a")
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
        "secondary_window": None,
    }), source_detail="/wham/usage")
    assert "codex" in backend_usage.exhausted_backends()

    # Switch back to acct-b within the 5-minute window: exhaustion clears at once AND legacy row refreshes.
    now[0] += 1_000
    _write_codex_auth(codex_home, "tok-b", "acct-b")
    assert backend_usage.exhausted_backends() == {}
    assert [w["used_percentage"] for w in backend_usage._structured_provider("codex", now[0])["windows"]] == [20.0, 30.0]
    assert backend_usage._codex_needs_refresh() is True


def test_completed_turn_reconciliation_requires_newer_same_backend_turn_for_codex_and_claude(tmp_path, monkeypatch):
    from lib import agents, db, turn_lifecycle

    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    monkeypatch.setattr(turn_lifecycle, "now_ms", lambda: now[0])

    codex_home = tmp_path / "codex"
    claude_dir = tmp_path / "claude"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    _write_codex_auth(codex_home, "tok-a", "acct-a")
    _write_claude_auth(claude_dir, "ctok-a")

    codex_aid = agents.create_agent(persona="G", voice_id="v", cwd="/tmp", session="g-1", backend="codex")
    claude_aid = agents.create_agent(persona="K", voice_id="v", cwd="/tmp", session="k-1", backend="claude")

    # Turns completed BEFORE exhaustion is observed must never clear the later exhaustion.
    agents.record_state(codex_aid, "thinking", {"source": "user"})
    agents.record_state(codex_aid, "done", {"source": "user"})
    agents.record_state(claude_aid, "thinking", {"source": "user"})
    agents.record_state(claude_aid, "done", {"source": "user"})

    now[0] += 5_000
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
        "secondary_window": None,
    }), source_detail="/wham/usage")
    _mock_claude_usage_response(monkeypatch, 100.0, 50.0)
    backend_usage.fetch_claude_usage()

    assert set(backend_usage.exhausted_backends()) == {"codex", "claude"}

    # Completing a Claude turn clears ONLY Claude, leaving Codex exhausted.
    now[0] += 5_000
    agents.record_state(claude_aid, "thinking", {"source": "user"})
    agents.record_state(claude_aid, "done", {"source": "user"})
    assert set(backend_usage.exhausted_backends()) == {"codex"}

    # Re-exhaust Claude, then complete a Codex turn: clears ONLY Codex, leaving Claude exhausted.
    now[0] += 5_000
    backend_usage.fetch_claude_usage()
    assert set(backend_usage.exhausted_backends()) == {"codex", "claude"}

    now[0] += 5_000
    agents.record_state(codex_aid, "thinking", {"source": "user"})
    agents.record_state(codex_aid, "done", {"source": "user"})
    assert set(backend_usage.exhausted_backends()) == {"claude"}


def test_authoritative_codex_refresh_at_100_percent_keeps_unknown_terminal_until_sub_100(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    now = [1_800_000_000_000]
    patch_shared(monkeypatch, backend_usage, "_now_ms", lambda: now[0])

    _write_codex_auth(codex_home, "tok-1", "acct-a")
    terminal = backend_usage.record_classified_usage_limit("codex")

    # Authoritative payload with allowed=True (no block) BUT window at 100% must NOT close unknown terminal.
    now[0] += 1_000
    still_full = backend_usage.capture_codex_rate_limits({
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
            "secondary_window": None,
        },
    }, source_detail="/wham/usage")
    assert not any(
        ev["episode_id"] == terminal["episode_id"] and ev["kind"] == "recovered"
        for ev in still_full["limit_events"]
    )
    unknown_rows = backend_usage.db.conn().execute(
        "SELECT COUNT(*) FROM provider_usage_windows WHERE window_kind='unknown'"
    ).fetchone()[0]
    assert unknown_rows == 1

    # Even if access token rotated on same account, authoritative sub-100% refresh cleans up unknown window.
    now[0] += 1_000
    _write_codex_auth(codex_home, "tok-2", "acct-a")
    backend_usage.capture_codex_rate_limits({
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {"used_percent": 25, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
            "secondary_window": None,
        },
    }, source_detail="/wham/usage")
    unknown_after = backend_usage.db.conn().execute(
        "SELECT COUNT(*) FROM provider_usage_windows WHERE window_kind='unknown'"
    ).fetchone()[0]
    assert unknown_after == 0
    assert backend_usage.exhausted_backends() == {}


def test_snapshot_and_exhaustion_take_no_write_lock_behind_active_writer(tmp_path, monkeypatch):
    import sqlite3
    import time
    from lib import agents, db, snapshot as snapshot_mod

    codex_home = tmp_path / "codex"
    claude_dir = tmp_path / "claude"
    claude_cfg_file = tmp_path / "claude.json"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_FILE", str(claude_cfg_file))
    _write_codex_auth(codex_home, "tok-1", "acct-a")
    _write_claude_auth(
        claude_dir, "ctok-1",
        config_file=claude_cfg_file,
        account_uuid="u-1", org_uuid="o-1", email="a@example.com",
    )
    agents.create_agent(persona="G", voice_id="v", cwd="/tmp", session="g-lock", backend="codex")

    def _assert_zero_writes_under_writer_lock():
        seen: list[str] = []
        db.conn().set_trace_callback(seen.append)
        try:
            backend_usage.exhausted_backends()
            snapshot_mod.build_agent_snapshot(None)
        finally:
            db.conn().set_trace_callback(None)
        writes = [
            s for s in seen
            if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "BEGIN"))
        ]
        assert writes == []

        held = threading.Event()
        release = threading.Event()

        def writer():
            c = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
            c.execute("BEGIN IMMEDIATE")
            held.set()
            release.wait(10)
            c.execute("COMMIT")
            c.close()

        th = threading.Thread(target=writer)
        th.start()
        assert held.wait(5)
        try:
            t0 = time.perf_counter()
            backend_usage.exhausted_backends()
            snapshot_mod.build_agent_snapshot(None)
            elapsed = time.perf_counter() - t0
            assert elapsed < 1.0, f"Read path blocked behind writer lock for {elapsed:.2f}s"
        finally:
            release.set()
            th.join(5)

    # 1. Cold DB (before provider_usage_identity_secret exists in settings):
    # warm one-time server_identity / builtin janitor / preview cache bootstrap, then assert 0 writes.
    snapshot_mod.build_agent_snapshot(None)
    assert backend_usage.settings_store.get(backend_usage._IDENTITY_SECRET_KEY) is None
    _assert_zero_writes_under_writer_lock()

    # 2. Warm DB with both healthy and exhausted observations + indexed state_log seek check.
    backend_usage.capture_codex_rate_limits(_depleted({
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604_800, "reset_at": 1_900_000_000},
        "secondary_window": None,
    }), source_detail="/wham/usage")
    _mock_claude_usage_response(monkeypatch, 30.0, 40.0)
    backend_usage.fetch_claude_usage()
    # Warm dashboard preview cache once outside lock, then verify steady-state snapshot + exhaustion.
    snapshot_mod.build_agent_snapshot(None)
    _assert_zero_writes_under_writer_lock()

    plan = db.conn().execute(
        """EXPLAIN QUERY PLAN
           SELECT MAX((
               SELECT s.ts FROM state_log s
                WHERE s.agent_id = a.agent_id
                  AND s.ts > ? AND s.ts <= ? AND s.kind = 'done'
                ORDER BY s.ts DESC LIMIT 1
           )) FROM agents a WHERE a.backend = ?""",
        (1, 2, "codex"),
    ).fetchall()
    plan_text = " | ".join(str(tuple(r)) for r in plan)
    assert "idx_state_log_agent_ts" in plan_text
