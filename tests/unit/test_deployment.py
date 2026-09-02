from lib.deployment import DeploymentLayout, deployment_mode


def test_native_layout_preserves_existing_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLARP_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_PWA_DB", raising=False)
    monkeypatch.delenv("CLARP_SHARE_DIR", raising=False)
    layout = DeploymentLayout.from_environment()
    assert deployment_mode() == "native"
    assert layout.share == tmp_path / ".local/share/clarp"
    assert layout.state_database == layout.share / "state.sqlite"
    assert layout.claude_home == tmp_path / ".claude"
    assert layout.codex_home == tmp_path / ".codex"


def test_container_layout_uses_one_data_root(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(data))
    monkeypatch.delenv("CLAUDE_PWA_DB", raising=False)
    layout = DeploymentLayout.from_environment()
    assert layout.mode == "container"
    assert layout.state_database == data / "clarp/state.sqlite"
    assert layout.claude_home == data / "claude"
    assert layout.codex_home == data / "codex"
    assert layout.workspace_root == data / "workspace"


def test_container_layout_creates_only_under_data_and_cache(monkeypatch, tmp_path):
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(data))
    monkeypatch.setenv("CLARP_CACHE_DIR", str(cache))
    layout = DeploymentLayout.from_environment()
    layout.create_container_directories()
    assert (data / "clarp/backups").is_dir()
    assert (data / "skills/imported").is_dir()
    assert (data / "workspace").is_dir()
    assert cache.is_dir()


def test_explicit_container_paths_override_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLARP_MEDIA_DIR", str(tmp_path / "special-media"))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "state.db"))
    layout = DeploymentLayout.from_environment()
    assert layout.media_dir == tmp_path / "special-media"
    assert layout.state_database == tmp_path / "state.db"
