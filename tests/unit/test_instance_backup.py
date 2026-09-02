import importlib
import pathlib
import sqlite3
import tarfile


def _module(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "clarp/state.sqlite"))
    from lib import deployment, instance_backup
    importlib.reload(deployment)
    return importlib.reload(instance_backup)


def _database(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))


def test_backup_create_verify_and_restore(monkeypatch, tmp_path):
    backup = _module(monkeypatch, tmp_path)
    database = tmp_path / "clarp/state.sqlite"
    _database(database, "before")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "work.txt").write_text("preserved")
    (workspace / "work-link").symlink_to("work.txt")
    cli_tmp = tmp_path / "codex/tmp"
    cli_tmp.mkdir(parents=True)
    (cli_tmp / "generated-link").symlink_to("/usr/bin/true")

    archive = backup.create()
    assert archive.is_file()
    assert backup.verify(archive)["ok"] is True

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE marker SET value='after'")
    (workspace / "work.txt").write_text("changed")
    (tmp_path / "credentials-added-after-backup").write_text("must disappear")
    backup.stage_restore(archive)
    assert backup.apply_pending_restore() is True

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "before"
    assert (workspace / "work.txt").read_text() == "preserved"
    assert (workspace / "work-link").is_symlink()
    assert (workspace / "work-link").read_text() == "preserved"
    assert not (tmp_path / "credentials-added-after-backup").exists()
    assert backup.apply_pending_restore() is False


def test_backup_rejects_traversal(monkeypatch, tmp_path):
    backup = _module(monkeypatch, tmp_path)
    archive = tmp_path / "bad.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("bad")
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(payload, arcname="../escape")
    try:
        backup.verify(archive)
    except ValueError as error:
        assert "unsafe backup member" in str(error)
    else:
        raise AssertionError("unsafe archive accepted")
