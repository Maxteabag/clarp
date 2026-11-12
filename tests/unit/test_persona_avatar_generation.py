from __future__ import annotations

import base64
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
    _stub_cfg(monkeypatch)
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
    _stub_cfg(monkeypatch)
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
    t0 = 1_800_000_000_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t0)
    row = personas.create(name="Coalesce Clara")

    first = persona_avatar_generation.start_best_effort(row["name"], now_ms=t0)
    assert first is not None and first["status"] == "queued"
    assert len(launched) == 1
    assert background_jobs.count_launches_since("persona-avatar", since_ms=0) == 1

    # Duplicate call while queued returns the active job without spawning a second unit
    # or recording an extra queued launch event against the daily cap.
    monkeypatch.setattr("lib.db.now_ms", lambda: t0 + 500)
    second = persona_avatar_generation.start_best_effort(row["name"], now_ms=t0 + 500)
    assert second is not None and second["job_id"] == first["job_id"]
    assert second["revision"] == first["revision"]
    assert len(launched) == 1
    assert background_jobs.count_launches_since("persona-avatar", since_ms=0) == 1

    # Duplicate call while running also coalesces without adding queued launch events.
    patch_shared(monkeypatch, background_jobs, "process_start_token", lambda _pid: "boot:1")
    patch_shared(monkeypatch, background_jobs, "worker_is_alive", lambda _pid, _tok: True)
    monkeypatch.setattr("lib.db.now_ms", lambda: t0 + 1_000)
    background_jobs.start(
        first["job_id"], generation=1, worker_pid=4242, worker_start_token="boot:1"
    )
    third = persona_avatar_generation.start_best_effort(row["name"], now_ms=t0 + 1_500)
    assert third is not None and third["status"] == "running"
    assert len(launched) == 1
    assert background_jobs.count_launches_since("persona-avatar", since_ms=0) == 1

    # Once succeeded, start_best_effort does not re-run even hours past retry backoff
    # when avatar_path is still empty (e.g. user cleared/switched avatar mid-flight).
    t_done = t0 + 2_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_done)
    background_jobs.finish(
        first["job_id"],
        generation=1,
        status="succeeded",
        worker_pid=4242,
        worker_start_token="boot:1",
    )
    assert personas.get(row["name"])["avatar_path"] == ""
    t_hours_later = t_done + 2 * 60 * 60 * 1000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_hours_later)
    assert persona_avatar_generation.start_best_effort(row["name"], now_ms=t_hours_later) is None
    assert len(launched) == 1
    assert background_jobs.count_launches_since("persona-avatar", since_ms=0) == 1


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
    assert background_jobs.count_launches_since("persona-avatar", since_ms=0) == 0

    # 2. Anonymous session creation never triggers avatar generation even if name is supplied.
    anon = svc.create({
        "anonymous": True,
        "name": janitor_contact["name"],
        "cwd": str(tmp_path),
        "backend": "codex",
    })
    assert launches == []
    assert background_jobs.count_launches_since("persona-avatar", since_ms=0) == 0

    # 3. Built-in Contact, session-level avatar overrides, and reopen never trigger generation.
    svc.create({"name": "Mike", "session": "mike-live", "cwd": str(tmp_path), "backend": "claude"})
    assert launches == []

    inline_symbol = personas.create(name="Inline Symbol Contact")
    svc.create({
        "name": inline_symbol["name"],
        "avatar_symbol": "star.fill",
        "cwd": str(tmp_path),
        "backend": "claude",
    })
    assert launches == []

    import base64
    inline_photo = personas.create(name="Inline Photo Contact")
    tiny_jpeg = persona_avatar_generation._normalize_avatar_jpeg(_sample_1024_png(tmp_path))
    svc.create({
        "name": inline_photo["name"],
        "avatar_base64": base64.b64encode(tiny_jpeg).decode("ascii"),
        "cwd": str(tmp_path),
        "backend": "claude",
    })
    assert launches == []

    reopen_contact = personas.create(name="Reopen Contact")
    reopen_agent_id = agents.create_agent(
        persona=reopen_contact["name"],
        voice_id=reopen_contact["voice_id"],
        cwd=str(tmp_path),
        session="reopen-live",
        backend="claude",
    )
    agents.start_runtime(reopen_agent_id, "reopen-live")
    agents.bind_backend_session(reopen_agent_id, "sid-reopen-1")
    reopened = svc.create({
        "open_existing": True,
        "resume_session_id": "sid-reopen-1",
        "cwd": str(tmp_path),
        "backend": "claude",
    })
    assert reopened.session == "reopen-live"
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
    # 1. Config file + env parsing for defaults, off-switch words, unreadable values, and caps.
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(cfg_file))
    monkeypatch.delenv("CLARP_CONTACT_AVATAR_GENERATION", raising=False)
    config.reset_cache()
    defaults = config.load()
    assert defaults.contact_avatar_generation is config.DEFAULT_CONTACT_AVATAR_GENERATION
    assert defaults.contact_avatar_daily_limit == config.DEFAULT_CONTACT_AVATAR_DAILY_LIMIT
    assert defaults.contact_avatar_max_attempts == config.DEFAULT_CONTACT_AVATAR_MAX_ATTEMPTS

    for raw_toml in (
        "false", "0", '"false"', '"FALSE"', '"off"', '"OFF"', '"no"', '"0"',
        '""', '"maybe"', "2", '["on"]',
    ):
        cfg_file.write_text(
            f"[contacts]\navatar_generation = {raw_toml}\n"
            "avatar_daily_limit = 2\navatar_max_attempts = 3\n",
            encoding="utf-8",
        )
        config.reset_cache()
        parsed = config.load()
        assert parsed.contact_avatar_generation is False, f"[contacts] {raw_toml} stayed on"
        assert parsed.contact_avatar_daily_limit == 2
        assert parsed.contact_avatar_max_attempts == 3

        cfg_file.write_text(
            f"[openai]\ncontact_avatar_generation = {raw_toml}\n",
            encoding="utf-8",
        )
        config.reset_cache()
        assert config.load().contact_avatar_generation is False, f"[openai] {raw_toml} stayed on"

    for non_table_toml in (
        'contacts = "x"\n',
        "contacts = false\n",
        "contacts = 0\n",
        "contacts = []\n",
        'openai = "x"\n',
        "openai = false\n",
        "[contacts]\navatar_daily_limit = 5\n\n[server]\nport = 7682\n",
    ):
        if "[contacts]" in non_table_toml:
            non_table_toml = 'openai = "x"\n\n' + non_table_toml
        cfg_file.write_text(non_table_toml, encoding="utf-8")
        config.reset_cache()
        assert config.load().contact_avatar_generation is False, (
            f"non-table section stayed on:\n{non_table_toml}"
        )

    cfg_file.write_text('contacts = false\n', encoding="utf-8")
    monkeypatch.setenv("CLARP_CONTACT_AVATAR_GENERATION", "true")
    config.reset_cache()
    assert config.load().contact_avatar_generation is True
    monkeypatch.delenv("CLARP_CONTACT_AVATAR_GENERATION", raising=False)

    for raw_toml in ("true", "1", '"true"', '"on"', '"yes"', '"1"'):
        cfg_file.write_text(f"[contacts]\navatar_generation = {raw_toml}\n", encoding="utf-8")
        config.reset_cache()
        assert config.load().contact_avatar_generation is True, f"[contacts] {raw_toml} was off"

    cfg_file.write_text("[contacts]\navatar_generation = false\n", encoding="utf-8")
    for env_true in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("CLARP_CONTACT_AVATAR_GENERATION", env_true)
        config.reset_cache()
        assert config.load().contact_avatar_generation is True
    cfg_file.write_text("[contacts]\navatar_generation = true\n", encoding="utf-8")
    for env_false in ("0", "false", "no", "off", "unreadable"):
        monkeypatch.setenv("CLARP_CONTACT_AVATAR_GENERATION", env_false)
        config.reset_cache()
        assert config.load().contact_avatar_generation is False
    monkeypatch.delenv("CLARP_CONTACT_AVATAR_GENERATION", raising=False)
    config.reset_cache()

    # 2. Off-switch and zero limits block enqueueing and in-flight worker execution.
    _stub_cfg(monkeypatch, enabled=False)
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager,
        "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )
    disabled_contact = personas.create(name="Disabled Contact")
    assert persona_avatar_generation.start_best_effort(disabled_contact["name"]) is None
    _stub_cfg(monkeypatch, enabled=True, daily_limit=0, max_attempts=3)
    assert persona_avatar_generation.start_best_effort(disabled_contact["name"]) is None
    _stub_cfg(monkeypatch, enabled=True, daily_limit=20, max_attempts=0)
    assert persona_avatar_generation.start_best_effort(disabled_contact["name"]) is None
    assert launched == []
    _stub_cfg(monkeypatch, enabled=False)
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
    monkeypatch.setattr("lib.db.now_ms", lambda: t_day + 100)
    assert persona_avatar_generation.start_best_effort(c2["name"], now_ms=t_day + 100) is not None
    assert persona_avatar_generation.start_best_effort(c3["name"], now_ms=t_day + 200) is None
    assert len(launched) == 5

    # Still blocked just before the full 24-hour window elapses (e.g. 23h 59m 59s).
    t_almost_day = t_day + 24 * 60 * 60 * 1000 - 1_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_almost_day)
    assert persona_avatar_generation.start_best_effort(c3["name"], now_ms=t_almost_day) is None
    assert len(launched) == 5

    # Once the 24h window rolls past t_day, c3 is eligible to launch.
    t_next_day = t_day + 24 * 60 * 60 * 1000 + 1_000
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


def test_simultaneous_starts_never_overshoot_daily_limit_or_duplicate_retry(monkeypatch):
    _stub_cfg(monkeypatch, enabled=True, daily_limit=20, max_attempts=3)
    launched_lock = threading.Lock()
    launched: list[list[str]] = []

    def record_launch(command, **_kwargs):
        with launched_lock:
            launched.append(list(command))
        return True, ""

    monkeypatch.setattr(service_manager, "launch_detached", record_launch)
    t0 = 1_800_000_000_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t0)

    # Pre-launch 19 Contacts, leaving exactly 1 slot in the 20/day rolling window.
    for idx in range(19):
        pre = personas.create(name=f"Pre Cap {idx}")
        assert persona_avatar_generation.start_best_effort(pre["name"], now_ms=t0) is not None
    assert len(launched) == 19
    assert background_jobs.count_launches_since("persona-avatar", since_ms=t0 - 1_000) == 19

    # Release 16 distinct Contact starts at the exact same barrier instant.
    contenders = [personas.create(name=f"Burst Cap {idx}") for idx in range(16)]
    barrier = threading.Barrier(len(contenders))
    results: list[dict | None] = [None] * len(contenders)
    errors: list[BaseException] = []

    def burst_worker(slot: int, name: str) -> None:
        try:
            barrier.wait(timeout=5.0)
            results[slot] = persona_avatar_generation.start_best_effort(name, now_ms=t0 + 50)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=burst_worker, args=(idx, row["name"]))
        for idx, row in enumerate(contenders)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert errors == []
    admitted = [r for r in results if r is not None]
    assert len(admitted) == 1
    assert len(launched) == 20
    assert background_jobs.count_launches_since("persona-avatar", since_ms=t0 - 1_000) == 20

    # Advance past the 24h window and verify simultaneous retry callers on a failed
    # Contact admit at most 1 retry generation and 1 queued event.
    t_retry_base = t0 + 48 * 60 * 60 * 1000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_retry_base)
    retry_contact = personas.create(name="Concurrent Retry Contact")
    initial_job = persona_avatar_generation.start_best_effort(
        retry_contact["name"], now_ms=t_retry_base
    )
    assert initial_job is not None and initial_job["generation"] == 1
    monkeypatch.setattr("lib.db.now_ms", lambda: t_retry_base + 1_000)
    background_jobs.finish(
        initial_job["job_id"], generation=1, status="failed", reason="transient"
    )
    launches_before_retry = len(launched)
    t_after_backoff = t_retry_base + 1_000 + 5 * 60 * 1000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_after_backoff)

    retry_barrier = threading.Barrier(8)
    retry_results: list[dict | None] = [None] * 8

    def retry_worker(slot: int) -> None:
        try:
            retry_barrier.wait(timeout=5.0)
            retry_results[slot] = persona_avatar_generation.start_best_effort(
                retry_contact["name"], now_ms=t_after_backoff
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    retry_threads = [threading.Thread(target=retry_worker, args=(idx,)) for idx in range(8)]
    for thread in retry_threads:
        thread.start()
    for thread in retry_threads:
        thread.join(timeout=10.0)

    assert errors == []
    assert all(r is not None and r["generation"] == 2 for r in retry_results)
    assert len(launched) == launches_before_retry + 1
    assert background_jobs.count_launches_since(
        "persona-avatar", since_ms=t_retry_base
    ) == 2


def test_post_agents_returns_queued_avatar_state_without_waiting_on_slow_tts_and_transitions_cleanly(
    tmp_path, monkeypatch
):
    import time
    from lib.audio_stream import AudioStream
    from lib.context import ServerContext, StubSTT
    from lib.routes.agents import AgentRoutes

    raw_png = _sample_1024_png(tmp_path)
    _stub_cfg(monkeypatch, enabled=True, daily_limit=1, max_attempts=2)
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    launched: list[list[str]] = []
    monkeypatch.setattr(
        service_manager,
        "launch_detached",
        lambda command, **_kwargs: (launched.append(list(command)) or True, ""),
    )

    tts_entered = threading.Event()
    tts_release = threading.Event()
    tts_calls: list[tuple[str, str, str | None]] = []

    class BlockingTTSEngine:
        def synthesize(self, text: str, voice_id: str, session: str | None = None):
            tts_calls.append((text, voice_id, session))
            tts_entered.set()
            tts_release.wait(timeout=5.0)
            return None

    broadcasts: list[dict] = []
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    real_static = pathlib.Path(__file__).resolve().parents[2] / "static"
    ctx = ServerContext(
        root=tmp_path,
        static=real_static,
        audio_dir=audio_dir,
        agents_path=tmp_path / "agents.json",
        default_session="claude",
        tts=BlockingTTSEngine(),
        stream=AudioStream(audio_dir),
        stt=StubSTT(),
        roster_names=("Mike", "Rachel"),
    )
    monkeypatch.setattr(ctx.stream, "broadcast", broadcasts.append)

    class RouteHarness(AgentRoutes):
        def __init__(self, payload: dict | None = None, query: dict | None = None):
            self.ctx = ctx
            self._payload = payload or {}
            self._qs = query or {}
            self.responses: list[tuple[int, dict]] = []

        def _query(self):
            return self._qs

        def _read_json(self):
            return dict(self._payload)

        def _bookkeeping_lineage_refusal(self, _parent: str):
            return None

        def _json(self, code: int, body: dict):
            self.responses.append((code, body))

        def _json_ok(self, body: dict):
            self.responses.append((200, body))

        def _json_error(self, code: int, message: str):
            self.responses.append((code, {"error": message}))

    # 1. Empty Contact pool + slow network TTS + real Host static/ portraits:
    #    occupy built-ins so auto_contact mints 'Clara', and verify POST /agents
    #    returns immediately with default-claude.png AND avatar_generation_status == 'queued'
    #    while TTS is still blocked.
    for row in list(contact_assignment.available_contacts("claude")):
        agents.create_agent(
            persona=row["name"],
            voice_id=row["voice_id"],
            cwd=str(tmp_path),
            session=f"occ-{row['name'].lower()}",
            backend="claude",
        )
    assert contact_assignment.available_contacts("claude") == []

    t_start = time.monotonic()
    h1 = RouteHarness({"auto_contact": True, "backend": "claude", "cwd": str(tmp_path), "synthesize_audio": True})
    h1._handle_create_agent()
    elapsed = time.monotonic() - t_start
    try:
        assert elapsed < 0.5, f"POST /agents blocked on TTS ({elapsed:.3f}s)"
        assert tts_entered.wait(timeout=2.0)
    finally:
        tts_release.set()

    assert len(h1.responses) == 1
    code, body = h1.responses[0]
    assert code == 200
    created_agent = body["agent"]
    assert created_agent["persona"] == "Clara"
    assert created_agent["avatar_url"].startswith("/static/avatars/default-claude.png?v=")
    assert created_agent["avatar_generation_status"] == "queued"
    assert len(tts_calls) == 1 and tts_calls[0][0] == "Clara is ready."
    assert any(b.get("type") == "agent-roster" and b.get("kind") == "created" for b in broadcasts)

    # Snapshot and Contact public rows also expose 'queued', then 'running'.
    snap_queued = snapshot.build_agent_snapshot(ctx, include_archived=False)
    snap_agent = next(a for a in snap_queued["agents"] if a["session"] == body["session"])
    snap_persona = next(p for p in snap_queued["personas"] if p["name"] == "Clara")
    assert snap_agent["avatar_url"].startswith("/static/avatars/default-claude.png?v=")
    assert snap_agent["avatar_generation_status"] == "queued"
    assert snap_persona["avatar_generation_status"] == "queued"

    cmd = launched[0]
    handle = cmd[cmd.index("--handle") + 1]
    persona_id = cmd[cmd.index("--persona-id") + 1]
    tier = cmd[cmd.index("--tier") + 1]
    job_id = persona_avatar_generation.job_id_for(persona_id)

    patch_shared(monkeypatch, background_jobs, "process_start_token", lambda _pid: "boot:av1")
    patch_shared(monkeypatch, background_jobs, "worker_is_alive", lambda _pid, _tok: True)
    background_jobs.start(job_id, generation=1, worker_pid=5151, worker_start_token="boot:av1")

    snap_running = snapshot.build_agent_snapshot(ctx, include_archived=False)
    assert next(a for a in snap_running["agents"] if a["session"] == body["session"])["avatar_generation_status"] == "running"
    assert personas.public(personas.get("Clara"))["avatar_generation_status"] == "running"

    # GET /agents/archived exposes the exact same running state on an archived agent,
    # then clears once generation finishes.
    agents.set_archived(created_agent["agent_id"], True)
    h_arch_running = RouteHarness()
    h_arch_running._handle_archived_agents()
    arch_code, arch_body = h_arch_running.responses[0]
    assert arch_code == 200 and arch_body["total"] == 1
    assert arch_body["agents"][0]["session"] == body["session"]
    assert arch_body["agents"][0]["avatar_url"].startswith("/static/avatars/default-claude.png?v=")
    assert arch_body["agents"][0]["avatar_generation_status"] == "running"
    agents.set_archived(created_agent["agent_id"], False)

    # 2. Second Contact hits daily_limit=1 -> default family portrait, avatar_generation_status is None.
    h2 = RouteHarness({"mint_contact": True, "backend": "claude", "cwd": str(tmp_path), "synthesize_audio": False})
    h2._handle_create_agent()
    capped_agent = h2.responses[0][1]["agent"]
    assert capped_agent["avatar_generation_status"] is None
    assert capped_agent["avatar_url"].startswith("/static/avatars/default-claude.png?v=")

    # 3. Worker failure emits persona-updated SSE and clears avatar_generation_status to None.
    watcher_events: list[dict] = []
    watcher = background_job_watcher.BackgroundJobWatcher(SimpleNamespace(broadcast=watcher_events.append))
    background_jobs.finish(job_id, generation=1, status="failed", reason="openai 500", worker_pid=5151, worker_start_token="boot:av1")
    watcher._poll_once()
    failed_roster_events = [
        e for e in watcher_events
        if e.get("type") == "agent-roster" and e.get("kind") == "persona-updated"
    ]
    assert len(failed_roster_events) == 1
    assert failed_roster_events[0].get("persona") == "Clara"
    snap_failed = snapshot.build_agent_snapshot(ctx, include_archived=False)
    assert next(a for a in snap_failed["agents"] if a["session"] == body["session"])["avatar_generation_status"] is None
    assert personas.public(personas.get("Clara"))["avatar_generation_status"] is None

    # 4. Retry succeeds -> avatar_url populated and avatar_generation_status stays None
    #    on both active snapshot and GET /agents/archived.
    _stub_cfg(monkeypatch, enabled=True, daily_limit=5, max_attempts=2)
    t_retry = background_jobs.get(job_id)["terminal_at"] + persona_avatar_generation.RETRY_BACKOFF_MS[0] + 1_000
    monkeypatch.setattr("lib.db.now_ms", lambda: t_retry)
    retried = persona_avatar_generation.start_best_effort("Clara", now_ms=t_retry)
    assert retried is not None and retried["status"] == "queued"
    watcher_events.clear()
    watcher._poll_once()
    # Retry re-entering queued from failed also nudges persona-updated so clients resume loading state.
    assert any(e.get("type") == "agent-roster" and e.get("kind") == "persona-updated" for e in watcher_events)

    retry_cmd = launched[-1]
    retry_handle = retry_cmd[retry_cmd.index("--handle") + 1]
    monkeypatch.setattr(persona_avatar_generation, "_request_generation", lambda **_kw: raw_png)
    assert worker.run(handle=retry_handle, persona_id=persona_id, tier=tier, stop=threading.Event()) == 0
    watcher_events.clear()
    watcher._poll_once()
    assert any(e.get("type") == "agent-roster" and e.get("kind") == "persona-updated" for e in watcher_events)
    snap_done = snapshot.build_agent_snapshot(ctx, include_archived=False)
    done_agent = next(a for a in snap_done["agents"] if a["session"] == body["session"])
    assert done_agent["avatar_generation_status"] is None
    assert done_agent["avatar_url"].startswith(f"/avatars/{done_agent['agent_id']}?v=")

    agents.set_archived(created_agent["agent_id"], True)
    h_arch_done = RouteHarness()
    h_arch_done._handle_archived_agents()
    arch_done = h_arch_done.responses[0][1]["agents"][0]
    assert arch_done["avatar_generation_status"] is None
    assert arch_done["avatar_url"].startswith(f"/avatars/{done_agent['agent_id']}?v=")


def test_avatar_generation_status_respects_family_defaults_and_clears_for_builtins_janitors_and_explicit_avatars(
    tmp_path, monkeypatch
):
    from lib.audio_stream import AudioStream
    from lib.context import ServerContext, StubSTT
    from lib.tts_engine import FakeTTSEngine

    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    real_static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    ctx = ServerContext(
        root=tmp_path,
        static=real_static,
        audio_dir=audio_dir,
        agents_path=tmp_path / "agents.json",
        default_session="claude",
        tts=FakeTTSEngine(audio_dir),
        stream=AudioStream(audio_dir),
        stt=StubSTT(),
        roster_names=("Mike",),
        async_announcements=False,
    )

    # 1. Built-in Contact never reports avatar_generation_status even if an active job row exists.
    mike = personas.get("Mike")
    assert mike is not None and mike["builtin"]
    background_jobs.upsert_computer(
        computer_id="srv-test",
        job_id=persona_avatar_generation.job_id_for(mike["persona_id"]),
        kind="persona-avatar",
        title="Unexpected builtin job",
        status="running",
        metadata={"persona_id": mike["persona_id"], "persona_name": "Mike"},
    )
    agents.create_agent(persona="Mike", voice_id=mike["voice_id"], cwd=str(tmp_path), session="mike-live", backend="claude")
    assert persona_avatar_generation.avatar_generation_status_for_persona(mike) is None
    assert personas.public(mike)["avatar_generation_status"] is None

    # 2. Custom Contact with an active 'running' job:
    #    - Claude, Codex, Grok, and Antigravity agents resolve Host default-<family>.png AND still report 'running'.
    #    - Agent with its own avatar_symbol or avatar_path reports None.
    #    - Janitor agent reports None.
    active_contact = personas.create(name="Zorina Custom")
    background_jobs.upsert_computer(
        computer_id="srv-test",
        job_id=persona_avatar_generation.job_id_for(active_contact["persona_id"]),
        kind="persona-avatar",
        title="Generating portrait for Zorina Custom",
        status="running",
        metadata={"persona_id": active_contact["persona_id"], "persona_name": "Zorina Custom"},
    )
    expected_defaults = {
        "claude": "/static/avatars/default-claude.png?v=",
        "codex": "/static/avatars/default-codex.png?v=",
        "grok": "/static/avatars/default-grok.png?v=",
        "agy": "/static/avatars/default-gemini.png?v=",
    }
    for backend_id in expected_defaults:
        agents.create_agent(
            persona="Zorina Custom", voice_id="v-zorina", cwd=str(tmp_path),
            session=f"zorina-{backend_id}", backend=backend_id,
        )
    custom_img = tmp_path / "own-face.jpg"
    custom_img.write_bytes(b"\xff\xd8\xff\xd9")
    sym_id = agents.create_agent(
        persona="Zorina Custom", voice_id="v-zorina", cwd=str(tmp_path),
        session="zorina-symbol", backend="claude",
    )
    agents.update_agent(sym_id, avatar_symbol="⚡")
    path_id = agents.create_agent(
        persona="Zorina Custom", voice_id="v-zorina", cwd=str(tmp_path),
        session="zorina-path", backend="claude",
    )
    agents.update_agent(path_id, avatar_path=str(custom_img))
    janitor_id = agents.create_agent(
        persona="Zorina Custom", voice_id="v-zorina", cwd=str(tmp_path),
        session="zorina-janitor", backend="claude",
    )
    agents.mark_janitor(agents.conn(), janitor_id)
    assert sym_id and path_id and janitor_id

    # 3. Contacts that already have their own avatar_symbol or avatar_path also report None.
    sym_contact = personas.create(name="Symbol Contact", avatar_symbol="🌿")
    background_jobs.upsert_computer(
        computer_id="srv-test",
        job_id=persona_avatar_generation.job_id_for(sym_contact["persona_id"]),
        kind="persona-avatar",
        title="Stale job for symbol contact",
        status="running",
        metadata={"persona_id": sym_contact["persona_id"], "persona_name": sym_contact["name"]},
    )
    agents.create_agent(
        persona=sym_contact["name"], voice_id="v-sym", cwd=str(tmp_path),
        session="sym-contact-agent", backend="claude",
    )
    path_contact = personas.create(
        name="Path Contact",
        avatar_base64=base64.b64encode(custom_img.read_bytes()).decode(),
    )
    background_jobs.upsert_computer(
        computer_id="srv-test",
        job_id=persona_avatar_generation.job_id_for(path_contact["persona_id"]),
        kind="persona-avatar",
        title="Stale job for path contact",
        status="running",
        metadata={"persona_id": path_contact["persona_id"], "persona_name": path_contact["name"]},
    )
    agents.create_agent(
        persona=path_contact["name"], voice_id="v-path", cwd=str(tmp_path),
        session="path-contact-agent", backend="claude",
    )
    assert persona_avatar_generation.avatar_generation_status_for_persona(sym_contact) is None
    assert persona_avatar_generation.avatar_generation_status_for_persona(path_contact) is None

    snap = snapshot.build_agent_snapshot(ctx, include_archived=False)
    by_session = {row["session"]: row for row in snap["agents"]}
    assert by_session["mike-live"]["avatar_generation_status"] is None
    for backend_id, expected_prefix in expected_defaults.items():
        row = by_session[f"zorina-{backend_id}"]
        assert row["avatar_url"].startswith(expected_prefix)
        assert row["avatar_generation_status"] == "running"
    assert by_session["zorina-symbol"]["avatar_generation_status"] is None
    assert by_session["zorina-path"]["avatar_generation_status"] is None
    assert by_session["zorina-janitor"]["avatar_generation_status"] is None
    assert by_session["sym-contact-agent"]["avatar_generation_status"] is None
    assert by_session["path-contact-agent"]["avatar_generation_status"] is None
    personas_by_name = {p["name"]: p for p in snap["personas"]}
    assert personas_by_name["Mike"]["avatar_generation_status"] is None
    assert personas_by_name["Zorina Custom"]["avatar_generation_status"] == "running"
    assert personas_by_name["Symbol Contact"]["avatar_generation_status"] is None
    assert personas_by_name["Path Contact"]["avatar_generation_status"] is None
