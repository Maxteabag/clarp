"""Exclusive restore ownership of a container data root (lib.data_root_locks, docker/data-root-locks.sh).

Everything runs in a temporary data root: no Docker, container, volume, live Host or real restore.
Lock holders are real flock(2) locks taken by this test process or by short-lived child processes.
"""
import fcntl
import importlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tarfile
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
LOCK_SCRIPT = REPO / "docker" / "data-root-locks.sh"


@pytest.fixture
def root(monkeypatch, tmp_path):
    """A temporary container data root with a small non-WAL database."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(data / "clarp/state.sqlite"))
    monkeypatch.delenv("CLARP_RESTORE_ALLOW_FILESYSTEM", raising=False)
    from lib import data_root_locks, deployment, instance_backup
    importlib.reload(deployment)
    locks = importlib.reload(data_root_locks)
    backup = importlib.reload(instance_backup)
    database = data / "clarp/state.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("INSERT INTO marker VALUES ('before')")
    connection.commit()
    connection.close()
    return {"data": data, "locks": locks, "backup": backup, "database": database}


def _value(database):
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        connection.close()


def _set_value(database, value):
    connection = sqlite3.connect(database)
    connection.execute("UPDATE marker SET value = ?", (value,))
    connection.commit()
    connection.close()


def _open_lock(locks, name):
    directory = locks.ensure_backups_dir()
    return os.open(directory / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)


@pytest.fixture
def held(root, monkeypatch):
    """This process holds both locks EXCLUSIVE on fds the module treats as the inherited 8/9."""
    locks = root["locks"]
    owner, users = _open_lock(locks, locks.OWNER_LOCK), _open_lock(locks, locks.USERS_LOCK)
    fcntl.flock(owner, fcntl.LOCK_EX)
    fcntl.flock(users, fcntl.LOCK_EX)
    monkeypatch.setattr(locks, "OWNER_FD", owner)
    monkeypatch.setattr(locks, "USERS_FD", users)
    yield owner, users
    os.close(owner)
    os.close(users)


def _allow_this_filesystem(root, monkeypatch):
    locks = root["locks"]
    kind = locks.filesystem_type(root["data"])
    if kind not in locks.LOCAL_FILESYSTEMS:
        monkeypatch.setenv(locks.FILESYSTEM_OVERRIDE_ENV, kind)


def _stage_with_live_host(root):
    """Stage as `docker compose exec` into a running Host would: its owner lock is held elsewhere."""
    locks, backup = root["locks"], root["backup"]
    archive = backup.create()
    owner = _open_lock(locks, locks.OWNER_LOCK)
    try:
        # Non-blocking: when the `held` fixture already holds it, that already stands in for the
        # live Host (flock(2) conflicts between two descriptions even in one process).
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    try:
        pending = backup.stage_restore(archive)
    finally:
        os.close(owner)
    return archive, pending


# ---- identity of the inherited lock fds -------------------------------------------------------

def test_only_the_expected_lock_file_is_accepted_as_an_inherited_lock_fd(root, tmp_path):
    locks = root["locks"]
    fd = _open_lock(locks, locks.OWNER_LOCK)
    other = os.open(tmp_path / "other", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        assert locks.inherited_lock_fd(fd, locks.OWNER_LOCK) is True
        assert locks.inherited_lock_fd(other, locks.OWNER_LOCK) is False
        assert locks.inherited_lock_fd(fd, locks.USERS_LOCK) is False
        assert locks.inherited_lock_fd(9999, locks.OWNER_LOCK) is False
    finally:
        os.close(fd)
        os.close(other)


def test_a_symlinked_lock_path_is_refused_and_its_target_is_not_truncated(root, tmp_path):
    locks = root["locks"]
    directory = locks.ensure_backups_dir()
    target = tmp_path / "precious"
    target.write_text("keep me")
    (directory / locks.USERS_LOCK).symlink_to(target)
    fd = os.open(target, os.O_WRONLY | os.O_APPEND)
    try:
        assert locks.inherited_lock_fd(fd, locks.USERS_LOCK) is False
    finally:
        os.close(fd)
    assert target.read_text() == "keep me"


def test_a_group_writable_lock_directory_is_refused_not_repaired(root):
    locks = root["locks"]
    directory = locks.ensure_backups_dir()
    directory.chmod(0o770)
    with pytest.raises(locks.RestoreRefused) as refused:
        locks.ensure_backups_dir()
    assert refused.value.code == locks.EXIT_LOCKING
    assert directory.stat().st_mode & 0o777 == 0o770


# ---- X7: the stack keeps its locks but terminals get no copy ----------------------------------

def test_sealing_marks_only_the_matching_lock_fds_non_inheritable(root, tmp_path, monkeypatch):
    locks = root["locks"]
    owner, users = _open_lock(locks, locks.OWNER_LOCK), _open_lock(locks, locks.USERS_LOCK)
    unrelated = os.open(tmp_path / "unrelated", os.O_WRONLY | os.O_CREAT, 0o600)
    for fd in (owner, users, unrelated):
        os.set_inheritable(fd, True)
    monkeypatch.setattr(locks, "OWNER_FD", owner)
    monkeypatch.setattr(locks, "USERS_FD", users)
    try:
        assert sorted(locks.seal_inherited_lock_fds()) == sorted([owner, users])
        assert not os.get_inheritable(owner) and not os.get_inheritable(users)
        assert os.get_inheritable(unrelated)
        child = subprocess.run(["ls", "/proc/self/fd"], close_fds=False, capture_output=True,
                               text=True, check=True)
        listed = set(child.stdout.split())
        assert str(owner) not in listed and str(users) not in listed      # no copy for a child
        assert str(unrelated) in listed
    finally:
        for fd in (owner, users, unrelated):
            os.close(fd)


def test_sealing_touches_nothing_when_fds_are_not_the_lock_files(root, tmp_path, monkeypatch):
    locks = root["locks"]
    foreign = os.open(tmp_path / "foreign", os.O_WRONLY | os.O_CREAT, 0o600)
    os.set_inheritable(foreign, True)
    monkeypatch.setattr(locks, "OWNER_FD", foreign)
    monkeypatch.setattr(locks, "USERS_FD", foreign)
    try:
        assert locks.seal_inherited_lock_fds() == []
        assert os.get_inheritable(foreign)
    finally:
        os.close(foreign)


# ---- held-lock self-probe (Rule E) -------------------------------------------------------------

def test_a_fresh_open_elsewhere_sees_a_held_lock_and_takes_a_free_one(root):
    locks = root["locks"]
    fd = _open_lock(locks, locks.OWNER_LOCK)
    path = locks.backups_dir() / locks.OWNER_LOCK
    try:
        assert locks.held_elsewhere(path) is False
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert locks.held_elsewhere(path) is True
        fcntl.flock(fd, fcntl.LOCK_UN)
        assert locks.held_elsewhere(path) is False
    finally:
        os.close(fd)


def test_a_lock_survives_the_helper_that_took_it(root):
    """The shell owns the open file description; a helper process that locked the INHERITED fd
    exits, and a fresh open elsewhere still cannot take the lock."""
    locks = root["locks"]
    path = locks.ensure_backups_dir() / locks.USERS_LOCK
    script = f'exec 9>>"{path}"; flock -x -n 9 && "{sys.executable}" -c "import sys; sys.exit(0)"; ' \
             f'flock -x -n -E 75 "{path}" true; echo "probe=$?"'
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
    assert "probe=75" in out


def test_require_held_refuses_an_unlocked_or_foreign_fd(root, monkeypatch, tmp_path):
    locks = root["locks"]
    fd = _open_lock(locks, locks.OWNER_LOCK)
    try:
        with pytest.raises(locks.RestoreRefused) as unlocked:
            locks.require_held(fd, locks.OWNER_LOCK)
        assert unlocked.value.code == locks.EXIT_LOCKING
        with pytest.raises(locks.RestoreRefused):
            locks.require_held(fd, locks.USERS_LOCK)
    finally:
        os.close(fd)


# ---- SQLite dead-man-switch probe ------------------------------------------------------------

def test_the_dead_man_switch_probe_sees_another_process_holding_byte_128(root, tmp_path):
    locks = root["locks"]
    shm = tmp_path / "state.sqlite-shm"
    assert locks.dms_conflict(shm) is False                             # no -shm: no WAL user
    shm.write_bytes(b"\0" * 4096)
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import fcntl, os, sys
        fd = os.open({str(shm)!r}, os.O_RDWR)
        fcntl.lockf(fd, fcntl.LOCK_SH, 1, {locks.SHM_DMS_OFFSET}, os.SEEK_SET)
        print("held", flush=True)
        sys.stdin.read()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert locks.dms_conflict(shm) is True
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert locks.dms_conflict(shm) is False


# ---- archives and staging ----------------------------------------------------------------------

def test_an_archive_member_inside_clarp_backups_is_refused(root, tmp_path):
    backup = root["backup"]
    for name in ("clarp/backups/host-owner.lock", "./clarp/backups/restore.pending.owner",
                 "clarp/backups"):
        archive = tmp_path / "evil.tar.gz"
        payload = tmp_path / "payload"
        payload.write_text("x")
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(root["database"], arcname="clarp/state.sqlite")
            handle.add(payload, arcname=name)
        with pytest.raises(ValueError, match="inside clarp/backups"):
            backup.verify(archive)


def test_staging_needs_a_live_host_and_binds_the_exact_archive(root):
    locks, backup = root["locks"], root["backup"]
    archive = backup.create()
    with pytest.raises(locks.RestoreRefused) as refused:
        backup.stage_restore(archive)                                   # no Host holds the owner lock
    assert refused.value.code == locks.EXIT_REFUSED
    assert not (locks.backups_dir() / locks.PENDING).exists()

    stale = locks.backups_dir() / locks.SIDECAR
    stale.write_text(json.dumps({"sha256": "0" * 64, "size": 1}))
    _, pending = _stage_with_live_host(root)
    record = json.loads(stale.read_text())
    assert record["size"] == pending.stat().st_size
    assert record["sha256"] == locks.sha256_file(pending)
    assert record["evidence"] == {"host_owner_held": True}


# ---- apply ------------------------------------------------------------------------------------

def test_apply_refuses_without_exclusive_locks_and_keeps_everything(root):
    locks, backup = root["locks"], root["backup"]
    _stage_with_live_host(root)
    _set_value(root["database"], "after")
    with pytest.raises(locks.RestoreRefused) as refused:
        backup.apply_pending_restore()
    assert refused.value.code == locks.EXIT_LOCKING
    assert _value(root["database"]) == "after"
    assert (locks.backups_dir() / locks.PENDING).exists()
    assert (locks.backups_dir() / locks.SIDECAR).exists()
    assert not (locks.backups_dir() / locks.MARKER).exists()


def test_apply_refuses_an_archive_changed_after_staging(root, held, monkeypatch):
    locks, backup = root["locks"], root["backup"]
    _allow_this_filesystem(root, monkeypatch)
    _, pending = _stage_with_live_host(root)
    _set_value(root["database"], "after")
    with open(pending, "ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(locks.RestoreRefused) as refused:
        backup.apply_pending_restore()
    assert refused.value.code == locks.EXIT_REFUSED
    assert _value(root["database"]) == "after"
    assert pending.exists() and not (locks.backups_dir() / locks.MARKER).exists()


def test_apply_refuses_a_staging_without_a_record(root, held, monkeypatch):
    locks, backup = root["locks"], root["backup"]
    _allow_this_filesystem(root, monkeypatch)
    _stage_with_live_host(root)
    (locks.backups_dir() / locks.SIDECAR).unlink()
    with pytest.raises(locks.RestoreRefused) as refused:
        backup.apply_pending_restore()
    assert refused.value.code == locks.EXIT_REFUSED


def test_apply_refuses_a_remote_filesystem(root, held, monkeypatch):
    locks, backup = root["locks"], root["backup"]
    _stage_with_live_host(root)
    monkeypatch.setattr(locks, "filesystem_type", lambda path: "nfs4")
    with pytest.raises(locks.RestoreRefused) as refused:
        backup.apply_pending_restore()
    assert refused.value.code == locks.EXIT_LOCKING
    monkeypatch.setattr(locks, "filesystem_type", lambda path: "tmpfs")
    with pytest.raises(locks.RestoreRefused):
        backup.apply_pending_restore()                                   # off-list without override


def test_apply_restores_and_removes_archive_record_and_marker_in_order(root, held, monkeypatch):
    locks, backup = root["locks"], root["backup"]
    _allow_this_filesystem(root, monkeypatch)
    _stage_with_live_host(root)
    _set_value(root["database"], "after")
    (root["data"] / "added-after-backup").write_text("must disappear")
    owner_lock = locks.backups_dir() / locks.OWNER_LOCK
    inode = os.stat(owner_lock).st_ino
    removed = []
    real_unlink = pathlib.Path.unlink

    def recording_unlink(self, *args, **kwargs):
        if self.parent == locks.backups_dir():
            removed.append(self.name)
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "unlink", recording_unlink)

    assert backup.apply_pending_restore() is True
    assert _value(root["database"]) == "before"
    assert not (root["data"] / "added-after-backup").exists()
    assert removed == [locks.PENDING, locks.SIDECAR, locks.MARKER]      # the marker LAST
    assert os.stat(owner_lock).st_ino == inode                          # lock files preserved
    assert backup.apply_pending_restore() is False


# ---- marker recovery ---------------------------------------------------------------------------

def test_marker_recovery_tells_a_completed_apply_from_an_interrupted_one(root):
    locks = root["locks"]
    directory = locks.ensure_backups_dir()
    assert locks.recover_marker() == "none"
    (directory / locks.MARKER).write_text("1")
    (directory / locks.PENDING).write_bytes(b"archive")
    assert locks.recover_marker() == "interrupted"                      # re-applied by the Host start
    assert (directory / locks.MARKER).exists() and (directory / locks.PENDING).exists()
    (directory / locks.PENDING).unlink()
    (directory / locks.SIDECAR).write_text("{}")                        # left by a crash mid-cleanup
    assert locks.recover_marker() == "completed"
    assert not (directory / locks.MARKER).exists() and not (directory / locks.SIDECAR).exists()


# ---- the entrypoint lock sequence (docker/data-root-locks.sh) ---------------------------------

@pytest.fixture
def entry(root, tmp_path):
    """Runs the real sourced lock script in a child shell against the temporary data root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "python3"           # exec, not a symlink: keeps the test interpreter's venv
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home"),
           "CLARP_DATA_DIR": str(root["data"]), "CLARP_DEPLOYMENT_MODE": "container",
           "CLARP_SHARE_DIR": str(REPO / "server"),
           "CLAUDE_PWA_DB": str(root["database"]), "LANG": "C.UTF-8"}

    def run(*command, extra_env=None):
        script = f'. "{LOCK_SCRIPT}"; clarp_data_root_locks "$@"; echo "role=$CLARP_ENTRYPOINT_ROLE"'
        return subprocess.run(["sh", "-c", script, "entry", *command], env={**env, **(extra_env or {})},
                              capture_output=True, text=True, timeout=60)
    run.bin_dir = bin_dir
    return run


SERVER = ("python3", "/opt/clarp/server.py")


def test_the_host_start_is_identified_exactly(entry):
    assert "role=server" in entry(*SERVER).stdout
    assert "role=maintenance" in entry(*SERVER, "--extra").stdout
    assert "role=maintenance" in entry("clarp-admin", "doctor").stdout


def test_a_second_host_on_a_live_root_refuses_with_75(root, entry):
    locks = root["locks"]
    owner = _open_lock(locks, locks.OWNER_LOCK)
    fcntl.flock(owner, fcntl.LOCK_EX)
    try:
        result = entry(*SERVER)
    finally:
        os.close(owner)
    assert result.returncode == 75 and "role=" not in result.stdout


def test_a_pending_restore_refuses_with_76_while_another_user_holds_the_root(root, entry):
    locks = root["locks"]
    _stage_with_live_host(root)
    users = _open_lock(locks, locks.USERS_LOCK)
    fcntl.flock(users, fcntl.LOCK_SH)
    try:
        result = entry(*SERVER)
    finally:
        os.close(users)
    assert result.returncode == 76
    assert (locks.backups_dir() / locks.PENDING).exists()
    assert _value(root["database"]) == "before"


def test_a_host_start_with_a_maintenance_holder_and_nothing_staged_starts(root, entry):
    locks = root["locks"]
    users = _open_lock(locks, locks.USERS_LOCK)
    fcntl.flock(users, fcntl.LOCK_SH)
    try:
        result = entry(*SERVER)
    finally:
        os.close(users)
    assert result.returncode == 0 and "role=server" in result.stdout


def test_a_locking_error_refuses_only_a_restore(root, entry):
    shim = entry.bin_dir / "flock"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    started = entry(*SERVER)
    assert started.returncode == 0 and "role=server" in started.stdout
    assert "single-Host guarantee is unavailable" in started.stderr
    shim.unlink()
    _stage_with_live_host(root)
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    refused = entry(*SERVER)
    assert refused.returncode == 77
    assert (root["locks"].backups_dir() / root["locks"].PENDING).exists()


def test_maintenance_refuses_a_half_swapped_root_and_never_applies(root, entry):
    locks = root["locks"]
    _stage_with_live_host(root)
    assert "role=maintenance" in entry("clarp-admin", "doctor").stdout
    assert (locks.backups_dir() / locks.PENDING).exists()                # never applied by maintenance
    (locks.backups_dir() / locks.MARKER).write_text("1")
    assert entry("clarp-admin", "doctor").returncode == 76


def test_a_completed_marker_is_recovered_before_the_host_starts(root, entry):
    locks = root["locks"]
    directory = locks.ensure_backups_dir()
    (directory / locks.MARKER).write_text("1")
    (directory / locks.SIDECAR).write_text("{}")
    result = entry(*SERVER)
    assert result.returncode == 0 and "role=server" in result.stdout
    assert not (directory / locks.MARKER).exists() and not (directory / locks.SIDECAR).exists()
