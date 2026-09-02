from __future__ import annotations

import json
import threading

import pytest

from lib import backend_usage


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


def test_claude_oauth_usage_normalizes_windows(monkeypatch):
    body = json.dumps({
        "five_hour": {"utilization": 37.0, "resets_at": "2030-01-01T05:00:00Z"},
        "seven_day_oauth_apps": {
            "utilization": 12.0, "resets_at": "2030-01-07T00:00:00Z"},
    }).encode()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return body

    monkeypatch.setattr(backend_usage, "_read_claude_auth",
                        lambda: ("token", 0, "max_20x"))
    monkeypatch.setattr(backend_usage.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response())

    result = backend_usage.fetch_claude_usage()
    assert result["windows"]["five_hour"]["used_percentage"] == 37
    assert result["windows"]["seven_day"]["window_minutes"] == 10_080
    snapshot = backend_usage.get_backend_usage(refresh_codex=False)["providers"]["claude"]
    assert [window["kind"] for window in snapshot["windows"]] == ["five_hour", "seven_day"]


def test_codex_direct_failure_uses_app_server_fallback(monkeypatch):
    monkeypatch.setattr(backend_usage, "_claude_needs_refresh", lambda: False)
    monkeypatch.setattr(backend_usage, "_codex_needs_refresh", lambda: True)
    monkeypatch.setattr(backend_usage, "fetch_codex_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("direct down")))
    called = []
    monkeypatch.setattr(backend_usage, "fetch_codex_usage_app_server",
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

    monkeypatch.setattr(backend_usage, "fetch_codex_usage", fake_fetch)
    monkeypatch.setattr(backend_usage, "_codex_needs_refresh", lambda: False)

    backend_usage.get_backend_usage(refresh_codex=True)
    assert calls == 0

    backend_usage.get_backend_usage(refresh_codex=True, force_codex=True)
    assert calls == 1


def test_fresh_legacy_row_does_not_suppress_first_structured_refresh(monkeypatch):
    now = 1_800_000_000_000
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
    backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(95, resets_at=1_900_000_000),
        identity=("auth-a", "account-a"))
    known_window_id = backend_usage.get_backend_usage(
        refresh_codex=False)["providers"]["codex"]["windows"][0]["window_id"]
    monkeypatch.setattr(
        backend_usage, "_read_codex_auth", lambda: ("token-a", "account-a"))
    monkeypatch.setattr(
        backend_usage, "_codex_identity",
        lambda _token, _account: ("auth-a", "account-a"))
    now[0] += backend_usage.CODEX_FRESH_MS + 1

    event = backend_usage.record_classified_usage_limit("codex")
    assert event["used_percentage"] is None
    assert event["resets_at"] is None
    assert event["window_id"] != known_window_id


def test_classified_hard_limit_decays_to_unknown_when_stale(monkeypatch):
    now = [1_800_000_000_000]
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: now[0])
    monkeypatch.setattr(
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    warning_episode = warning["limit_events"][0]["episode_id"]
    monkeypatch.setattr(
        backend_usage, "_read_codex_auth", lambda: ("token-a", "account-a"))
    monkeypatch.setattr(
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
    monkeypatch.setattr(backend_usage, "_now_ms", lambda: 1_800_000_000_000)
    warning = backend_usage.capture_codex_rate_limits(
        _codex_rate_limits(85), identity=("auth-a", "account-a"))
    episode_id = warning["limit_events"][0]["episode_id"]

    def missing_auth():
        raise RuntimeError("auth temporarily unavailable")

    monkeypatch.setattr(backend_usage, "_read_codex_auth", missing_auth)
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
