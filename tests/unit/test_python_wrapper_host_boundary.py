"""Execute the installer's wrapper writer without executing the installer.

The historical function pins the pre-guard bytes and interpreter environment;
all generated files, stand-in interpreters and foreign tools live in tmp_path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install.sh"
BASELINE = ROOT / "tests/fixtures/python-wrapper-main.sh"
WRAPPERS = re.findall(r"^write_python_wrapper ([\w-]+) (\S+)$", INSTALLER.read_text(), re.M)
SHELLS = [shutil.which(name) for name in ("sh", "bash", "dash") if shutil.which(name)]
ARGS = ["two words", "a'b", 'say "hi"', "", "--literal=$()"]


def _writer(source):
    start = source.index("write_python_wrapper() {\n")
    return source[start:source.index("\n}\n", start) + 3]


def _generate(source, directory, layout, name, relative):
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run([shutil.which("bash"), "-c", _writer(source) + '\nwrite_python_wrapper "$@"',
                    "scratch-writer", name, relative], check=True, capture_output=True,
                   env={"PATH": os.defpath, "PYTHON": sys.executable, "BIN": str(directory),
                        "SHARE": str(layout.share), "CFG_DIR": str(layout.config),
                        "CACHE_DIR": str(layout.cache), "RELEASE": str(layout.release)})
    return directory / name


@pytest.fixture(params=SHELLS, ids=lambda path: Path(path).name)
def shell(request):
    return request.param


@pytest.fixture(params=WRAPPERS, ids=lambda item: item[0])
def wrapper(request, tmp_path):
    name, relative = request.param
    root = tmp_path / "wrapper paths with spaces"
    layout = SimpleNamespace(root=root, share=root / "installed share", config=root / "config",
                             cache=root / "cache", release=root / "fallback release",
                             called=root / "python-called", name=name, relative=relative)
    interpreter = root / "stand-in interpreter"
    root.mkdir()
    interpreter.write_text(f"#!{sys.executable}\n"
                           "import json, os, pathlib, sys\n"
                           f"pathlib.Path({str(layout.called)!r}).touch()\n"
                           "print(json.dumps({'argv':sys.argv[1:], 'env':dict(os.environ)}, sort_keys=True))\n"
                           "sys.exit(23)\n")
    interpreter.chmod(0o700)
    for release in (layout.share / "current", layout.release):
        (release / relative).parent.mkdir(parents=True, exist_ok=True)
        (release / relative).touch()
        (release / "SERVICE_PYTHON").write_text(str(interpreter) + "\n")
    layout.path = _generate(INSTALLER.read_text(), root / "tools", layout, name, relative)
    return layout


def _run(shell, path, extra):
    return subprocess.run([shutil.which("env"), "-i", "PATH=" + os.defpath,
                           *[key + "=" + str(value) for key, value in extra.items()],
                           shell, str(path), *ARGS], capture_output=True, timeout=3)


def test_original_body_and_managed_marker_are_unchanged(wrapper):
    baseline = _generate(BASELINE.read_text(), wrapper.root / "baseline", wrapper,
                         wrapper.name, wrapper.relative).read_bytes()
    actual = wrapper.path.read_bytes()
    header = b"#!/bin/sh\n# managed-by-clarp\n"
    assert baseline.startswith(header) and actual.startswith(header)
    # Every old byte from the first export onward must remain unchanged.
    body = baseline[len(header):]
    assert actual[actual.index(b"export CLARP_SHARE_DIR="):] == body


@pytest.mark.parametrize("address", [None, ""])
@pytest.mark.parametrize("active", [True, False], ids=["current", "fallback"])
def test_local_turn_matches_main_execution(wrapper, shell, address, active):
    baseline = _generate(BASELINE.read_text(), wrapper.root / "baseline", wrapper,
                         wrapper.name, wrapper.relative)
    if not active:
        (wrapper.share / "current/SERVICE_PYTHON").unlink()
    env = {"CLARP_SHARE_DIR": "inherited share", "CLARP_CONFIG_DIR": "inherited config",
           "CLAUDE_PWA_DB": "inherited db", "UNCHANGED": 'a "quoted" value'}
    if address is not None:
        env["CLARP_HOST_ADDR"] = address
    before = _run(shell, baseline, env)
    after = _run(shell, wrapper.path, env)
    assert before.returncode == after.returncode == 23
    assert before.stdout == after.stdout
    assert before.stderr == after.stderr == b""
    delivered = json.loads(after.stdout)
    release = wrapper.share / "current" if active else wrapper.release
    assert delivered["argv"] == [str(release / wrapper.relative), *ARGS]
    assert delivered["env"]["CLARP_SHARE_DIR"] == str(wrapper.share)
    assert delivered["env"]["CLARP_CODE_ROOT"] == str(release)


def test_foreign_turn_executes_its_own_named_tool(wrapper, shell):
    foreign = wrapper.root / "foreign share"
    tool = foreign / "tools" / wrapper.name
    tool.parent.mkdir(parents=True)
    tool.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    "print(json.dumps({'argv':sys.argv[1:], 'env':dict(os.environ)}, sort_keys=True))\n"
                    "sys.exit(37)\n")
    tool.chmod(0o700)
    env = {"CLARP_HOST_ADDR": "http://foreign.invalid", "CLARP_SHARE_DIR": str(foreign),
           "CLARP_CONFIG_DIR": "foreign config", "CLAUDE_PWA_DB": "foreign db"}
    result = _run(shell, wrapper.path, env)
    assert result.returncode == 37
    assert result.stderr == b""
    delivered = json.loads(result.stdout)
    assert delivered["argv"] == ARGS
    for key, value in env.items():
        assert delivered["env"][key] == value
    assert not wrapper.called.exists()


@pytest.mark.parametrize("target", ["missing", "nonexecutable", "directory", "self", "symlink-self",
                                   "hardlink-self", "share-unset", "share-empty"])
def test_foreign_turn_is_refused_before_python_exports(wrapper, shell, target):
    foreign = wrapper.root / "foreign share"
    tool = foreign / "tools" / wrapper.name
    tool.parent.mkdir(parents=True)
    env = {"CLARP_HOST_ADDR": "http://foreign.invalid", "CLARP_SHARE_DIR": str(foreign)}
    if target == "nonexecutable":
        tool.write_text("not executable")
        tool.chmod(0o600)
    elif target == "directory":
        tool.mkdir(mode=0o700)
    elif target == "self":
        env["CLARP_SHARE_DIR"] = str(wrapper.root)
    elif target == "symlink-self":
        tool.symlink_to(wrapper.path)
    elif target == "hardlink-self":
        tool.hardlink_to(wrapper.path)
    elif target == "share-unset":
        del env["CLARP_SHARE_DIR"]
    elif target == "share-empty":
        env["CLARP_SHARE_DIR"] = ""
    result = _run(shell, wrapper.path, env)
    assert result.returncode == 69
    assert result.stdout == b""
    assert result.stderr == f"{wrapper.name}: this turn belongs to another Clarp Host\n".encode()
    assert not wrapper.called.exists()
