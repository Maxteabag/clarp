from __future__ import annotations

import pathlib

import pytest

from lib import agent_portraits, agents, db, media_store


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
    assert initial["portraits"][0]["url"].endswith(
        "?v=" + initial["portraits"][0]["content_version"])

    first = _publish(tmp_path, "lena", "first.png", PNG_1X1)
    second = _publish(tmp_path, "lena", "second.png", PNG_1X1 + b"variant")
    with_first = agent_portraits.add_media_asset(
        session="lena", asset_id=first["asset_id"], portrait_dir=tmp_path / "media")
    # The first change records the avatar under the id the listing gave it,
    # as an immutable snapshot, not an alias to a mutable avatar.
    stored_legacy = agent_portraits.get_content(
        initial["portraits"][0]["portrait_id"])
    assert pathlib.Path(stored_legacy["storage_path"]) != legacy
    assert pathlib.Path(stored_legacy["storage_path"]).read_bytes() == legacy.read_bytes()
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


# --- GET /agent-portraits reads and changes nothing -------------------------------
# The portrait viewer asks each time it opens, and a limited paired device may
# ask too. Listing used to record the avatar as a portrait, copy it into the
# media directory, and could retire a portrait the user kept.

def _unchanged(tmp_path):
    """What listing must leave alone: the database and the media directory."""
    media = tmp_path / "media"
    rows = [tuple(row) for row in db.conn().execute(
        "SELECT * FROM agent_portraits ORDER BY portrait_id")]
    files = sorted(str(path) for path in media.rglob("*")) if media.exists() else None
    return db.conn().total_changes, rows, files


def _lena_with_an_avatar(tmp_path, blob=PNG_1X1 + b"legacy"):
    agent_id = agents.create_agent(
        persona="Lena", voice_id="voice", cwd=str(tmp_path), session="lena")
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(blob)
    agents.update_agent(agent_id, avatar_path=str(avatar))
    return agent_id, avatar


def test_listing_an_avatar_that_is_no_portrait_yet_writes_nothing(tmp_path):
    _agent_id, avatar = _lena_with_an_avatar(tmp_path)
    before = _unchanged(tmp_path)

    listed = agent_portraits.list_for_session("lena", portrait_dir=tmp_path / "media")
    again = agent_portraits.list_for_session("lena", portrait_dir=tmp_path / "media")

    assert _unchanged(tmp_path) == before
    assert not (tmp_path / "media").exists()
    [portrait] = listed["portraits"]
    assert again == listed                                    # the same id each time
    assert listed["primary_portrait_id"] == portrait["portrait_id"]
    assert (portrait["role"], portrait["source"], portrait["available"]) == (
        "primary", "legacy_avatar", True)
    # Its picture is served from the avatar itself, also without a write.
    content = agent_portraits.get_content(portrait["portrait_id"])
    assert pathlib.Path(content["storage_path"]) == avatar
    assert _unchanged(tmp_path) == before


def test_the_id_a_listing_gave_selects_that_portrait(tmp_path):
    agent_id, avatar = _lena_with_an_avatar(tmp_path)
    listed = agent_portraits.list_for_session("lena", portrait_dir=tmp_path / "media")
    portrait_id = listed["primary_portrait_id"]

    selected = agent_portraits.select_primary(
        session="lena", portrait_id=portrait_id, portrait_dir=tmp_path / "media")

    assert selected["primary_portrait_id"] == portrait_id
    stored = pathlib.Path(agent_portraits.get_content(portrait_id)["storage_path"])
    assert stored != avatar and stored.read_bytes() == avatar.read_bytes()
    assert agent_portraits.list_for_session(
        "lena", portrait_dir=tmp_path / "media")["portraits"] == selected["portraits"]


def test_a_listed_id_is_dead_once_the_avatar_is_another_picture(tmp_path):
    agent_id, avatar = _lena_with_an_avatar(tmp_path)
    listed = agent_portraits.list_for_session("lena", portrait_dir=tmp_path / "media")
    avatar.write_bytes(PNG_1X1 + b"another")

    assert agent_portraits.get_content(listed["primary_portrait_id"]) is None
    assert agent_portraits.get_content("portrait_legacy_nobody_0123456789abcdef01234567") is None


def test_listing_shows_what_the_next_change_will_record_and_retires_nothing(tmp_path):
    """Three portraits are kept, then the avatar becomes a fourth picture by
    another path. The listing shows the avatar as primary and two of the
    rest, exactly as the next change will leave them, and deletes no row."""
    agent_id, avatar = _lena_with_an_avatar(tmp_path)
    media = tmp_path / "media"
    assets = [_publish(tmp_path, "lena", f"v{index}.png", PNG_1X1 + bytes([index]))
              for index in range(2)]
    for asset in assets:
        kept = agent_portraits.add_media_asset(
            session="lena", asset_id=asset["asset_id"], portrait_dir=media)
    oldest, middle, newest = (
        kept["primary_portrait_id"],
        *(next(row["portrait_id"] for row in kept["portraits"]
               if row["media_asset_id"] == asset["asset_id"]) for asset in assets))
    for age, portrait_id in enumerate((oldest, middle, newest), start=1):
        db.conn().execute("UPDATE agent_portraits SET created_at=? WHERE portrait_id=?",
                          (age, portrait_id))
    avatar.write_bytes(PNG_1X1 + b"changed elsewhere")
    before = _unchanged(tmp_path)

    listed = agent_portraits.list_for_session("lena", portrait_dir=media)

    assert _unchanged(tmp_path) == before
    ids = [row["portrait_id"] for row in listed["portraits"]]
    # The portrait that was primary stays as an alternate; of the two that
    # were alternates the older one is what the change retires.
    assert ids[1:] == [oldest, newest] and ids[0] not in (oldest, middle, newest)
    assert listed["primary_portrait_id"] == ids[0]
    assert [row["role"] for row in listed["portraits"]] == ["primary", "alternate", "alternate"]
    # The next change records exactly that.
    selected = agent_portraits.select_primary(
        session="lena", portrait_id=ids[0], portrait_dir=media)
    assert [row["portrait_id"] for row in selected["portraits"]] == ids


def test_listing_leaves_out_an_alternate_whose_file_is_gone_without_deleting_it(tmp_path):
    agent_id, avatar = _lena_with_an_avatar(tmp_path)
    media = tmp_path / "media"
    asset = _publish(tmp_path, "lena", "v.png", PNG_1X1 + b"v")
    kept = agent_portraits.add_media_asset(
        session="lena", asset_id=asset["asset_id"], portrait_dir=media)
    assert len(kept["portraits"]) == 2
    pathlib.Path(media_store.get(asset["asset_id"])["storage_path"]).unlink()
    before = _unchanged(tmp_path)

    listed = agent_portraits.list_for_session("lena", portrait_dir=media)

    assert _unchanged(tmp_path) == before
    assert [row["role"] for row in listed["portraits"]] == ["primary"]


def test_listing_shows_an_avatar_that_is_a_kept_alternate_as_primary_without_writing(tmp_path):
    agent_id, avatar = _lena_with_an_avatar(tmp_path)
    media = tmp_path / "media"
    asset = _publish(tmp_path, "lena", "v.png", PNG_1X1 + b"v")
    kept = agent_portraits.add_media_asset(
        session="lena", asset_id=asset["asset_id"], portrait_dir=media)
    alternate = next(row["portrait_id"] for row in kept["portraits"]
                     if row["media_asset_id"] == asset["asset_id"])
    assert kept["primary_portrait_id"] != alternate
    # Another path makes that picture the avatar.
    agents.update_agent(agent_id, avatar_path=str(media_store.get(asset["asset_id"])["storage_path"]))
    before = _unchanged(tmp_path)

    listed = agent_portraits.list_for_session("lena", portrait_dir=media)

    assert _unchanged(tmp_path) == before
    assert listed["primary_portrait_id"] == alternate
    assert [(row["portrait_id"] == alternate, row["role"]) for row in listed["portraits"]] == [
        (True, "primary"), (False, "alternate")]


def test_a_retired_avatar_that_comes_back_is_recorded_again_under_its_id(tmp_path):
    agent_id, avatar = _lena_with_an_avatar(tmp_path)
    media = tmp_path / "media"
    original = avatar.read_bytes()

    def record_the_avatar(age):
        agents.update_agent(agent_id, avatar_path=str(avatar))
        shown = agent_portraits.list_for_session("lena", portrait_dir=media)["primary_portrait_id"]
        agent_portraits.select_primary(session="lena", portrait_id=shown, portrait_dir=media)
        db.conn().execute("UPDATE agent_portraits SET created_at=? WHERE portrait_id=?",
                          (age, shown))
        return shown

    first = record_the_avatar(1)
    for index in range(3):                 # three more avatars: the first is retired
        avatar.write_bytes(PNG_1X1 + bytes([index]))
        record_the_avatar(index + 2)
    assert agent_portraits.get_content(first) is None

    avatar.write_bytes(original)
    assert record_the_avatar(9) == first
    stored = pathlib.Path(agent_portraits.get_content(first)["storage_path"])
    assert stored != avatar and stored.read_bytes() == original
    listed = agent_portraits.list_for_session("lena", portrait_dir=media)
    assert listed["primary_portrait_id"] == first and len(listed["portraits"]) == 3


def test_listing_an_unknown_session_is_not_found(tmp_path):
    with pytest.raises(agent_portraits.PortraitError) as unknown:
        agent_portraits.list_for_session("nobody", portrait_dir=tmp_path / "media")
    assert unknown.value.status == 404
