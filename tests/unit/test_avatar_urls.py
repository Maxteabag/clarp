import pathlib
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


def test_avatar_content_version_is_hashed_once_per_file_revision(tmp_path, monkeypatch):
    from lib import avatar_urls
    portrait = tmp_path / "p.png"
    portrait.write_bytes(b"one")
    first = avatar_urls.avatar_content_version(portrait)
    reads = []
    real = pathlib.Path.read_bytes

    def counting(self):
        reads.append(str(self))
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counting)
    assert avatar_urls.avatar_content_version(portrait) == first
    assert reads == [], "an unchanged file must not be re-read on every snapshot"
    portrait.write_bytes(b"two-is-longer")
    assert avatar_urls.avatar_content_version(portrait) != first


def test_static_persona_avatar_url_prefers_the_persona_then_the_backend_default(tmp_path):
    from lib.avatar_urls import static_persona_avatar_url
    root = tmp_path / "static" / "avatars"
    root.mkdir(parents=True)
    (root / "cipher.png").write_bytes(b"c")
    (root / "default-codex.png").write_bytes(b"d")
    assert static_persona_avatar_url("Cipher", "codex", static_root=tmp_path / "static") \
        .startswith("/static/avatars/cipher.png?v=")
    assert static_persona_avatar_url("Codex-8194", "codex", static_root=tmp_path / "static") \
        .startswith("/static/avatars/default-codex.png?v=")
    assert static_persona_avatar_url("Nobody", "grok", static_root=tmp_path / "static") == ""
    assert static_persona_avatar_url("Nobody", "grok", static_root=None) == ""
