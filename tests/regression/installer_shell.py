"""Read the real installer shell so regression tests cannot drift from it.

Three of the six reported issues were shell-side: a `cp` of a file that is not
in the archive, a remote resolved differently in bash than in Python, and a
`git` call in a directory that is not a repository. Testing bash by copying its
logic into Python would pin the copy, not the installer, so these helpers
extract the real lines out of `install.sh` / `setup.sh` and run them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"
SETUP_SH = ROOT / "setup.sh"


def source_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def logical_lines(path: Path) -> list[str]:
    """Shell lines with backslash continuations joined into one entry."""
    joined: list[str] = []
    buffer = ""
    for line in source_lines(path):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1].rstrip() + " "
            continue
        joined.append((buffer + stripped.strip()).strip())
        buffer = ""
    if buffer:
        joined.append(buffer.strip())
    return joined


def line_number(path: Path, needle: str) -> int:
    """1-based line number of the first line containing `needle`."""
    for index, line in enumerate(source_lines(path), start=1):
        if needle in line:
            return index
    raise AssertionError(f"{path.name} no longer contains {needle!r}")


def region(path: Path, start: str, end: str, *, include_end: bool = False) -> str:
    """The verbatim block from the line holding `start` to the line holding `end`."""
    lines = source_lines(path)
    first = next(i for i, line in enumerate(lines) if start in line)
    last = next(i for i, line in enumerate(lines[first + 1:], start=first + 1)
                if end in line)
    return "\n".join(lines[first:last + 1 if include_end else last])


def bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = {"PATH": os.environ.get("PATH", "")}
    environment.update(env or {})
    return subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True,
        env=environment, timeout=30)


# ---- staging -------------------------------------------------------------

_REPO_SOURCE = re.compile(r'"\$REPO_DIR/([^"]+)"')
_STAGE_DEST = re.compile(r'"\$STAGE/([^"]*)"')


def guarded_paths() -> set[str]:
    """Repo-relative paths install.sh explicitly tests for before using."""
    guarded: set[str] = set()
    for line in logical_lines(INSTALL_SH):
        if not re.search(r"\[\[\s+-[fdel]\s+", line):
            continue
        guarded.update(_REPO_SOURCE.findall(line))
    return guarded


def staged_sources() -> tuple[set[str], set[str]]:
    """(unconditionally copied, guarded) repo-relative paths in the cp list."""
    guarded = guarded_paths()
    copied: set[str] = set()
    for line in logical_lines(INSTALL_SH):
        if not re.match(r"cp\b", line):
            continue
        copied.update(_REPO_SOURCE.findall(line))
    return copied - guarded, copied & guarded


def staged_release_entries() -> tuple[set[str], set[str]]:
    """(unconditional, guarded) top-level names created inside the staged release."""
    guarded_sources = guarded_paths()
    unconditional: set[str] = set()
    guarded: set[str] = set()
    for line in logical_lines(INSTALL_SH):
        if not re.match(r"(cp|mkdir|printf)\b", line):
            continue
        destinations = {name.strip("/").split("/")[0]
                        for name in _STAGE_DEST.findall(line) if name.strip("/")}
        if not destinations:
            continue
        sources = set(_REPO_SOURCE.findall(line))
        target = guarded if sources & guarded_sources else unconditional
        target.update(destinations)
    return unconditional, guarded - unconditional


def chmod_targets_under_current() -> set[str]:
    """Top-level release entries install.sh chmods through `$SHARE/current`."""
    found: set[str] = set()
    for line in logical_lines(INSTALL_SH):
        if not re.match(r"chmod\b", line):
            continue
        for match in re.findall(r'"\$SHARE/current/([^"]+)"', line):
            found.add(match.strip("/").split("/")[0])
    return found


def backed_up_paths() -> set[str]:
    """Literal arguments passed to install.sh's `backup_external`."""
    return {match for line in logical_lines(INSTALL_SH)
            for match in re.findall(r'backup_external\s+"([^"]+)"', line)}


def installed_wrappers() -> set[str]:
    """Helper names install.sh writes into `$BIN` via write_python_wrapper."""
    return {line.split()[1] for line in logical_lines(INSTALL_SH)
            if re.match(r"write_python_wrapper\s+\S+\s+\S+", line)}


# ---- runnable snippets ---------------------------------------------------

def deployed_version(repo_dir: Path) -> str:
    """Run install.sh's DEPLOYED_VERSION block against `repo_dir`."""
    snippet = region(
        INSTALL_SH, 'DEPLOYED_VERSION="$(git -C "$REPO_DIR" rev-parse',
        'printf \'%s\\n\' "$DEPLOYED_VERSION" > "$STAGE/DEPLOYED_VERSION"')
    script = f'{snippet}\nprintf "%s" "$DEPLOYED_VERSION"\n'
    result = bash(script, {"REPO_DIR": str(repo_dir)})
    assert result.returncode == 0, result.stderr
    return result.stdout


def recorded_source_remote(origin: str) -> str:
    """Run install.sh's SOURCE_REMOTE block for a given `git remote get-url`."""
    snippet = region(
        INSTALL_SH, 'SOURCE_REMOTE="${CLARP_SOURCE_REMOTE:-',
        'printf \'%s\\n\' "$SOURCE_REMOTE" > "$STAGE/SOURCE_REMOTE"')
    script = f'{snippet}\nprintf "%s" "$SOURCE_REMOTE"\n'
    result = bash(script, {
        "REPO_DIR": "/nonexistent", "CLARP_SOURCE_REMOTE": origin})
    assert result.returncode == 0, result.stderr
    return result.stdout


def _platform_shims(directory: Path, uname: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "uname").write_text(f'#!/bin/sh\nprintf "%s\\n" "{uname}"\n')
    (directory / "sw_vers").write_text('#!/bin/sh\nprintf "15.0\\n"\n')
    for name in ("uname", "sw_vers"):
        (directory / name).chmod(0o755)
    return directory


def platform_resolution(
    script: Path, *, host: str, override: str, home: Path, shims: Path,
    variables: tuple[str, ...] = ("PLATFORM", "SHARE"),
) -> dict[str, str]:
    """Run the platform/dir block of install.sh or setup.sh and read it back."""
    if script == INSTALL_SH:
        snippet = region(
            INSTALL_SH, 'HOST_PLATFORM="$(uname -s)"', 'RELEASES="$SHARE/releases"')
    else:
        snippet = region(
            SETUP_SH, 'HOST_PLATFORM="$(uname -s)"',
            'export CLARP_SHARE_DIR="$SHARE"')
    _platform_shims(shims, host)
    reads = "\n".join(f'printf "{name}=%s\\n" "${{{name}}}"' for name in variables)
    result = bash(f"{snippet}\n{reads}\n", {
        "PATH": f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "CLARP_PLATFORM_OVERRIDE": override,
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    })
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def tracked_files() -> set[str]:
    """Every path a GitHub source archive of this ref would contain."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        text=True, capture_output=True, check=True)
    return {name for name in result.stdout.split("\0") if name}


def have_git() -> bool:
    return shutil.which("git") is not None
