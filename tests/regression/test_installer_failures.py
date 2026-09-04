"""Archive and failure-path regressions in the first-run installer."""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _archive_source(tmp_path: Path) -> Path:
    """Copy the checked-out product without git metadata, like GitHub's tarball."""
    source = tmp_path / "archive-source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"
        ),
    )
    built = source / "static/app/bundle.js"
    built.parent.mkdir(parents=True, exist_ok=True)
    built.write_text("// fixture bundle\n")
    return source


def _install_env(tmp_path: Path, fake_bin: Path) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "CLARP_PLATFORM_OVERRIDE": "linux",
            "CLARP_SKIP_HEALTHCHECK": "1",
            "PYTHON": sys.executable,
        }
    )
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "CLARP_CONFIG_DIR",
        "CLARP_SHARE_DIR",
        "CLARP_CACHE_DIR",
    ):
        env.pop(name, None)
    return env, home


def test_quick_start_removes_its_downloaded_source_tree(tmp_path):
    """``get.sh`` must not leak a full checkout after successful setup.

    Its EXIT trap is currently discarded by ``exec ./setup.sh``, leaving the
    temporary source behind. That leaked path is also persisted as source_repo.
    """
    archive = tmp_path / "fixture.tar.gz"
    setup = b"#!/bin/sh\nprintf '%s\\n' \"$PWD\" > \"$CLARP_TEST_MARKER\"\n"
    with tarfile.open(archive, "w:gz") as out:
        info = tarfile.TarInfo("clarp-fixture/setup.sh")
        info.mode = 0o755
        info.size = len(setup)
        out.addfile(info, io.BytesIO(setup))
        install = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo("clarp-fixture/install.sh")
        info.mode = 0o755
        info.size = len(install)
        out.addfile(info, io.BytesIO(install))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "curl",
        """
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then cp "$CLARP_TEST_ARCHIVE" "$2"; exit 0; fi
  shift
done
exit 2
""",
    )
    _executable(fake_bin / "uv", "exit 0\n")
    temp_root = tmp_path / "downloads"
    temp_root.mkdir()
    marker = tmp_path / "source-path"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "TMPDIR": str(temp_root),
            "CLARP_TEST_ARCHIVE": str(archive),
            "CLARP_TEST_MARKER": str(marker),
        }
    )

    subprocess.run([str(ROOT / "get.sh")], env=env, check=True, timeout=30)

    extracted_source = Path(marker.read_text().strip())
    assert not extracted_source.exists(), (
        "quick start left its downloaded source tree behind"
    )


def test_archive_install_uses_the_supplied_release_identity(tmp_path):
    """A tarball install must not report every clean release as unknown-dirty."""
    source = _archive_source(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "systemctl", "exit 0\n")
    env, home = _install_env(tmp_path, fake_bin)
    env.update(
        {
            "CLARP_SKIP_ENV": "1",
            "CLARP_TOOLCHAIN_MODE": "none",
            "CLARP_VERSION": "v9.9.9",
        }
    )

    subprocess.run([str(source / "install.sh")], env=env, check=True, timeout=60)

    version = (home / ".local/share/clarp/current/DEPLOYED_VERSION").read_text().strip()
    assert version == "v9.9.9"


def test_failure_before_activation_does_not_leave_a_rollback_candidate(tmp_path):
    """A half-prepared release must never appear in ``clarp-admin rollback``."""
    source = _archive_source(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "systemctl", "exit 0\n")
    failing_uv = _executable(fake_bin / "uv", "exit 19\n")
    env, home = _install_env(tmp_path, fake_bin)
    env.update(
        {
            "CLARP_TOOLCHAIN_MODE": "none",
            "CLARP_VERSION": "v9.9.9",
            "UV": str(failing_uv),
        }
    )

    result = subprocess.run([str(source / "install.sh")], env=env, timeout=60)

    assert result.returncode == 19
    releases = home / ".local/share/clarp/releases"
    assert list(releases.iterdir()) == []
    assert not (home / ".local/share/clarp/current").exists()
