import importlib
import subprocess


def _module(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLARP_WORKSPACE_ROOT", str(workspace))
    from lib import deployment, workspace_repos
    importlib.reload(deployment)
    return importlib.reload(workspace_repos), workspace


def test_clone_and_health_are_workspace_confined(monkeypatch, tmp_path):
    repos, workspace = _module(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    (source / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "initial",
    ], cwd=source, check=True)

    row = repos.clone(str(source), name="project")
    assert row["path"] == str((workspace / "project").resolve())
    assert row["branch"] == "main"
    assert row["dirty"] is False
    assert repos.list_repositories()[0]["name"] == "project"
