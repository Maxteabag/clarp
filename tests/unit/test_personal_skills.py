import importlib


def _module(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLARP_CLAUDE_HOME", str(tmp_path / "data/claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "data/codex"))
    monkeypatch.setenv("CLARP_CLAUDE_SKILLS", str(tmp_path / "data/claude/skills"))
    monkeypatch.setenv("CLARP_CODEX_SKILLS", str(tmp_path / "data/codex/skills"))
    from lib import deployment, personal_skills
    importlib.reload(deployment)
    return importlib.reload(personal_skills)


def test_import_links_both_cli_homes(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    source = tmp_path / "example"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: example\ndescription: Portable example\n---\n")
    assert module.import_path(source) == ["example"]
    assert (tmp_path / "data/claude/skills/example").resolve() == (
        tmp_path / "data/skills/imported/example")
    assert (tmp_path / "data/codex/skills/example").resolve() == (
        tmp_path / "data/skills/imported/example")
    row = module.status()[0]
    assert row["health"] == "healthy"
    assert row["description"] == "Portable example"


def test_import_reports_host_specific_path(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    source = tmp_path / "host-bound"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: host-bound\ndescription: Host bound\n---\n"
        "Run /home/example/bin/helper.\n")
    module.import_path(source)
    row = module.status()[0]
    assert row["health"] == "host-path-dependency"
    assert row["requirements_ok"] is False


def test_import_rejects_symlink_outside_source(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    source = tmp_path / "linked"
    source.mkdir()
    (source / "SKILL.md").write_text("# Linked\n")
    secret = tmp_path / "secret"
    secret.write_text("credential")
    (source / "secret-link").symlink_to(secret)
    try:
        module.import_path(source)
    except ValueError as error:
        assert "symlink" in str(error)
    else:
        raise AssertionError("external symlink was imported")


def test_import_preserves_existing_non_owned_skill(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    source = tmp_path / "example"
    source.mkdir()
    (source / "SKILL.md").write_text("# Example\n")
    conflict = tmp_path / "data/claude/skills/example"
    conflict.mkdir(parents=True)
    (conflict / "SKILL.md").write_text("# Existing\n")
    try:
        module.import_path(source)
    except ValueError as error:
        assert "preserving existing skill" in str(error)
    else:
        raise AssertionError("existing personal skill was overwritten")


def test_replace_updates_owned_skill_without_breaking_links(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    source = tmp_path / "example"
    source.mkdir()
    skill_file = source / "SKILL.md"
    skill_file.write_text("# Version one\n")
    module.import_path(source)
    skill_file.write_text("# Version two\n")
    module.import_path(source, replace=True)
    assert (tmp_path / "data/claude/skills/example/SKILL.md").read_text() == "# Version two\n"
    assert (tmp_path / "data/codex/skills/example").is_symlink()


def test_multi_skill_import_rolls_back_when_later_skill_conflicts(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    source = tmp_path / "collection"
    for name in ("alpha", "zulu"):
        path = source / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"# {name}\n")
    conflict = tmp_path / "data/codex/skills/zulu"
    conflict.mkdir(parents=True)
    try:
        module.import_path(source)
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting collection was accepted")
    assert not (tmp_path / "data/skills/imported/alpha").exists()
    assert not (tmp_path / "data/claude/skills/alpha").exists()


def test_git_source_conflict_leaves_no_clone_or_links(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    import subprocess
    source = tmp_path / "source"
    skill = source / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Example\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "initial",
    ], cwd=source, check=True)
    conflict = tmp_path / "data/claude/skills/example"
    conflict.mkdir(parents=True)
    try:
        module.add_git(str(source), name="source")
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting Git source was accepted")
    assert not (tmp_path / "data/skills/git/source").exists()
    assert not (tmp_path / "data/skills/git/.source.next").exists()
    assert not (tmp_path / "data/codex/skills/example").exists()


def test_git_update_conflict_keeps_published_revision(monkeypatch, tmp_path):
    module = _module(monkeypatch, tmp_path)
    import subprocess
    source = tmp_path / "source"
    example = source / "example"
    example.mkdir(parents=True)
    (example / "SKILL.md").write_text("# One\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    commit = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*commit, "commit", "-qm", "one"], cwd=source, check=True)
    module.add_git(str(source), name="source")
    published = tmp_path / "data/skills/git/source/example/SKILL.md"
    assert published.read_text() == "# One\n"

    zulu = source / "zulu"
    zulu.mkdir()
    (zulu / "SKILL.md").write_text("# Conflict\n")
    (example / "SKILL.md").write_text("# Two\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run([*commit, "commit", "-qm", "two"], cwd=source, check=True)
    conflict = tmp_path / "data/codex/skills/zulu"
    conflict.mkdir(parents=True)
    try:
        module.update_git("source")
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting update was accepted")
    assert published.read_text() == "# One\n"
    assert not (tmp_path / "data/claude/skills/zulu").exists()
