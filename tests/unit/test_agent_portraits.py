from __future__ import annotations

import pathlib

import pytest

from lib import agent_portraits, agents, media_store


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _publish(tmp_path: pathlib.Path, session: str, name: str, blob: bytes) -> dict:
    return media_store.publish(
        session=session, blob=blob, source_name=name,
        content_type="image/png", caption=name, created_by="agent",
        media_dir=tmp_path / "media",
    )


def test_portraits_import_select_and_recover_missing_primary(tmp_path):
    agent_id = agents.create_agent(
        persona="Lena", voice_id="voice", cwd=str(tmp_path), session="lena")
    legacy = tmp_path / "legacy.png"
    legacy.write_bytes(PNG_1X1 + b"legacy")
    agents.update_agent(agent_id, avatar_path=str(legacy))

    initial = agent_portraits.list_for_session(
        "lena", portrait_dir=tmp_path / "media")
    assert initial["contract"] == "agent-portraits.v1"
    assert initial["max_portraits"] == 3
    assert len(initial["portraits"]) == 1
    assert initial["portraits"][0]["role"] == "primary"
    assert initial["portraits"][0]["source"] == "legacy_avatar"
    stored_legacy = agent_portraits.get_content(
        initial["portraits"][0]["portrait_id"])
    assert pathlib.Path(stored_legacy["storage_path"]) != legacy
    assert pathlib.Path(stored_legacy["storage_path"]).read_bytes() == legacy.read_bytes()
    assert initial["portraits"][0]["url"].endswith(
        "?v=" + initial["portraits"][0]["content_version"])

    first = _publish(tmp_path, "lena", "first.png", PNG_1X1)
    second = _publish(tmp_path, "lena", "second.png", PNG_1X1 + b"variant")
    with_first = agent_portraits.add_media_asset(
        session="lena", asset_id=first["asset_id"], portrait_dir=tmp_path / "media")
    with_both = agent_portraits.add_media_asset(
        session="lena", asset_id=second["asset_id"], portrait_dir=tmp_path / "media")
    assert len(with_first["portraits"]) == 2
    assert len(with_both["portraits"]) == 3

    selected_id = next(
        row["portrait_id"] for row in with_both["portraits"]
        if row["media_asset_id"] == first["asset_id"])
    selected = agent_portraits.select_primary(
        session="lena", portrait_id=selected_id, portrait_dir=tmp_path / "media")
    assert selected["primary_portrait_id"] == selected_id
    assert agents.get_by_agent_id(agent_id)["avatar_path"] == str(
        media_store.get(first["asset_id"])["storage_path"])

    pathlib.Path(agents.get_by_agent_id(agent_id)["avatar_path"]).unlink()
    missing = agent_portraits.list_for_session(
        "lena", portrait_dir=tmp_path / "media")
    assert next(
        row for row in missing["portraits"] if row["portrait_id"] == selected_id
    )["available"] is False
    recovery_id = next(
        row["portrait_id"] for row in missing["portraits"]
        if row["available"] and row["portrait_id"] != selected_id)
    recovered = agent_portraits.select_primary(
        session="lena", portrait_id=recovery_id, portrait_dir=tmp_path / "media")
    assert recovered["primary_portrait_id"] == recovery_id
    assert selected_id not in {row["portrait_id"] for row in recovered["portraits"]}

    # The legacy row is an immutable snapshot, not an alias to a mutable avatar.
    legacy.write_bytes(PNG_1X1 + b"replacement")
    agents.update_agent(agent_id, avatar_path=str(legacy))
    refreshed = agent_portraits.list_for_session(
        "lena", portrait_dir=tmp_path / "media")
    assert len(refreshed["portraits"]) <= 3
    assert refreshed["primary_portrait_id"] != recovery_id


def test_portrait_limit_and_agent_ownership_are_enforced(tmp_path):
    agents.create_agent(
        persona="Lena", voice_id="voice", cwd=str(tmp_path), session="lena")
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd=str(tmp_path), session="mike")
    assets = [
        _publish(tmp_path, "lena", f"v{index}.png", PNG_1X1 + bytes([index]))
        for index in range(4)
    ]
    agent_portraits.add_media_asset(
        session="lena", asset_id=assets[0]["asset_id"], portrait_dir=tmp_path / "media")
    agent_portraits.add_media_asset(
        session="lena", asset_id=assets[1]["asset_id"], portrait_dir=tmp_path / "media")
    agent_portraits.add_media_asset(
        session="lena", asset_id=assets[2]["asset_id"], portrait_dir=tmp_path / "media")
    with pytest.raises(agent_portraits.PortraitError) as limit:
        agent_portraits.add_media_asset(
            session="lena", asset_id=assets[3]["asset_id"],
            portrait_dir=tmp_path / "media")
    assert limit.value.status == 409

    mike = _publish(tmp_path, "mike", "mike.png", PNG_1X1 + b"mike")
    with pytest.raises(agent_portraits.PortraitError) as ownership:
        agent_portraits.add_media_asset(
            session="lena", asset_id=mike["asset_id"],
            portrait_dir=tmp_path / "media")
    assert ownership.value.status == 409


def test_oversized_legacy_avatar_is_not_reconciled(tmp_path):
    agent_id = agents.create_agent(
        persona="Lena", voice_id="voice", cwd=str(tmp_path), session="lena")
    oversized = tmp_path / "oversized.png"
    with oversized.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.truncate(agent_portraits.MAX_PORTRAIT_BYTES + 1)
    agents.update_agent(agent_id, avatar_path=str(oversized))

    result = agent_portraits.list_for_session(
        "lena", portrait_dir=tmp_path / "media")

    assert result["portraits"] == []
    assert not (tmp_path / "media" / "portrait-blobs").exists()
