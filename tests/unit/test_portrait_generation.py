from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from lib import (agent_portraits, agents, background_jobs, config,
                 media_store, portrait_generation, service_manager)


@pytest.fixture(autouse=True)
def _capture_platform_worker_launch(monkeypatch):
    def launch(command, **_kwargs):
        result = portrait_generation.subprocess.run(
            command, text=True, capture_output=True, check=False)
        return result.returncode == 0, result.stderr or ""

    monkeypatch.setattr(service_manager, "launch_detached", launch)


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _agent(tmp_path):
    avatar = tmp_path / "primary.png"
    avatar.write_bytes(PNG + b"primary")
    agent_id = agents.create_agent(
        persona="Bella", voice_id="voice", cwd=str(tmp_path), session="bella")
    agents.update_agent(agent_id, avatar_path=str(avatar))
    return agent_id


def _key(monkeypatch, value="secret"):
    monkeypatch.setattr(
        config, "load", lambda: SimpleNamespace(openai_key=lambda: value))


def test_capability_is_truthful_about_key_and_primary(tmp_path, monkeypatch):
    _agent(tmp_path)
    _key(monkeypatch, "")
    unavailable = portrait_generation.capability(
        "bella", media_dir=tmp_path / "media")
    assert unavailable["available"] is False
    assert "API key" in unavailable["reason"]

    _key(monkeypatch)
    available = portrait_generation.capability(
        "bella", media_dir=tmp_path / "media")
    assert available["available"] is True
    assert available["model"] == "gpt-image-2"
    assert available["count"] == 2
    assert "charges" in available["cost_notice"]


def test_generate_two_replaces_alternates_and_preserves_primary(
    tmp_path, monkeypatch,
):
    agent_id = _agent(tmp_path)
    _key(monkeypatch)
    media_dir = tmp_path / "media"
    collection = agent_portraits.list_for_session("bella", portrait_dir=media_dir)
    primary_id = collection["primary_portrait_id"]
    old = media_store.publish(
        session="bella", blob=PNG + b"old", source_name="old.png",
        content_type="image/png", media_dir=media_dir)
    agent_portraits.add_media_asset(
        session="bella", asset_id=old["asset_id"], portrait_dir=media_dir)
    job = background_jobs.upsert_computer(
        computer_id="computer-a",
        job_id=portrait_generation.job_id_for(agent_id),
        kind="portrait-generation", title="Generate", status="queued")
    seen = []

    def edit(**kwargs):
        seen.append(kwargs["prompt"])
        return PNG + f"generated-{len(seen)}".encode()

    result = portrait_generation.generate_two(
        "bella", handle=background_jobs.job_handle(job), media_dir=media_dir,
        request=edit)

    assert len(seen) == 2
    assert result["primary_portrait_id"] == primary_id
    assert len(result["portraits"]) == 3
    alternates = [row for row in result["portraits"] if row["role"] == "alternate"]
    assert len(alternates) == 2
    assert all(row["created_by"] == "portrait_generation:openai"
               for row in alternates)
    assert old["asset_id"] not in {row["media_asset_id"] for row in alternates}


def test_cancelled_generation_never_publishes_media(tmp_path, monkeypatch):
    _agent(tmp_path)
    _key(monkeypatch)
    media_dir = tmp_path / "media"
    agent_portraits.list_for_session("bella", portrait_dir=media_dir)

    with pytest.raises(portrait_generation.GenerationCancelled):
        portrait_generation.generate_two(
            "bella", handle="portrait-generation-dead:bg1:1",
            media_dir=media_dir, should_continue=lambda: False,
            request=lambda **_kwargs: PNG)

    assert media_store.list_for_session("bella") == []


def test_cancellation_winning_at_commit_preserves_existing_gallery(
    tmp_path, monkeypatch,
):
    agent_id = _agent(tmp_path)
    _key(monkeypatch)
    media_dir = tmp_path / "media"
    before = agent_portraits.list_for_session("bella", portrait_dir=media_dir)
    old = media_store.publish(
        session="bella", blob=PNG + b"old", source_name="old.png",
        content_type="image/png", media_dir=media_dir)
    before = agent_portraits.add_media_asset(
        session="bella", asset_id=old["asset_id"], portrait_dir=media_dir)
    blobs_before = set((media_dir / "blobs").rglob("*"))
    job = background_jobs.upsert_computer(
        computer_id="computer-a",
        job_id=portrait_generation.job_id_for(agent_id),
        kind="portrait-generation", title="Generate", status="queued")
    real_replace = agent_portraits.replace_alternates_with_media_assets

    def cancel_before_commit(**kwargs):
        background_jobs.cancel(job["job_id"])
        return real_replace(**kwargs)

    monkeypatch.setattr(
        agent_portraits, "replace_alternates_with_media_assets",
        cancel_before_commit)
    with pytest.raises(agent_portraits.PortraitError, match="cancelled"):
        portrait_generation.generate_two(
            "bella", handle=background_jobs.job_handle(job), media_dir=media_dir,
            request=lambda **kwargs: PNG + kwargs["prompt"].encode())

    after = agent_portraits.list_for_session("bella", portrait_dir=media_dir)
    assert after["primary_portrait_id"] == before["primary_portrait_id"]
    assert old["asset_id"] in {row["media_asset_id"] for row in after["portraits"]}
    assert {path for path in (media_dir / "blobs").rglob("*") if path.is_file()} \
        == {path for path in blobs_before if path.is_file()}


def test_start_registers_computer_job_without_exposing_key(
    tmp_path, monkeypatch,
):
    _agent(tmp_path)
    _key(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("CLAUDE_PWA_DB", "/srv/clarp/state.sqlite")
    monkeypatch.setenv("CLARP_MEDIA_DIR", "/srv/clarp/media")
    media_dir = tmp_path / "media"
    agent_portraits.list_for_session("bella", portrait_dir=media_dir)
    monkeypatch.setattr(
        portrait_generation, "_worker_script",
        lambda: tmp_path / "portrait_generation_job.py")
    calls = []
    monkeypatch.setattr(
        portrait_generation.subprocess, "run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0, stderr=""))

    response = portrait_generation.start("bella", media_dir=media_dir)

    assert response["job"]["owner_kind"] == "computer"
    assert response["job"]["kind"] == "portrait-generation"
    assert response["job"]["metadata"]["expire_queued"] is True
    assert "environment-secret" not in " ".join(calls[0])
    assert "/srv/clarp/state.sqlite" not in " ".join(calls[0])
    assert calls[0][-4:-2] == ["--handle", background_jobs.job_handle(response["job"])]
    assert calls[0][-2:] == ["--session", "bella"]


def test_second_start_does_not_launch_shared_generation(
    tmp_path, monkeypatch,
):
    _agent(tmp_path)
    _key(monkeypatch)
    media_dir = tmp_path / "media"
    agent_portraits.list_for_session("bella", portrait_dir=media_dir)
    calls = []
    monkeypatch.setattr(
        portrait_generation, "_worker_script",
        lambda: tmp_path / "portrait_generation_job.py")
    monkeypatch.setattr(
        portrait_generation.subprocess, "run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0, stderr=""))

    first = portrait_generation.start("bella", media_dir=media_dir)
    second = portrait_generation.start("bella", media_dir=media_dir)

    assert first["job"]["generation"] == second["job"]["generation"]
    assert len(calls) == 1


def test_openai_edit_request_is_bounded_multipart(monkeypatch):
    captured = {}
    payload = json.dumps({
        "data": [{"b64_json": base64.b64encode(PNG).decode()}]
    }).encode()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, limit):
            assert limit == portrait_generation.MAX_RESPONSE_BYTES + 1
            return payload

    def open_request(request, **kwargs):
        captured["request"] = request
        captured["timeout"] = kwargs["timeout"]
        return Response()

    monkeypatch.setattr(
        portrait_generation.urllib.request, "urlopen", open_request)
    result = portrait_generation._request_edit(
        api_key="secret", source=PNG, source_mime="image/png", prompt="portrait")

    request = captured["request"]
    assert request.full_url == "https://api.openai.com/v1/images/edits"
    assert request.get_header("Authorization") == "Bearer secret"
    assert b'gpt-image-2' in request.data
    assert b'name="image[]"' in request.data
    assert result == PNG
