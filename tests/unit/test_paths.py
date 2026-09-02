"""RuntimePaths: one product name, XDG bases, and the data/cache split."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.paths import RuntimePaths  # noqa: E402
from lib import xdg  # noqa: E402


def test_runtime_paths_are_derived_from_home(monkeypatch):
    monkeypatch.setattr(xdg.sys, "platform", "linux")
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "CLARP_CONFIG_DIR", "CLARP_SHARE_DIR",
                "CLARP_CACHE_DIR", "CLARP_DATA_DIR",
                "CLARP_UPLOADS_DIR", "CLARP_MEDIA_DIR"):
        monkeypatch.delenv(var, raising=False)
    home = pathlib.Path("/tmp/example-home")
    paths = RuntimePaths.from_home(home)

    assert paths.cache_dir == home / ".cache" / "clarp"
    assert paths.config_dir == home / ".config" / "clarp"
    assert paths.data_dir == home / ".local" / "share" / "clarp"
    assert paths.app_session == home / ".cache" / "clarp" / "current-session"
    assert paths.source_markers_dir == home / ".cache" / "clarp" / "source-markers"
    assert paths.source_marker("rachel/../x") == (
        home / ".cache" / "clarp" / "source-markers" / "rachel..x"
    )
    assert paths.audio_dir == home / ".cache" / "clarp" / "audio"
    assert paths.hook_log == home / ".cache" / "clarp" / "hook.log"


def test_uploads_and_media_are_data_not_cache(monkeypatch):
    """A phone upload is the user's only copy, so `rm -rf ~/.cache/*` must not
    reach it. The container layout has always treated these as data."""
    monkeypatch.setattr(xdg.sys, "platform", "linux")
    for var in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "CLARP_SHARE_DIR",
                "CLARP_UPLOADS_DIR", "CLARP_MEDIA_DIR", "CLARP_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    home = pathlib.Path("/tmp/example-home")
    paths = RuntimePaths.from_home(home)
    assert paths.uploads_dir == home / ".local" / "share" / "clarp" / "uploads"
    assert paths.media_dir == home / ".local" / "share" / "clarp" / "media"
    assert ".cache" not in str(paths.uploads_dir)
    assert ".cache" not in str(paths.media_dir)


def test_xdg_env_vars_are_honoured(monkeypatch, tmp_path):
    monkeypatch.setattr(xdg.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "dat"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
    for var in ("CLARP_CONFIG_DIR", "CLARP_SHARE_DIR",
                "CLARP_CACHE_DIR", "CLARP_DATA_DIR",
                "CLARP_UPLOADS_DIR", "CLARP_MEDIA_DIR"):
        monkeypatch.delenv(var, raising=False)
    paths = RuntimePaths.from_home(tmp_path / "home")
    assert paths.config_dir == tmp_path / "cfg" / "clarp"
    assert paths.data_dir == tmp_path / "dat" / "clarp"
    assert paths.cache_dir == tmp_path / "cch" / "clarp"


def test_relative_xdg_value_is_ignored_per_spec(monkeypatch):
    """The spec says a relative $XDG_*_HOME must be ignored."""
    monkeypatch.setattr(xdg.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/nope")
    home = pathlib.Path("/tmp/example-home")
    assert xdg.config_dir(home) == home / ".config" / "clarp"


def test_clarp_env_overrides_still_win(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path / "share"))
    monkeypatch.setenv("CLARP_MEDIA_DIR", str(tmp_path / "m"))
    paths = RuntimePaths.from_home(tmp_path / "home")
    assert paths.cache_dir == tmp_path / "c"
    assert paths.config_dir == tmp_path / "cfg"
    assert paths.data_dir == tmp_path / "share"
    assert paths.media_dir == tmp_path / "m"


def test_macos_uses_native_user_library_paths(monkeypatch):
    monkeypatch.setattr(xdg.sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    home = pathlib.Path("/Users/example")

    assert xdg.config_dir(home) == home / "Library/Application Support/Clarp"
    assert xdg.data_dir(home) == home / "Library/Application Support/Clarp"
    assert xdg.cache_dir(home) == home / "Library/Caches/Clarp"
    assert xdg.log_dir(home) == home / "Library/Logs/Clarp"
