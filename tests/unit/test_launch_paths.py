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
