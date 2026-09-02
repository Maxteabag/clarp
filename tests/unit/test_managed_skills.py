from __future__ import annotations

import json

from lib import managed_skills


def configure(tmp_path, monkeypatch):
    release = tmp_path / "share/releases/current"
    manifest = release / "skills/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"skills": [
        {"id": "clarp-core", "pack": "core", "description": "Core"},
        {"id": "clarp-extra", "pack": "native", "description": "Extra"},
    ]}))
    for skill_id in ("clarp-core", "clarp-extra"):
        path = release / "skills" / skill_id
        path.mkdir()
        (path / "SKILL.md").write_text(skill_id)
    state = tmp_path / "config/install.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"skills": ["clarp-core"]}))
    monkeypatch.setattr(managed_skills, "RELEASE_ROOT", release)
    monkeypatch.setattr(managed_skills, "SHARE", tmp_path / "share")
    monkeypatch.setattr(managed_skills, "INSTALL_STATE", state)
    monkeypatch.setattr(managed_skills, "CLAUDE_SKILLS", tmp_path / "claude")
    monkeypatch.setattr(managed_skills, "CODEX_SKILLS", tmp_path / "codex")
    return release, state


def test_skill_health_and_optional_toggle(tmp_path, monkeypatch):
    _release, state = configure(tmp_path, monkeypatch)
    managed_skills.set_enabled("clarp-core", True)
    rows = {row["id"]: row for row in managed_skills.status()}
    assert rows["clarp-core"]["health"] == "healthy"
    assert rows["clarp-extra"]["health"] == "inactive"

    managed_skills.set_enabled("clarp-extra", True)
    assert next(row for row in managed_skills.status()
                if row["id"] == "clarp-extra")["health"] == "healthy"
    managed_skills.set_enabled("clarp-extra", False)
    assert json.loads(state.read_text())["skills"] == ["clarp-core"]


def test_modified_user_skill_is_preserved(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    conflict = managed_skills.CLAUDE_SKILLS / "clarp-extra"
    conflict.mkdir(parents=True)
    (conflict / "SKILL.md").write_text("mine")
    import pytest
    with pytest.raises(ValueError, match="preserving non-Clarp"):
        managed_skills.set_enabled("clarp-extra", True)
    assert (conflict / "SKILL.md").read_text() == "mine"


def test_second_backend_conflict_does_not_partially_enable(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    conflict = managed_skills.CODEX_SKILLS / "clarp-extra"
    conflict.mkdir(parents=True)
    (conflict / "SKILL.md").write_text("mine")
    import pytest
    with pytest.raises(ValueError, match="preserving non-Clarp"):
        managed_skills.set_enabled("clarp-extra", True)
    assert not (managed_skills.CLAUDE_SKILLS / "clarp-extra").exists()


def test_dangling_user_symlink_is_conflict_and_preserved(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    destination = managed_skills.CLAUDE_SKILLS / "clarp-extra"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(tmp_path / "user-skill-that-moved", target_is_directory=True)
    row = next(row for row in managed_skills.status() if row["id"] == "clarp-extra")
    assert row["health"] == "modified"
    import pytest
    with pytest.raises(ValueError, match="preserving non-Clarp"):
        managed_skills.set_enabled("clarp-extra", True)
    assert destination.is_symlink()


def test_link_to_different_release_skill_is_conflict(tmp_path, monkeypatch):
    release, _state = configure(tmp_path, monkeypatch)
    destination = managed_skills.CLAUDE_SKILLS / "clarp-extra"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(release / "skills/clarp-core", target_is_directory=True)
    row = next(row for row in managed_skills.status() if row["id"] == "clarp-extra")
    assert row["health"] == "modified"


def test_second_backend_io_failure_rolls_back_first_link(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    original = managed_skills.Path.symlink_to

    def fail_codex(path, target, *args, **kwargs):
        if path.parent == managed_skills.CODEX_SKILLS:
            raise OSError("read only")
        return original(path, target, *args, **kwargs)

    monkeypatch.setattr(managed_skills.Path, "symlink_to", fail_codex)
    import pytest
    with pytest.raises(OSError, match="read only"):
        managed_skills.set_enabled("clarp-extra", True)
    assert not (managed_skills.CLAUDE_SKILLS / "clarp-extra").exists()


def test_core_skill_cannot_be_disabled(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(ValueError, match="core"):
        managed_skills.set_enabled("clarp-core", False)
