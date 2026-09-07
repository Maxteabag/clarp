"""Robot role portraits never overwrite an ordinary agent's identity."""
from pathlib import Path

from lib import avatar_urls


def test_all_janitor_roles_have_distinct_versioned_robot_portraits(tmp_path):
    assets = tmp_path / "janitor-avatars"
    assets.mkdir()
    files = {
        "task-labels": "rivet-task-label-keeper.jpg",
        "message-delegator": "delegator-message-dispatcher.jpg",
        "tool-explainer": "explainer-tool-guide.jpg",
        "future-kind": "janitor-default.jpg",
    }
    for name in files.values():
        (assets / name).write_bytes(name.encode())
    urls = [avatar_urls.janitor_avatar_url(True, role, static_root=tmp_path)
            for role in files]
    assert len(set(urls)) == 4
    for url, name in zip(urls, files.values()):
        assert url.startswith(f"/static/janitor-avatars/{name}?v=")


def test_releasing_sam_restores_his_normal_avatar_without_changing_the_file(tmp_path):
    portrait = tmp_path / "sam.jpg"
    portrait.write_bytes(b"original portrait")
    assert avatar_urls.janitor_avatar_url(False, "task-labels", static_root=tmp_path) == ""
    assert avatar_urls.versioned_avatar_url("/avatars", "sam", str(portrait)).startswith("/avatars/sam?v=")
    assert portrait.read_bytes() == b"original portrait"


def test_missing_role_asset_uses_generic_and_unknown_role_cannot_escape(tmp_path):
    assets = tmp_path / "janitor-avatars"
    assets.mkdir()
    (assets / "janitor-default.jpg").write_bytes(b"generic")
    for role in ("tool-explainer", "../../private", None):
        assert avatar_urls.janitor_avatar_url(True, role, static_root=tmp_path).startswith(
            "/static/janitor-avatars/janitor-default.jpg?v=")
