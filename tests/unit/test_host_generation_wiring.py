"""Host-generation wiring, slice C2-S2a (design r4 §12-§14, C2-S2 packet and addendum 1).

The token is bootstrapped at every process start before the state database is opened, and rotated
only by the supported exclusive restore before its first destructive step. Nothing reads it yet.

Rotation and the import check run real file operations on owned temporary roots. The runtime's call
order is recorded through its real main(). The server's `__main__` block and the entrypoint heredoc
cannot be called in a unit test: their order is checked structurally, from the production files
themselves, and the container proof of them is a CI step of its own.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
from lib import host_generation as hg  # noqa: E402

OLD = "5d2f8a3e-1c4b-4e7a-8f60-3b9d7c2e4a10"


@pytest.fixture
def backups(tmp_path):
    path = tmp_path / "data" / "clarp" / "backups"
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


@pytest.fixture
def os_patch():
    """Patches on os undone at this fixture's teardown, before conftest's own teardown runs."""
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def _token(backups, text=OLD + "\n", mode=0o600):
    path = backups / hg.TOKEN_NAME
    path.write_bytes(text.encode())
    path.chmod(mode)
    return path


def _valid(path: pathlib.Path) -> str:
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
    text = path.read_text()
    assert text.endswith("\n") and str(uuid.UUID(text[:-1])) == text[:-1]
    return text[:-1]


def _entries(directory: pathlib.Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ---- rotation (only the exclusive restore calls it) ------------------------------------------

def test_rotation_replaces_a_valid_token_with_a_fresh_one(backups):
    _token(backups)
    returned = hg.rotate_instance_generation(backups_dir=backups)
    token = _valid(backups / hg.TOKEN_NAME)
    assert token == returned and token != OLD
    assert _entries(backups) == [hg.TOKEN_NAME]


def test_rotation_creates_the_token_when_absent(backups):
    token = hg.rotate_instance_generation(backups_dir=backups)
    assert _valid(backups / hg.TOKEN_NAME) == token


def test_rotation_replaces_an_invalid_token_rather_than_trusting_it(backups):
    _token(backups, "not-a-uuid\n", 0o644)
    token = hg.rotate_instance_generation(backups_dir=backups)
    assert _valid(backups / hg.TOKEN_NAME) == token


def test_rotation_replaces_a_symlink_at_the_name_without_following_it(backups, tmp_path):
    outside = tmp_path / "target-dir"
    outside.mkdir()
    target = outside / "elsewhere"
    target.write_text("keep")
    link = backups / hg.TOKEN_NAME
    link.symlink_to(target)
    before = (os.lstat(target).st_ino, os.lstat(target).st_mtime_ns, target.read_bytes())
    token = hg.rotate_instance_generation(backups_dir=backups)
    assert _valid(link) == token                                    # a regular file now
    assert (os.lstat(target).st_ino, os.lstat(target).st_mtime_ns, target.read_bytes()) == before


def test_a_directory_at_the_name_refuses_rotation_loudly(backups):
    (backups / hg.TOKEN_NAME).mkdir(mode=0o700)
    with pytest.raises(hg.RotationRefused):
        hg.rotate_instance_generation(backups_dir=backups)
    assert (backups / hg.TOKEN_NAME).is_dir()
    assert _entries(backups) == [hg.TOKEN_NAME]                     # no temporary left behind


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_a_writable_backups_directory_refuses_rotation_and_changes_nothing(backups, mode):
    path = _token(backups)
    backups.chmod(mode)
    try:
        with pytest.raises(hg.RotationRefused):
            hg.rotate_instance_generation(backups_dir=backups)
        assert path.read_text() == OLD + "\n"
        assert _entries(backups) == [hg.TOKEN_NAME]
    finally:
        backups.chmod(0o700)


def test_a_symlinked_or_foreign_backups_directory_refuses_rotation(backups, tmp_path):
    _token(backups)
    link = tmp_path / "linked-backups"
    link.symlink_to(backups, target_is_directory=True)
    with pytest.raises(hg.RotationRefused):
        hg.rotate_instance_generation(backups_dir=link)
    with pytest.raises(hg.RotationRefused):
        hg.rotate_instance_generation(backups_dir=backups, uid=os.geteuid() + 1)
    assert (backups / hg.TOKEN_NAME).read_text() == OLD + "\n"


def test_a_failed_write_refuses_rotation_and_keeps_the_old_token(backups, os_patch):
    _token(backups)
    real_fsync = os.fsync

    def failing_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        return real_fsync(fd)
    os_patch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(hg.RotationRefused):
        hg.rotate_instance_generation(backups_dir=backups)
    assert (backups / hg.TOKEN_NAME).read_text() == OLD + "\n"
    assert _entries(backups) == [hg.TOKEN_NAME]


def test_a_failed_replace_refuses_rotation_and_removes_the_temporary(backups, os_patch):
    _token(backups)

    def failing_replace(*args, **kwargs):
        raise OSError(errno.EIO, "injected")
    os_patch.setattr(os, "replace", failing_replace)
    with pytest.raises(hg.RotationRefused):
        hg.rotate_instance_generation(backups_dir=backups)
    assert (backups / hg.TOKEN_NAME).read_text() == OLD + "\n"
    assert _entries(backups) == [hg.TOKEN_NAME]


def test_a_failed_directory_sync_after_the_replace_says_the_token_was_replaced(backups, os_patch):
    _token(backups)
    real_fsync = os.fsync

    def failing_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        return real_fsync(fd)
    os_patch.setattr(os, "fsync", failing_dir_fsync)
    with pytest.raises(hg.RotationRefused) as refused:
        hg.rotate_instance_generation(backups_dir=backups)
    assert "already replaced" in str(refused.value)
    assert _valid(backups / hg.TOKEN_NAME) != OLD                  # it was, and says so


def test_a_failed_cleanup_is_reported_in_the_refusal_not_swallowed(backups, os_patch):
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "injected")
    os_patch.setattr(os, "replace", fail)
    os_patch.setattr(os, "unlink", fail)
    with pytest.raises(hg.RotationRefused) as refused:
        hg.rotate_instance_generation(backups_dir=backups)
    assert "not removed" in str(refused.value)


# ---- default paths: no test and no import ever touches the real ones ------------------------

@pytest.mark.parametrize("call", [
    lambda: hg.read_host_generation(),
    lambda: hg.read_host_generation(backups_dir=pathlib.Path("/nonexistent")),
    lambda: hg.ensure_instance_generation(),
    lambda: hg.rotate_instance_generation(),
])
def test_the_test_guard_refuses_every_default_generation_path(call):
    with pytest.raises(pytest.fail.Exception, match="default host-generation path"):
        call()


@pytest.mark.parametrize("mode", ["container", "native"])
def test_importing_the_module_reads_no_generation_path(tmp_path, mode):
    """A fresh interpreter imports lib.host_generation under an audit hook: no open, listdir or
    scandir of its data root or of the kernel boot id happens at import (CPython raises no audit
    event for stat). Both LAYOUT branches: a container data root, and a native one under HOME."""
    home = tmp_path / "home"
    home.mkdir()
    probe = f"""
import json, sys
sys.path.insert(0, {str(REPO / 'server')!r})
seen = []
def hook(event, args):
    if event in ("open", "os.listdir", "os.scandir") and args:
        seen.append(str(args[0]))
sys.addaudithook(hook)
import lib.host_generation
print(json.dumps({{"seen": seen, "data_root": str(lib.host_generation.LAYOUT.data_root)}}))
"""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}
    if mode == "container":
        env.update(CLARP_DEPLOYMENT_MODE="container", CLARP_DATA_DIR=str(tmp_path / "import-data"))
    result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["data_root"].startswith(str(tmp_path))         # the probe never saw a real root
    assert not [p for p in report["seen"]
                if p.startswith(report["data_root"]) or "kernel/random/boot_id" in p]


# ---- bootstrap never stops a start (design r4 §12 D) -----------------------------------------

def test_an_unexpected_bootstrap_error_is_logged_and_never_raised(os_patch):
    logged = []
    os_patch.setattr(hg, "log_exception", lambda event, exc, detail="": logged.append(
        (event, type(exc).__name__)))
    assert hg.ensure_instance_generation(backups_dir=object()) is False     # TypeError inside
    assert logged and logged[-1][1] == "TypeError"


def test_an_unresolvable_default_path_is_logged_and_never_raised(os_patch):
    logged = []
    os_patch.setattr(hg, "log_exception", lambda event, exc, detail="": logged.append(
        (event, type(exc).__name__)))

    def broken_layout():
        raise RuntimeError("no data root")
    os_patch.setattr(hg, "default_backups_dir", broken_layout)
    assert hg.ensure_instance_generation() is False
    assert logged and logged[-1][1] == "RuntimeError"


# ---- bootstrap call order --------------------------------------------------------------------

def test_the_runtime_seals_its_lock_fds_then_bootstraps_before_any_context(os_patch):
    """runtime.main(): the real call order up to the first ServerContext (which opens state)."""
    import runtime
    from lib import data_root_locks
    order = []

    class Stop(Exception):
        pass

    class FakeContext:
        @classmethod
        def production(cls, **kwargs):
            order.append("context")
            raise Stop

    os_patch.setattr(data_root_locks, "seal_inherited_lock_fds", lambda: order.append("seal"))
    os_patch.setattr(hg, "ensure_instance_generation", lambda **kw: order.append("ensure") or True)
    os_patch.setattr(runtime, "ServerContext", FakeContext)
    with pytest.raises(Stop):
        runtime.main()
    assert order == ["seal", "ensure", "context"]


def _calls(nodes) -> list[str]:
    names = []
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                names.append(ast.unparse(sub.func))
    return names


def _ordered(names: list[str], *wanted: str) -> list[int]:
    return [names.index(name) for name in wanted]


def test_structural_the_server_main_seals_then_bootstraps_before_its_context():
    """STRUCTURAL ONLY: the server's `if __name__ == "__main__":` block in server/server.py."""
    tree = ast.parse((REPO / "server" / "server.py").read_text())
    block = next(node for node in tree.body if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "__name__ == '__main__'")
    names = _calls(block.body)
    seal, ensure, context = _ordered(
        names, "seal_inherited_lock_fds", "ensure_instance_generation", "ServerContext.production")
    assert names[0] == "seal_inherited_lock_fds" and ensure == seal + 1 and ensure < context


def test_structural_the_entrypoint_bootstraps_after_the_locks_and_before_the_database():
    """STRUCTURAL ONLY: docker/entrypoint.sh, its lock step and its python heredoc."""
    text = (REPO / "docker" / "entrypoint.sh").read_text()
    locks = text.index('clarp_data_root_locks "$@"')
    match = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", text, re.S)
    assert match and match.start() > locks
    names = _calls(ast.parse(match.group(1)).body)
    directories, ensure, database = _ordered(
        names, "LAYOUT.create_container_directories", "ensure_instance_generation", "db.conn")
    assert directories < ensure < database


# ---- ownership: the only callers --------------------------------------------------------------

def _callers(name: str) -> set[str]:
    found = set()
    for path in [*(REPO / "server").rglob("*.py"), *(REPO / "bin").rglob("*"),
                 *(REPO / "docker").rglob("*")]:
        if path.is_file() and path.name != "host_generation.py":
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if re.search(rf"\b{name}\(", text):
                found.add(str(path.relative_to(REPO)))
    return found


def test_only_process_starts_bootstrap_only_the_restore_rotates_and_nothing_reads_yet():
    assert _callers("ensure_instance_generation") == {
        "docker/entrypoint.sh", "server/server.py", "server/runtime.py"}
    assert _callers("rotate_instance_generation") == {"server/lib/data_root_locks.py"}
    assert _callers("read_host_generation") == set()                 # no consumer in this slice
