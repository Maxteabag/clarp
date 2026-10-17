"""Host generation primitives (C2 design r4 §12, §14): the bootstrap creator and the
read-only validator, on owned temporary roots with real file operations.

This slice is unwired. These tests prove the helpers alone: the validator writes
nothing, the bootstrap survives a race and refuses bad input without repairing it,
and deleting the token gives None until a fresh bootstrap mints a new one. They do
not claim anything about call sites, the ring refusal, or transactions.
"""
from __future__ import annotations

import errno
import os
import pathlib
import stat
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import host_generation as hg  # noqa: E402

BOOT = "0b7c5a52-6f4e-4d0b-9a57-2f0c2b1d9e11"
TOKEN = "5d2f8a3e-1c4b-4e7a-8f60-3b9d7c2e4a10"


@pytest.fixture
def boot_file(tmp_path):
    path = tmp_path / "boot_id"
    path.write_text(BOOT + "\n")
    return path


@pytest.fixture
def backups(tmp_path):
    path = tmp_path / "data" / "clarp" / "backups"
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


def _token(backups: pathlib.Path, text: str = TOKEN + "\n", mode: int = 0o600) -> pathlib.Path:
    path = backups / hg.TOKEN_NAME
    path.write_bytes(text.encode())
    path.chmod(mode)
    return path


def _snapshot(root: pathlib.Path) -> dict:
    """Every entry under root by lstat, plus root itself: names, inodes, modes, sizes, mtimes."""
    out = {}
    for path in [root, *sorted(root.rglob("*"))]:
        st = os.lstat(path)
        out[str(path)] = (st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_nlink)
    return out


def _link_identity(link: pathlib.Path, target: pathlib.Path) -> tuple:
    """The symlink's own text and inode, and the target's bytes and identity."""
    lst, tst = os.lstat(link), os.lstat(target)
    return (os.readlink(link), lst.st_ino, stat.S_ISLNK(lst.st_mode), target.read_bytes(),
            tst.st_ino, tst.st_mode, tst.st_size, tst.st_mtime_ns, tst.st_nlink)


def _read(backups, boot_file, **kw):
    return hg.read_host_generation(backups_dir=backups, boot_id_path=boot_file, **kw)


@pytest.fixture
def os_patch():
    """Patches on os undone at this fixture's teardown, before conftest's own teardown
    (which writes, syncs and unlinks) runs under them."""
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture
def no_writes(os_patch):
    """Returns arm(): called after the test's own setup and right before the read, it fails any
    mutating os call or log and returns the list of every os.open flag set used from then on."""
    def arm():
        opened = []

        def forbid(name):
            def boom(*args, **kwargs):
                raise AssertionError(f"validator called {name}")
            return boom

        for name in ("mkdir", "makedirs", "link", "unlink", "remove", "rename", "replace",
                     "write", "fsync", "fdatasync", "fchmod", "chmod", "symlink", "truncate",
                     "ftruncate", "rmdir", "mkfifo"):
            os_patch.setattr(os, name, forbid(f"os.{name}"))
        real_open = os.open

        def recording_open(path, flags, *args, **kwargs):
            opened.append(flags)
            return real_open(path, flags, *args, **kwargs)

        os_patch.setattr(os, "open", recording_open)
        for name in ("log", "log_exception"):
            os_patch.setattr(hg, name, forbid(name))
        return opened
    return arm


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


# T19: the validator is read-only.

def test_t19_valid_token_reads_without_any_write(backups, boot_file, no_writes):
    _token(backups)
    before = _snapshot(backups.parent)
    opened = no_writes()
    result = _read(backups, boot_file)
    assert result.generation == (BOOT, TOKEN)
    assert result.reason is None
    assert _snapshot(backups.parent) == before
    assert opened, "the validator opened nothing"
    assert all(flags & _WRITE_FLAGS == 0 for flags in opened)
    assert all(flags & os.O_NOFOLLOW for flags in opened)


def test_t19_token_without_trailing_newline_is_valid(backups, boot_file, no_writes):
    _token(backups, TOKEN)
    no_writes()
    assert _read(backups, boot_file).generation == (BOOT, TOKEN)


def test_t19_missing_directory_is_none_and_not_created(tmp_path, boot_file, no_writes):
    backups = tmp_path / "data" / "clarp" / "backups"
    no_writes()
    result = _read(backups, boot_file)
    assert result.generation is None and result.reason
    assert not (tmp_path / "data").exists()


def test_t19_missing_token_is_none_and_not_created(backups, boot_file, no_writes):
    before = _snapshot(backups)
    no_writes()
    result = _read(backups, boot_file)
    assert result.generation is None and result.reason
    assert _snapshot(backups) == before


@pytest.mark.parametrize("text", [
    "",
    "\n",
    "not-a-uuid\n",
    TOKEN.upper() + "\n",
    "{" + TOKEN + "}\n",
    "urn:uuid:" + TOKEN + "\n",
    TOKEN.replace("-", "") + "\n",
    TOKEN + "\n\n",
    " " + TOKEN + "\n",
    TOKEN + "\r\n",
    TOKEN + "\n" + "x" * 30,
])
def test_t19_non_canonical_token_is_none_and_untouched(backups, boot_file, no_writes, text):
    _token(backups, text)
    before = _snapshot(backups)
    no_writes()
    assert _read(backups, boot_file).generation is None
    assert _snapshot(backups) == before


def test_t19_oversize_token_is_none(backups, boot_file, no_writes):
    _token(backups, TOKEN + "\n" + " " * (hg.MAX_TOKEN_BYTES - len(TOKEN)))
    assert (backups / hg.TOKEN_NAME).stat().st_size > hg.MAX_TOKEN_BYTES
    no_writes()
    assert _read(backups, boot_file).generation is None


def test_t19_non_utf8_token_is_none(backups, boot_file, no_writes):
    (backups / hg.TOKEN_NAME).write_bytes(b"\xff" * 36)
    (backups / hg.TOKEN_NAME).chmod(0o600)
    no_writes()
    assert _read(backups, boot_file).generation is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o700, 0o660])
def test_t19_wrong_token_mode_is_none(backups, boot_file, no_writes, mode):
    _token(backups, mode=mode)
    no_writes()
    assert _read(backups, boot_file).generation is None


def test_t19_symlinked_token_is_none_and_target_untouched(backups, boot_file, tmp_path, no_writes):
    outside = tmp_path / "target-dir"
    outside.mkdir()
    target = outside / "elsewhere"
    target.write_text(TOKEN + "\n")
    target.chmod(0o600)
    link = backups / hg.TOKEN_NAME
    link.symlink_to(target)
    before = (_snapshot(backups.parent), _snapshot(outside), _link_identity(link, target))
    no_writes()
    assert _read(backups, boot_file).generation is None
    assert (_snapshot(backups.parent), _snapshot(outside), _link_identity(link, target)) == before


def test_t19_fifo_token_is_none_without_blocking(backups, boot_file):
    os.mkfifo(backups / hg.TOKEN_NAME, 0o600)
    assert _read(backups, boot_file).generation is None


def test_t19_directory_token_is_none(backups, boot_file, no_writes):
    (backups / hg.TOKEN_NAME).mkdir(mode=0o700)
    no_writes()
    assert _read(backups, boot_file).generation is None


def test_t19_token_owner_mismatch_is_none(backups, boot_file, no_writes):
    _token(backups)
    no_writes()
    assert _read(backups, boot_file, uid=os.geteuid() + 1).generation is None


def test_t19_symlinked_directory_is_none(tmp_path, backups, boot_file, no_writes):
    _token(backups)
    link = tmp_path / "linked-backups"
    link.symlink_to(backups, target_is_directory=True)
    no_writes()
    assert _read(link, boot_file).generation is None


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_t19_group_or_world_writable_directory_is_none(backups, boot_file, mode):
    _token(backups)
    backups.chmod(mode)
    before = _snapshot(backups)
    try:
        assert _read(backups, boot_file).generation is None
        assert _snapshot(backups) == before
    finally:
        backups.chmod(0o700)


def test_t19_file_where_directory_expected_is_none(tmp_path, boot_file, no_writes):
    path = tmp_path / "backups"
    path.write_text("")
    no_writes()
    assert _read(path, boot_file).generation is None


@pytest.mark.parametrize("text", ["", "garbage\n", BOOT.upper() + "\n", BOOT + "\n\n"])
def test_t19_invalid_boot_id_is_none(backups, tmp_path, no_writes, text):
    _token(backups)
    boot = tmp_path / "boot_bad"
    boot.write_text(text)
    no_writes()
    assert _read(backups, boot).generation is None


def test_t19_missing_boot_id_is_none(backups, tmp_path, no_writes):
    _token(backups)
    no_writes()
    assert _read(backups, tmp_path / "absent").generation is None


# T20: the bootstrap is the only creator; it survives a race and repairs nothing.

def test_t20_bootstrap_creates_directory_and_token(tmp_path, boot_file):
    backups = tmp_path / "data" / "clarp" / "backups"
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    st = os.lstat(backups)
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700
    token = backups / hg.TOKEN_NAME
    tst = os.lstat(token)
    assert stat.S_ISREG(tst.st_mode) and stat.S_IMODE(tst.st_mode) == 0o600
    assert tst.st_nlink == 1
    text = token.read_text()
    assert text.endswith("\n") and str(uuid.UUID(text[:-1])) == text[:-1]
    assert sorted(p.name for p in backups.iterdir()) == [hg.TOKEN_NAME]
    assert _read(backups, boot_file).generation == (BOOT, text[:-1])


def test_t20_bootstrap_modes_hold_under_restrictive_umask(tmp_path):
    backups = tmp_path / "data" / "clarp" / "backups"
    old = os.umask(0o277)
    try:
        assert hg.ensure_instance_generation(backups_dir=backups) is True
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.lstat(backups).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(backups / hg.TOKEN_NAME).st_mode) == 0o600


def test_t20_bootstrap_is_idempotent(backups):
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    token = backups / hg.TOKEN_NAME
    first = (token.read_text(), os.lstat(token).st_ino)
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    assert (token.read_text(), os.lstat(token).st_ino) == first


def test_t20_existing_valid_token_is_kept(backups):
    path = _token(backups)
    ino = os.lstat(path).st_ino
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    assert path.read_text() == TOKEN + "\n" and os.lstat(path).st_ino == ino


def test_t20_existing_0755_directory_is_accepted(backups):
    backups.chmod(0o755)
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    assert stat.S_IMODE(os.lstat(backups).st_mode) == 0o755


def _lose_the_race(os_patch, backups, text, mode=0o600):
    """Another bootstrap links its token between our temp write and our link."""
    real_link = os.link
    calls = []

    def racing_link(src, dst, *args, **kwargs):
        calls.append((src, dst))
        _token(backups, text, mode)
        return real_link(src, dst, *args, **kwargs)

    os_patch.setattr(os, "link", racing_link)
    return calls


def test_t20_race_loser_adopts_valid_winner(backups, boot_file, os_patch):
    calls = _lose_the_race(os_patch, backups, TOKEN + "\n")
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    assert calls, "the bootstrap reached its link"
    assert (backups / hg.TOKEN_NAME).read_text() == TOKEN + "\n"
    assert sorted(p.name for p in backups.iterdir()) == [hg.TOKEN_NAME]
    assert _read(backups, boot_file).generation == (BOOT, TOKEN)


def test_t20_race_loser_refuses_invalid_winner(backups, os_patch):
    _lose_the_race(os_patch, backups, "not-a-uuid\n")
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert (backups / hg.TOKEN_NAME).read_text() == "not-a-uuid\n"
    assert sorted(p.name for p in backups.iterdir()) == [hg.TOKEN_NAME]


def test_t20_race_loser_refuses_wrong_mode_winner(backups, os_patch):
    _lose_the_race(os_patch, backups, TOKEN + "\n", mode=0o644)
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert stat.S_IMODE(os.lstat(backups / hg.TOKEN_NAME).st_mode) == 0o644


@pytest.mark.parametrize("text,mode", [
    ("not-a-uuid\n", 0o600),
    (TOKEN.upper() + "\n", 0o600),
    (TOKEN + "\n", 0o644),
    ("", 0o600),
])
def test_t20_invalid_token_is_not_repaired(backups, text, mode):
    path = _token(backups, text, mode)
    before = _snapshot(backups)
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert _snapshot(backups) == before
    assert path.read_bytes() == text.encode()


def test_t20_symlinked_token_is_not_replaced(backups, tmp_path):
    """The owned data tree (backups and its siblings) and the outside target are compared in full;
    only tmp_path's root, which also holds the harness's per-test telemetry, is left out."""
    outside = tmp_path / "target-dir"
    outside.mkdir()
    target = outside / "elsewhere"
    target.write_text("x")
    link = backups / hg.TOKEN_NAME
    link.symlink_to(target)
    before = (_snapshot(backups.parent), _snapshot(outside), _link_identity(link, target))
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert os.readlink(link) == str(target) and target.read_bytes() == b"x"
    assert (_snapshot(backups.parent), _snapshot(outside), _link_identity(link, target)) == before


def test_t20_bad_directory_owner_is_refused_and_logs_the_expected_uid(backups, os_patch):
    logged = []
    os_patch.setattr(hg, "log", lambda event, detail="": logged.append((event, detail)))
    expected = os.geteuid() + 1
    assert hg.ensure_instance_generation(backups_dir=backups, uid=expected) is False
    assert list(backups.iterdir()) == []
    assert logged == [("hostGenerationInvalid",
                       f"backups owner uid {os.geteuid()}, expected uid {expected}")]


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_t20_writable_directory_is_refused_not_fixed(backups, mode):
    backups.chmod(mode)
    try:
        assert hg.ensure_instance_generation(backups_dir=backups) is False
        assert stat.S_IMODE(os.lstat(backups).st_mode) == mode
        assert list(backups.iterdir()) == []
    finally:
        backups.chmod(0o700)


def test_t20_symlinked_directory_is_refused(tmp_path, backups):
    link = tmp_path / "linked-backups"
    link.symlink_to(backups, target_is_directory=True)
    assert hg.ensure_instance_generation(backups_dir=link) is False
    assert list(backups.iterdir()) == []


def test_t20_file_where_directory_expected_is_refused(tmp_path):
    path = tmp_path / "backups"
    path.write_text("keep")
    assert hg.ensure_instance_generation(backups_dir=path) is False
    assert path.read_text() == "keep"


def test_t20_os_error_mid_bootstrap_fails_closed_and_cleans_up(backups, os_patch):
    logged = []
    os_patch.setattr(hg, "log_exception", lambda event, exc, detail="": logged.append(
        (event, type(exc).__name__)))
    real_fsync = os.fsync

    def failing_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        return real_fsync(fd)

    os_patch.setattr(os, "fsync", failing_fsync)
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert list(backups.iterdir()) == []
    assert logged and logged[0][1] == "OSError"


def test_t20_link_unsupported_fails_closed_and_cleans_up(backups, os_patch):
    logged = []
    os_patch.setattr(hg, "log_exception", lambda event, exc, detail="": logged.append(event))

    def no_link(*args, **kwargs):
        raise OSError(errno.EPERM, "injected: hard links unsupported")

    os_patch.setattr(os, "link", no_link)
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert list(backups.iterdir()) == []
    assert logged


def test_t20_failed_temp_cleanup_is_logged_not_raised(backups, os_patch):
    logged = []
    os_patch.setattr(hg, "log_exception", lambda event, exc, detail="": logged.append(event))

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "injected")

    os_patch.setattr(os, "link", fail)
    os_patch.setattr(os, "unlink", fail)
    assert hg.ensure_instance_generation(backups_dir=backups) is False
    assert len(logged) >= 2, "both the failure and the failed cleanup leave a trail"


# T21: deletion gives None; only a fresh bootstrap mints a new identity.

def test_t21_deleted_token_reads_none_until_fresh_bootstrap(backups, boot_file):
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    first = _read(backups, boot_file).generation
    assert first is not None
    (backups / hg.TOKEN_NAME).unlink()
    gone = _read(backups, boot_file)
    assert gone.generation is None and gone.reason
    assert not (backups / hg.TOKEN_NAME).exists(), "reading never recreates the token"
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    second = _read(backups, boot_file).generation
    assert second is not None and second[0] == BOOT and second[1] != first[1]


def test_t21_boot_change_changes_generation_not_token(backups, tmp_path, boot_file):
    assert hg.ensure_instance_generation(backups_dir=backups) is True
    before = _read(backups, boot_file).generation
    other = tmp_path / "boot_other"
    other.write_text(str(uuid.uuid4()) + "\n")
    after = _read(backups, other).generation
    assert after[1] == before[1] and after[0] != before[0]
