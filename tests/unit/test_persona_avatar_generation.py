from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import threading
from types import SimpleNamespace

from lib import (
    agents,
    background_job_watcher,
    background_jobs,
    config,
    contact_minting,
    events,
    persona_avatar_generation,
    personas,
    roster,
    service_manager,
    snapshot,
)
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
