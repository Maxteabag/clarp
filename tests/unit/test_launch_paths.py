import pathlib

from lib.launch_paths import recover_user_path


def test_repairs_home_path_missing_username(tmp_path):
    home = tmp_path / "user"
    target = home / "GIT" / "claude-pwa"
    target.mkdir(parents=True)

    assert recover_user_path(
        "/home/GIT/claude-pwa", home=home
    ) == target


def test_preserves_valid_and_unknown_paths(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    valid = tmp_path / "shared"
    valid.mkdir()

    assert recover_user_path(str(valid), home=home) == valid
    assert recover_user_path("/home/does-not-exist", home=home) == pathlib.Path(
        "/home/does-not-exist")


def test_workspace_rejection_names_the_offending_path(tmp_path, monkeypatch):
    """The message must quote the rejected path, not only the root.

    The usual cause is an omitted cwd falling back to $HOME, and a message that
    names only the root reads as if the root itself had been refused.
    """
    import pytest

    from lib.launch_paths import validate_workspace_path

    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_WORKSPACE_ROOT", str(root))

    with pytest.raises(ValueError) as error:
        validate_workspace_path(outside)

    assert str(outside) in str(error.value)
    assert str(root) in str(error.value)
