from lib.avatar_urls import (
    avatar_content_version,
    notification_avatar_authorized,
    notification_avatar_signature,
    versioned_avatar_url,
)


def test_avatar_url_is_content_versioned_and_changes_on_replacement(tmp_path):
    avatar = tmp_path / "portrait.jpg"
    avatar.write_bytes(b"first portrait")

    first = versioned_avatar_url("/avatars", "agent one", str(avatar))
    same = versioned_avatar_url("/avatars", "agent one", str(avatar))
    assert first == same
    assert first.startswith("/avatars/agent%20one?v=")

    avatar.write_bytes(b"replacement portrait")
    replacement = versioned_avatar_url("/avatars", "agent one", str(avatar))
    assert replacement != first


def test_avatar_url_handles_empty_and_missing_paths(tmp_path):
    assert versioned_avatar_url("/avatars", "agent", "") == ""
    missing = versioned_avatar_url("/avatars", "agent", str(tmp_path / "missing.jpg"))
    assert missing == "/avatars/agent?v=missing"


def test_notification_avatar_signature_is_scoped_and_short_lived():
    signature = notification_avatar_signature(
        "secret", "agent-a", "content-v1", 1_500)
    assert notification_avatar_authorized(
        secret="secret", agent_id="agent-a", content_version="content-v1",
        expires_at=1_500, signature=signature, now=1_000)
    assert not notification_avatar_authorized(
        secret="secret", agent_id="agent-b", content_version="content-v1",
        expires_at=1_500, signature=signature, now=1_000)
    assert not notification_avatar_authorized(
        secret="secret", agent_id="agent-a", content_version="content-v1",
        expires_at=1_500, signature=signature, now=1_501)
