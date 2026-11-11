from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import threading
from types import SimpleNamespace

import pytest

from lib import (
    agents,
    background_job_watcher,
    background_jobs,
    config,
    contact_assignment,
    contact_minting,
    events,
    persona_avatar_generation,
    personas,
    roster,
    service_manager,
    snapshot,
)
from lib.agent_lifecycle import AgentLifecycleService
from lib.routes.personas import PersonaRoutes
from tests.patching import patch_shared


SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/persona_avatar_job.py"
SPEC = importlib.util.spec_from_file_location("persona_avatar_job_test", SCRIPT)
worker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worker)


def _sample_1024_png(tmp_path: pathlib.Path) -> bytes:
    out = tmp_path / "raw1024.png"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x2b4c7e:s=1024x1024:d=1",
            "-frames:v", "1", str(out),
        ],
        check=True,
    )
    return out.read_bytes()


def test_minted_contact_pools_are_disjoint_from_builtins_and_across_tiers():
    seen: set[str] = set()
    for tier, names in roster.MINTED_CONTACT_NAMES.items():
        assert len(names) >= 30
        for name in names:
            folded = name.casefold()
            assert folded not in roster.CONTACT_TIERS, f"{name} collides with CONTACT_TIERS"
            assert folded not in seen, f"{name} duplicated across tiers"
            seen.add(folded)
            assert roster.tier_for_contact(name) == tier
            assert roster.tier_for_contact(f"{name} B") == tier


def test_mint_contact_is_available_immediately_and_avatar_arrives_in_background(
    tmp_path, monkeypatch
):
    raw_png = _sample_1024_png(tmp_path)
    monkeypatch.setattr(
        config, "load",
        lambda: SimpleNamespace(
            openai_key=lambda: "sk-test",
            cartesia_voices={},
            roster=config.DEFAULT_ROSTER,
            cartesia_voice_for=lambda name: config.DEFAULT_CARTESIA_VOICES.get(name, ""),
            gemini_voice_for=lambda _name: "",
        ),
    )
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager, "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )

    # 1. Mint Contact and start session immediately: no avatar file exists yet,
    #    and session creation does not wait on image generation.
    row = contact_minting.mint_contact("agy")
    assert row["name"] == "Cosmo"
    assert row["avatar_path"] == ""
    assert len(launched) == 1

    agent_id = agents.create_agent(
        persona=row["name"], voice_id=row["voice_id"],
        cwd=str(tmp_path), session="cosmo-1234", backend="agy",
    )
    assert agents.get_by_agent_id(agent_id)["avatar_path"] == ""

    # 2. Run the detached worker to completion with a stubbed OpenAI image response;
    #    verify real ffmpeg downsamples to <=512KB JPEG, updates both persona and
    #    active agent rows, and BackgroundJobWatcher broadcasts persona-updated SSE.
    cmd = launched[0]
    handle = cmd[cmd.index("--handle") + 1]
    persona_id = cmd[cmd.index("--persona-id") + 1]
    tier = cmd[cmd.index("--tier") + 1]

    patch_shared(monkeypatch, background_jobs, "process_start_token", lambda _pid: "boot:worker")
    monkeypatch.setattr(
        persona_avatar_generation, "_request_generation",
        lambda **_kwargs: raw_png,
    )

    broadcasts: list[dict] = []
    stream = SimpleNamespace(broadcast=broadcasts.append)
    watcher = background_job_watcher.BackgroundJobWatcher(stream)

    exit_code = worker.run(
        handle=handle, persona_id=persona_id, tier=tier, stop=threading.Event()
    )
    assert exit_code == 0

    watcher._poll_once()

    updated_persona = personas.get("Cosmo")
    assert updated_persona is not None
    avatar_file = pathlib.Path(updated_persona["avatar_path"])
    assert avatar_file.is_file()
    jpeg_bytes = avatar_file.read_bytes()
    assert jpeg_bytes.startswith(b"\xff\xd8\xff")
    assert len(jpeg_bytes) <= persona_avatar_generation.AVATAR_MAX_BYTES

    updated_agent = agents.get_by_agent_id(agent_id)
    assert updated_agent["avatar_path"] == str(avatar_file)
    assert personas.public(updated_persona)["avatar_url"].startswith(
        f"/persona-avatars/{persona_id}?v="
    )
    roster_events = [
        b for b in broadcasts
        if b.get("type") == "agent-roster" and b.get("kind") == "persona-updated"
    ]
    assert len(roster_events) == 1


def test_background_avatar_never_overwrites_manual_user_avatar(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "load",
        lambda: SimpleNamespace(
            openai_key=lambda: "sk-test",
            cartesia_voices={},
            roster=config.DEFAULT_ROSTER,
            cartesia_voice_for=lambda name: config.DEFAULT_CARTESIA_VOICES.get(name, ""),
            gemini_voice_for=lambda _name: "",
        ),
    )
    row = personas.create(name="Clara", avatar_symbol="sparkles")
    result = persona_avatar_generation.generate_for_persona(
        row["persona_id"],
        tier="claude",
        request=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not call OpenAI")),
    )
    assert result["avatar_symbol"] == "sparkles"
    assert result["avatar_path"] == ""


def _stub_cfg(
    monkeypatch,
    *,
    enabled: bool = True,
    daily_limit: int = 20,
    max_attempts: int = 3,
):
    monkeypatch.setattr(
        config,
        "load",
        lambda: SimpleNamespace(
            openai_key=lambda: "sk-test",
            contact_avatar_generation=enabled,
            contact_avatar_daily_limit=daily_limit,
            contact_avatar_max_attempts=max_attempts,
            cartesia_voices={},
            roster=config.DEFAULT_ROSTER,
            cartesia_voice_for=lambda name: config.DEFAULT_CARTESIA_VOICES.get(name, ""),
            gemini_voice_for=lambda _name: "",
            read_global_mcp_servers=lambda: {},
        ),
    )


def test_builtin_and_existing_avatar_contacts_never_enqueue_or_generate(
    tmp_path, monkeypatch
):
    _stub_cfg(monkeypatch)
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager,
        "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )
    personas.ensure_builtins()
    mike = personas.get("Mike")
    assert mike is not None and mike["builtin"] == 1
    assert mike["avatar_path"] == "" and mike["avatar_symbol"] == ""

    assert persona_avatar_generation.start_best_effort("Mike") is None
    unchanged_mike = persona_avatar_generation.generate_for_persona(
        mike["persona_id"],
        request=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("builtin paid call")),
    )
    assert unchanged_mike["avatar_path"] == ""

    with_symbol = personas.create(name="Symbol Custom", avatar_symbol="bolt")
    assert persona_avatar_generation.start_best_effort(with_symbol["name"]) is None

    raw_png = _sample_1024_png(tmp_path)
    import base64
    with_photo = personas.create(
        name="Photo Custom",
        avatar_base64=base64.b64encode(
            persona_avatar_generation._normalize_avatar_jpeg(raw_png)
        ).decode("ascii"),
    )
    assert with_photo["avatar_path"] != ""
    assert persona_avatar_generation.start_best_effort(with_photo["name"]) is None
    assert launched == []


def test_active_and_succeeded_jobs_coalesce_without_relaunching(monkeypatch):
    _stub_cfg(monkeypatch)
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager,
        "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )
    row = personas.create(name="Coalesce Clara")

    first = persona_avatar_generation.start_best_effort(row["name"], now_ms=10_000)
    assert first is not None and first["status"] == "queued"
    assert len(launched) == 1

    # Duplicate call while queued returns the active job without spawning a second unit.
    second = persona_avatar_generation.start_best_effort(row["name"], now_ms=10_500)
    assert second is not None and second["job_id"] == first["job_id"]
    assert len(launched) == 1

    # Duplicate call while running also coalesces.
    patch_shared(monkeypatch, background_jobs, "process_start_token", lambda _pid: "boot:1")
    patch_shared(monkeypatch, background_jobs, "worker_is_alive", lambda _pid, _tok: True)
    background_jobs.start(
        first["job_id"], generation=1, worker_pid=4242, worker_start_token="boot:1"
    )
    third = persona_avatar_generation.start_best_effort(row["name"], now_ms=11_000)
    assert third is not None and third["status"] == "running"
    assert len(launched) == 1

    # Once succeeded, start_best_effort does not re-run even if avatar_path is still empty.
    background_jobs.finish(
        first["job_id"],
        generation=1,
        status="succeeded",
        worker_pid=4242,
        worker_start_token="boot:1",
    )
    assert persona_avatar_generation.start_best_effort(row["name"], now_ms=99_000_000) is None
    assert len(launched) == 1


def test_lifecycle_skips_janitor_anonymous_and_builtin_and_releases_lock_and_api_key(
    tmp_path, monkeypatch
):
    _stub_cfg(monkeypatch)
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret-must-not-leak")
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "inherited.db"))

    launches: list[tuple[list[str], dict, bool]] = []

    def spy_launch(command, **kwargs):
        lock_free_on_other_thread = False

        def probe():
            nonlocal lock_free_on_other_thread
            acquired = AgentLifecycleService._create_lock.acquire(blocking=False)
            if acquired:
                lock_free_on_other_thread = True
                AgentLifecycleService._create_lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        launches.append((list(command), dict(kwargs.get("environment") or {}), lock_free_on_other_thread))
        return True, ""

    monkeypatch.setattr(service_manager, "launch_detached", spy_launch)

    personas.ensure_builtins()
    janitor_contact = personas.create(name="Janitor Custom")
    ctx = SimpleNamespace(
        agents_path=tmp_path / "agents.json",
        stream=SimpleNamespace(broadcast=lambda _ev: None),
        speak_announcement=lambda *_a, **_kw: None,
    )
    svc = AgentLifecycleService(ctx)

    # 1. Janitor creation never triggers avatar generation.
    svc.create_janitor({"name": janitor_contact["name"], "cwd": str(tmp_path), "backend": "claude"})
    assert launches == []

    # 2. Anonymous session creation never triggers avatar generation.
    anon = svc.create({"anonymous": True, "cwd": str(tmp_path), "backend": "codex"})
    assert launches == []

    # 3. Built-in Contact session creation never triggers avatar generation.
    svc.create({"name": "Mike", "session": "mike-live", "cwd": str(tmp_path), "backend": "claude"})
    assert launches == []

    # 4. Minted Contact session creation runs start_best_effort AFTER releasing _create_lock
    #    and never passes OPENAI_API_KEY onto the systemd-run environment.
    svc.create({"mint_contact": True, "cwd": str(tmp_path), "backend": "codex"})
    assert len(launches) == 1
    cmd_1, env_1, lock_free_1 = launches[0]
    assert lock_free_1 is True
    assert "OPENAI_API_KEY" not in env_1
    assert "sk-env-secret-must-not-leak" not in " ".join(cmd_1)
    assert env_1.get("CLAUDE_PWA_DB") == str(tmp_path / "inherited.db")

    # 5. Assigning a newly created Contact also releases _create_lock before launching.
    contact_assignment.assign_contact(anon.session, "create", "Assigned Fresh")
    assert len(launches) == 2
    assert launches[1][2] is True


def test_persona_route_honors_generate_avatar_false(monkeypatch):
    _stub_cfg(monkeypatch)
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager,
        "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )

    class _DummyHandler(PersonaRoutes):
        def __init__(self, payload: dict):
            self._payload = payload
            self.ctx = SimpleNamespace(stream=SimpleNamespace(broadcast=lambda _e: None))
            self.response: tuple[int, dict] | None = None

        def _read_json(self):
            return self._payload

        def _json(self, status: int, body: dict):
            self.response = (status, body)
            return self.response

        def _json_error(self, status: int, message: str):
            self.response = (status, {"error": message})
            return self.response

    opted_out = _DummyHandler({"name": "Opted Out Contact", "generate_avatar": False})
    opted_out._handle_create_persona()
    assert opted_out.response is not None and opted_out.response[0] == 201
    assert launched == []

    default_on = _DummyHandler({"name": "Default Avatar Contact"})
    default_on._handle_create_persona()
    assert default_on.response is not None and default_on.response[0] == 201
    assert len(launched) == 1


def test_mid_flight_user_avatar_is_not_overwritten_when_worker_finishes(
    tmp_path, monkeypatch
):
    _stub_cfg(monkeypatch)
    raw_png = _sample_1024_png(tmp_path)
    row = personas.create(name="Race Clara")

    def slow_provider(**_kwargs) -> bytes:
        # User manually chooses a symbol while OpenAI image generation is in flight.
        personas.update(
            original_name="Race Clara",
            name="Race Clara",
            voice_id=row["voice_id"],
            avatar_symbol="star.fill",
        )
        return raw_png

    result = persona_avatar_generation.generate_for_persona(
        row["persona_id"],
        tier="claude",
        request=slow_provider,
    )
    assert result["avatar_symbol"] == "star.fill"
    assert result["avatar_path"] == ""
    stored = personas.get("Race Clara")
    assert stored is not None
    assert stored["avatar_symbol"] == "star.fill"
    assert stored["avatar_path"] == ""


def test_spending_guards_off_switch_backoff_attempt_cap_and_daily_limit(
    tmp_path, monkeypatch
):
    # 1. Config file + env parsing for off-switch, daily limit, and attempt cap.
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[contacts]\n'
        'avatar_generation = false\n'
        'avatar_daily_limit = 2\n'
        'avatar_max_attempts = 3\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(cfg_file))
    monkeypatch.delenv("CLARP_CONTACT_AVATAR_GENERATION", raising=False)
    config.reset_cache()
    parsed = config.load()
    assert parsed.contact_avatar_generation is False
    assert parsed.contact_avatar_daily_limit == 2
    assert parsed.contact_avatar_max_attempts == 3

    monkeypatch.setenv("CLARP_CONTACT_AVATAR_GENERATION", "1")
    config.reset_cache()
    assert config.load().contact_avatar_generation is True
    monkeypatch.setenv("CLARP_CONTACT_AVATAR_GENERATION", "0")
    config.reset_cache()
    assert config.load().contact_avatar_generation is False
    monkeypatch.delenv("CLARP_CONTACT_AVATAR_GENERATION", raising=False)
    config.reset_cache()

    # 2. Off-switch blocks both enqueueing and in-flight worker execution before provider call.
    _stub_cfg(monkeypatch, enabled=False)
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager,
        "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )
    disabled_contact = personas.create(name="Disabled Contact")
    assert persona_avatar_generation.start_best_effort(disabled_contact["name"]) is None
    assert launched == []
    with pytest.raises(persona_avatar_generation.PersonaAvatarCancelled):
        persona_avatar_generation.generate_for_persona(
            disabled_contact["persona_id"],
            request=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("paid call while disabled")),
        )

    # 3. Per-Contact retry backoff (5m, then 30m) and max_attempts=3 cap.
    _stub_cfg(monkeypatch, enabled=True, daily_limit=20, max_attempts=3)
    flaky = personas.create(name="Flaky Contact")
    t0 = 1_800_000_000_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t0)

    job1 = persona_avatar_generation.start_best_effort(flaky["name"], now_ms=t0)
    assert job1 is not None and job1["generation"] == 1
    assert len(launched) == 1
    monkeypatch.setattr("lib.db.now_ms", lambda: t0 + 1_000)
    background_jobs.finish(job1["job_id"], generation=1, status="failed", reason="http 500")

    # Before 5-minute backoff expires -> refused.
    assert persona_avatar_generation.start_best_effort(
        flaky["name"], now_ms=t0 + 1_000 + 5 * 60 * 1000 - 1
    ) is None
    assert len(launched) == 1

    # At 5-minute backoff -> attempt 2 launches.
    t2 = t0 + 1_000 + 5 * 60 * 1000
    monkeypatch.setattr("lib.db.now_ms", lambda: t2)
    job2 = persona_avatar_generation.start_best_effort(flaky["name"], now_ms=t2)
    assert job2 is not None and job2["generation"] == 2
    assert len(launched) == 2
    monkeypatch.setattr("lib.db.now_ms", lambda: t2 + 1_000)
    background_jobs.finish(job2["job_id"], generation=2, status="failed", reason="http 500")

    # Before 30-minute second backoff expires -> refused.
    assert persona_avatar_generation.start_best_effort(
        flaky["name"], now_ms=t2 + 1_000 + 30 * 60 * 1000 - 1
    ) is None
    assert len(launched) == 2

    # At 30-minute backoff -> attempt 3 launches.
    t3 = t2 + 1_000 + 30 * 60 * 1000
    monkeypatch.setattr("lib.db.now_ms", lambda: t3)
    job3 = persona_avatar_generation.start_best_effort(flaky["name"], now_ms=t3)
    assert job3 is not None and job3["generation"] == 3
    assert len(launched) == 3
    monkeypatch.setattr("lib.db.now_ms", lambda: t3 + 1_000)
    background_jobs.finish(job3["job_id"], generation=3, status="failed", reason="http 500")

    # After 3 failed attempts -> permanently capped even hours later.
    assert persona_avatar_generation.start_best_effort(
        flaky["name"], now_ms=t3 + 6 * 60 * 60 * 1000
    ) is None
    assert len(launched) == 3

    # 4. Daily rolling limit caps launches across distinct Contacts until 24h rolls over.
    t_day = t3 + 48 * 60 * 60 * 1000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_day)
    _stub_cfg(monkeypatch, enabled=True, daily_limit=2, max_attempts=3)
    c1 = personas.create(name="Cap One")
    c2 = personas.create(name="Cap Two")
    c3 = personas.create(name="Cap Three")

    assert persona_avatar_generation.start_best_effort(c1["name"], now_ms=t_day) is not None
    assert persona_avatar_generation.start_best_effort(c2["name"], now_ms=t_day + 100) is not None
    assert persona_avatar_generation.start_best_effort(c3["name"], now_ms=t_day + 200) is None
    assert len(launched) == 5

    # Once the 24h window rolls past t_day, c3 is eligible to launch.
    t_next_day = t_day + persona_avatar_generation.DAILY_WINDOW_MS + 1_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_next_day)
    assert persona_avatar_generation.start_best_effort(c3["name"], now_ms=t_next_day) is not None
    assert len(launched) == 6


def test_background_job_watcher_caps_status_cache_without_dropping_active_jobs():
    watcher = background_job_watcher.BackgroundJobWatcher(
        SimpleNamespace(broadcast=lambda _e: None)
    )
    watcher._statuses["active-keep"] = (1, "running")
    for idx in range(1_024):
        watcher._statuses[f"done-{idx}"] = (1, "succeeded")

    job = background_jobs.upsert_computer(
        computer_id="srv-1",
        job_id="overflow-trigger",
        kind="persona-avatar",
        title="Trigger prune",
        status="queued",
    )
    background_jobs.finish(job["job_id"], generation=1, status="succeeded")
    watcher._poll_once()

    assert len(watcher._statuses) <= 1_024
    assert watcher._statuses["active-keep"] == (1, "running")
    assert watcher._statuses["overflow-trigger"] == (1, "succeeded")
