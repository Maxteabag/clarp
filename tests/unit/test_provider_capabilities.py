from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import concurrent.futures

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import provider_capabilities as capabilities  # noqa: E402


OBSERVED_AT = "2026-08-26T10:00:00Z"


def test_codex_parser_preserves_model_specific_efforts_and_unknowns():
    raw = json.dumps({
        "models": [
            {
                "slug": "gpt-wide",
                "display_name": "GPT Wide",
                "visibility": "list",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "xhigh"},
                    {"effort": "ultra"},
                    {"unexpected": True},
                ],
            },
            {
                "slug": "gpt-partial",
                "visibility": "list",
            },
            {
                "slug": "gpt-literal-empty",
                "visibility": "list",
                "supported_reasoning_levels": [],
            },
            {
                "slug": "gpt-invalid-levels",
                "visibility": "list",
                "supported_reasoning_levels": [
                    {"effort": 123}, {"effort": None}, {"wrong": "high"},
                ],
            },
            {
                "slug": "gpt-hidden",
                "visibility": "hide",
                "supported_reasoning_levels": [{"effort": "high"}],
            },
            {"display_name": "missing id", "visibility": "list"},
            {"slug": 123, "display_name": "numeric id", "visibility": "list"},
            "malformed",
        ]
    })

    models = capabilities.parse_codex_models(raw, observed_at=OBSERVED_AT)

    assert [model["id"] for model in models] == [
        "gpt-wide", "gpt-partial", "gpt-literal-empty",
        "gpt-invalid-levels",
    ]
    assert models[0]["label"] == "GPT Wide"
    assert models[0]["default_effort"] == "medium"
    assert models[0]["supported_efforts"] == ["low", "xhigh", "ultra"]
    assert models[1]["label"] == "gpt-partial"
    assert models[1]["default_effort"] is None
    assert models[1]["supported_efforts"] is None
    assert models[1]["source"]["kind"] == "cli_probe"
    assert models[2]["supported_efforts"] == []
    assert models[3]["supported_efforts"] is None


def test_codex_parser_rejects_malformed_and_non_catalog_payloads():
    assert capabilities.parse_codex_models("not json", observed_at=OBSERVED_AT) == []
    assert capabilities.parse_codex_models("[]", observed_at=OBSERVED_AT) == []
    assert capabilities.parse_codex_models(
        '{"models":"wrong"}', observed_at=OBSERVED_AT) == []


def test_agy_parser_handles_tabs_partial_rows_noise_and_duplicates():
    raw = "\n".join([
        "Fetching available models...",
        "gemini-fast\tGemini Fast",
        "gemini-partial",
        "gemini-empty-label\t",
        "\tMissing ID",
        "gemini-fast\tDuplicate",
        "Loading...",
        "Warning:",
        "Gemini-Fast\tUppercase noise",
        "gemini fast\tWhitespace",
        "gemini_fast\tPunctuation",
        "gemini!fast",
        "123",
        "",
    ])

    models = capabilities.parse_agy_models(raw, observed_at=OBSERVED_AT)

    assert [model["id"] for model in models] == [
        "gemini-fast", "gemini-partial", "gemini-empty-label"]
    assert [model["label"] for model in models] == [
        "Gemini Fast", "gemini-partial", "gemini-empty-label"]
    assert models[0]["supported_efforts"] is None
    assert models[0]["default_effort"] is None


def test_discovery_uses_unknown_not_false_when_installed_probe_is_unusable():
    def fake_run(argv, **_kwargs):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="provider 1.2.3\n", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="malformed output with spaces", stderr="")

    result = capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda binary: f"/bin/{binary}",
        run=fake_run,
    )

    codex = result["providers"]["codex"]
    assert codex["installed"] is True
    assert codex["availability"] == "unknown"
    assert codex["cli_version"] == "provider 1.2.3"
    assert codex["cli_version_source"]["kind"] == "cli_probe"
    assert codex["authenticated"] is None
    assert codex["authentication_source"]["freshness"] == "unknown"
    assert codex["source"]["freshness"] == "unknown"
    assert codex["models"][0]["supported_efforts"] is None
    assert codex["models"][0]["source"]["kind"] == "static_fallback"


def test_missing_cli_is_explicitly_unavailable_but_model_fallback_is_unknown():
    result = capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda _binary: None,
        run=lambda *_args, **_kwargs: None,
    )

    agy = result["providers"]["agy"]
    assert agy["installed"] is False
    assert agy["availability"] == "unavailable"
    assert agy["cli_version"] is None
    assert agy["authenticated"] is None
    assert agy["models"][0]["source"]["freshness"] == "unknown"
    assert agy["models"][0]["default_effort"] is None
    assert agy["models"][0]["supported_efforts"] is None
    assert agy["supported_efforts"] == ["low", "medium", "high"]
    assert agy["supported_efforts_scope"] == "provider_flag"
    assert agy["model_effort_compatibility"] == "unknown"
    assert "grok" in result["providers"]
    assert "opencode" in result["providers"]
    assert result["providers"]["grok"]["label"] == "Grok"
    assert result["providers"]["opencode"]["label"] == "OpenCode"


def test_parse_grok_and_opencode_models():
    grok = capabilities.parse_grok_models(
        "You are logged in with grok.com.\n\nDefault model: grok-4.6\n\n"
        "Available models:\n  * grok-4.6 (default)\n  - grok-4.5\n",
        observed_at="2026-09-02T00:00:00Z",
    )
    assert [item["id"] for item in grok] == ["grok-4.6", "grok-4.5"]
    opencode = capabilities.parse_opencode_models(
        "anthropic/claude-sonnet-4-5\nopencode/gpt-5.4\n",
        observed_at="2026-09-02T00:00:00Z",
    )
    assert [item["id"] for item in opencode] == [
        "anthropic/claude-sonnet-4-5", "opencode/gpt-5.4"]


def test_claude_resolution_honors_configured_runtime_binary(monkeypatch):
    from lib import clarp_runner

    seen: list[str] = []
    monkeypatch.setattr(
        clarp_runner, "configured_claude_bin", lambda: "clarp-custom")
    monkeypatch.setattr(
        capabilities.shutil, "which",
        lambda binary: seen.append(binary) or f"/resolved/{binary}",
    )

    assert capabilities._resolve_executable("claude") == "/resolved/clarp-custom"
    assert capabilities._resolve_executable("codex") == "/resolved/codex"
    assert seen == ["clarp-custom", "codex"]


def test_agy_model_validation_uses_observed_catalog(monkeypatch):
    catalog = capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda _binary: None,
        run=lambda *_args, **_kwargs: None,
    )
    catalog["providers"]["agy"]["source"]["kind"] = "cli_probe"
    catalog["providers"]["agy"]["models"] = [{"id": "gemini-3.7-flash-low"}]
    monkeypatch.setattr(capabilities, "_cache", (0.0, catalog))
    assert capabilities.is_dispatchable_agy_model("gemini-3.7-flash-low")
    assert not capabilities.is_dispatchable_agy_model("gemini-4.8-flash")


def test_agy_model_validation_survives_restart_from_persisted_catalog(monkeypatch):
    from lib import settings_store
    settings_store.set_text(
        capabilities._AGY_LAST_CATALOG_KEY,
        '["gemini-future-flash-low"]')
    monkeypatch.setattr(capabilities, "_cache", None)
    assert capabilities.is_dispatchable_agy_model("gemini-future-flash-low")
    assert not capabilities.is_dispatchable_agy_model("gemini-3.5-flash-low")
    assert not capabilities.is_dispatchable_agy_model("gemini-other-flash-low")


def test_cache_reuses_one_observation_until_forced(monkeypatch):
    capabilities.clear_cache()
    calls: list[list[str]] = []
    # Monotonic readings the cache logic should observe, in order. Patching
    # the global clock also feeds unrelated callers (SQLite tracing, thread
    # pools), so hold the last value instead of exhausting an iterator.
    readings = [10.0, 10.0, 20.0, 30.0, 30.0]

    def clock() -> float:
        return readings.pop(0) if len(readings) > 1 else readings[0]

    monkeypatch.setattr(capabilities.time, "monotonic", clock)
    monkeypatch.setattr(capabilities.time, "time", lambda: 1787738400 + len(calls))
    monkeypatch.setattr(
        capabilities.shutil, "which",
        lambda binary: f"/bin/{binary}" if binary == "agy" else None,
    )

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="agy 1.1.21\n", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="agy-one\tAGY One\n", stderr="")

    monkeypatch.setattr(capabilities, "_run", fake_run)

    first = capabilities.capability_catalog()
    second = capabilities.capability_catalog()
    forced = capabilities.capability_catalog(force_refresh=True)

    assert first["observed_at"] == second["observed_at"]
    assert first["freshness"] == second["freshness"] == "fresh"
    assert len(calls) == 4
    assert forced["providers"]["agy"]["models"][0]["id"] == "agy-one"
    capabilities.clear_cache()


def test_provider_discovery_runs_concurrently():
    barrier = threading.Barrier(5)
    seen: list[str] = []

    def resolve(provider_id: str):
        seen.append(provider_id)
        barrier.wait(timeout=1)
        return None

    capabilities._build_catalog(
        wall_time=1787738400,
        which=resolve,
        run=lambda *_args, **_kwargs: None,
    )

    assert set(seen) == {"claude", "codex", "agy", "grok", "opencode"}


def test_cache_refresh_is_single_flight_for_concurrent_callers(monkeypatch):
    capabilities.clear_cache()
    catalog = capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda _binary: None,
        run=lambda *_args, **_kwargs: None,
    )
    release = threading.Event()
    all_waiting = threading.Event()
    calls = 0
    waiter_count = 0
    original_wait = capabilities._cache_condition.wait

    def counting_wait(*args, **kwargs):
        nonlocal waiter_count
        waiter_count += 1
        if waiter_count == 5:
            all_waiting.set()
        return original_wait(*args, **kwargs)

    def build(**_kwargs):
        nonlocal calls
        calls += 1
        assert release.wait(timeout=2)
        return catalog

    monkeypatch.setattr(capabilities._cache_condition, "wait", counting_wait)
    monkeypatch.setattr(capabilities, "_build_catalog", build)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(capabilities.capability_catalog)
                   for _ in range(6)]
        assert all_waiting.wait(timeout=2)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert calls == 1
    assert all(result["observed_at"] == catalog["observed_at"]
               for result in results)
    capabilities.clear_cache()


def test_single_flight_failure_notifies_waiters_and_allows_retry(monkeypatch):
    capabilities.clear_cache()
    catalog = capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda _binary: None,
        run=lambda *_args, **_kwargs: None,
    )
    release = threading.Event()
    all_waiting = threading.Event()
    calls = 0
    waiter_count = 0
    original_wait = capabilities._cache_condition.wait

    def counting_wait(*args, **kwargs):
        nonlocal waiter_count
        waiter_count += 1
        if waiter_count == 4:
            all_waiting.set()
        return original_wait(*args, **kwargs)

    def flaky_build(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert release.wait(timeout=2)
            raise ValueError("probe failed")
        return catalog

    monkeypatch.setattr(capabilities._cache_condition, "wait", counting_wait)
    monkeypatch.setattr(capabilities, "_build_catalog", flaky_build)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(capabilities.capability_catalog)
                   for _ in range(5)]
        assert all_waiting.wait(timeout=2)
        release.set()
        errors = []
        for future in futures:
            try:
                future.result(timeout=2)
            except (RuntimeError, ValueError) as error:
                errors.append(error)

    assert len(errors) == 5
    assert calls == 1
    assert capabilities.capability_catalog()["schema_version"] == 2
    assert calls == 2
    capabilities.clear_cache()


def test_top_level_freshness_is_cache_age_not_model_evidence(monkeypatch):
    catalog = capabilities._build_catalog(
        wall_time=1787738400,
        which=lambda _binary: None,
        run=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        capabilities, "capability_catalog", lambda **_kwargs: catalog)

    response = capabilities.endpoint_response()

    assert response["freshness"] == "fresh"
    assert response["freshness_scope"] == "response_observation"
    assert response["providers"]["codex"]["source"]["freshness"] == "unknown"
    assert "cache age" in response["freshness_note"]


def test_claude_models_come_from_the_account_cache_not_a_bundled_guess():
    raw = json.dumps({
        "additionalModelOptionsCache": [
            {"value": "claude-fable-5-1[1m]", "label": "Fable",
             "description": "Fable 5.1 \u00b7 Most capable for long tasks"},
            # An upgrade prompt the CLI renders in the same menu; --model would
            # reject it, so it must not reach the picker.
            {"value": "cc-update-required-1",
             "label": "Update to 2.1.255+ to use Fable 5.1"},
        ],
        "modelAccessCache": [{"value": "claude-opus-5", "label": "Opus 5"}],
    })

    models = capabilities.parse_claude_models(raw, observed_at="2026-09-02T00:00:00Z")

    assert [item["id"] for item in models] == ["claude-opus-5", "claude-fable-5-1[1m]"]
    # The label carries the version; "Fable" alone does not say which model.
    assert models[1]["label"] == "Fable 5.1"
    assert all(item["source"]["kind"] == "cli_cache" for item in models)


def test_claude_account_models_are_offered_alongside_the_bundled_list():
    observed = capabilities.parse_claude_models(
        json.dumps({"modelAccessCache": [{"value": "claude-fable-5-1[1m]",
                                          "label": "Fable 5.1"}]}),
        observed_at="2026-09-02T00:00:00Z")
    bundled = {model_id for model_id, _ in capabilities._fallback_model_rows("claude")}

    # An entitlement the bundled list cannot know about, and the stable ids the
    # account cache never lists, both have to be selectable.
    assert observed[0]["id"] not in bundled
    assert {"claude-opus-5", "sonnet"} <= bundled


def test_claude_parser_ignores_junk_without_raising():
    assert capabilities.parse_claude_models("not json", observed_at="x") == []
    assert capabilities.parse_claude_models("[]", observed_at="x") == []
    assert capabilities.parse_claude_models(
        json.dumps({"modelAccessCache": "nope"}), observed_at="x") == []


def test_claude_fallback_names_current_models_and_cli_aliases():
    bundled = [model_id for model_id, _ in capabilities._fallback_model_rows("claude")]
    # Retired ids must not be offered: the API rejects them with a 400.
    assert "claude-fable-5" not in bundled
    assert "claude-fable-5-1" in bundled
    # The aliases Claude Code resolves itself; "default" is not one of them.
    assert "fable" in bundled
    assert capabilities._is_dispatchable_claude_model("fable")
    assert capabilities._is_dispatchable_claude_model("opus[1m]")
    assert not capabilities._is_dispatchable_claude_model("default")
    assert not capabilities._is_dispatchable_claude_model("cc-update-required-1")


def test_claude_model_pin_drops_legacy_default():
    assert capabilities.claude_model_pin("default") == ""
    assert capabilities.claude_model_pin(" DEFAULT ") == ""
    assert capabilities.claude_model_pin(None) == ""
    assert capabilities.claude_model_pin("claude-fable-5-1[1m]") == "claude-fable-5-1[1m]"


def test_catalog_rows_advertise_presentation_and_flags():
    from lib import provider_capabilities
    observed = provider_capabilities._iso_now(0)
    row = provider_capabilities._discover_provider(
        "opencode", observed, resolve=lambda _name: None,
        run=lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert row["installed"] is False
    assert row["label"] == "OpenCode" and row["detail"] == "Runs on OpenCode."
    assert row["brand"]["tint_dark"] == "#5ee4b5"
    assert row["supports_compact"] is False and row["supports_resume"] is True
    assert row["effort_ui"] == "picker" and row["login_kind"] == "none"
    # Provider-wide efforts still surface for a provider-scoped CLI.
    assert row["supported_efforts"] == ["low", "medium", "high", "max"]

    agy = provider_capabilities._discover_provider(
        "agy", observed, resolve=lambda _name: None,
        run=lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert agy["supported_efforts_scope"] == "provider_flag"
    assert agy["model_effort_compatibility"] == "unknown"
    assert agy["effort_ui"] == "folded_into_model"

    claude = provider_capabilities._discover_provider(
        "claude", observed, resolve=lambda _name: None,
        run=lambda *a, **k: (_ for _ in ()).throw(OSError()))
    # Claude efforts are per model, so the provider row carries none.
    assert claude["supported_efforts"] is None
    assert claude["supports_mcp"] is True and claude["sort_index"] == 0
