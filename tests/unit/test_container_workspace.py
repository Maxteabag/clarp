import pathlib

import pytest

from lib import agent_lifecycle
from lib.launch_paths import (existing_workspace_path, recover_user_path,
                              validate_workspace_path)


def test_container_defaults_to_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_WORKSPACE_ROOT", str(workspace))
    assert recover_user_path("") == workspace.resolve()


def test_container_rejects_path_outside_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_WORKSPACE_ROOT", str(workspace))
    with pytest.raises(ValueError):
        validate_workspace_path(outside)
    with pytest.raises(agent_lifecycle.AgentLifecycleError) as error:
        agent_lifecycle._existing_cwd(outside)
    assert error.value.status == 403
    assert existing_workspace_path(outside) == workspace.resolve()


def test_native_paths_remain_unrestricted(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.delenv("CLARP_DEPLOYMENT_MODE", raising=False)
    assert validate_workspace_path(outside) == outside.resolve()
