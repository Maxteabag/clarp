"""What an agent's workspace is called, out loud.

The workspace pack is where "Clarp" comes from: the product name, the class
and module names, the branch the agent is on, the commit subjects it just
wrote. None of that is in any glossary, all of it is what the user says to the
agent, and all of it is derivable from the checkout.

Derivation costs a few git calls, so results are cached per working directory
and invalidated when HEAD moves. A directory that is not a repository still
yields its own name and the file stems under it.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .log import log_exception

GIT_TIMEOUT_SEC = 3.0
CACHE_TTL_SEC = 300.0
MAX_IDENTIFIERS = 400
MAX_BRANCHES = 30
MAX_COMMITS = 30
MAX_FILES_SCANNED = 3000

# Words a checkout is full of that nobody says to an agent.
_NOISE = frozenset({
    "src", "lib", "test", "tests", "spec", "index", "main", "utils", "util",
    "common", "core", "base", "types", "config", "readme", "license", "docs",
    "static", "public", "assets", "dist", "build", "node_modules", "vendor",
    "init", "setup", "package", "requirements", "makefile", "dockerfile",
})
_SKIP_DIRS = ("node_modules/", ".git/", "dist/", "build/", "vendor/", ".venv/")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class WorkspaceSources:
    project_name: str = ""
    identifiers: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    commit_subjects: tuple[str, ...] = ()
    head: str = ""


@dataclass
class _Entry:
    at: float
    head: str
    value: WorkspaceSources


_CACHE: dict[str, _Entry] = {}
_LOCK = threading.Lock()
_EMPTY = WorkspaceSources()


def _git(cwd: pathlib.Path, *args: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), text=True, capture_output=True,
            timeout=GIT_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _head(cwd: pathlib.Path) -> str:
    lines = _git(cwd, "rev-parse", "HEAD")
    return lines[0].strip() if lines else ""


def _tokens(stem: str) -> list[str]:
    """`ClipStreamBroker` -> [ClipStreamBroker, Clip, Stream, Broker]."""
    out: list[str] = []
    for chunk in _SPLIT_RE.split(stem):
        if not chunk:
            continue
        out.append(chunk)
        parts = [p for p in _CAMEL_RE.split(chunk) if p]
        if len(parts) > 1:
            out.extend(parts)
    return out


def _worth_saying(token: str) -> bool:
    low = token.lower()
    if len(token) < 4 or low in _NOISE:
        return False
    if token.isdigit():
        return False
    return True


def identifiers_from_paths(paths: list[str], *, limit: int = MAX_IDENTIFIERS
                           ) -> tuple[str, ...]:
    """Rank file-name tokens by how often they recur across the tree.

    A name used by many files (a module, a domain noun) is a name the user
    says; a one-off is probably not.
    """
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for raw in paths[:MAX_FILES_SCANNED]:
        if any(part in raw for part in _SKIP_DIRS):
            continue
        stem = pathlib.PurePosixPath(raw).stem
        for tok in _tokens(stem):
            if not _worth_saying(tok):
                continue
            key = tok.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, tok)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(display[k] for k, _ in ranked[:limit])


def _collect(cwd: pathlib.Path, head: str) -> WorkspaceSources:
    project = cwd.name
    if head:
        files = _git(cwd, "ls-files", "-z")
        if files:
            files = "\n".join(files).split("\0")
        branches = tuple(
            b.strip().lstrip("* ").replace("remotes/origin/", "")
            for b in _git(cwd, "branch", "-a", "--format=%(refname:short)",
                          "--sort=-committerdate")[:MAX_BRANCHES]
            if "HEAD" not in b)
        subjects = tuple(_git(cwd, "log", f"-{MAX_COMMITS}",
                              "--format=%s", "--no-merges"))
        top = _git(cwd, "rev-parse", "--show-toplevel")
        if top:
            project = pathlib.Path(top[0]).name
    else:
        try:
            files = [str(p.relative_to(cwd)) for p in list(cwd.rglob("*"))[:MAX_FILES_SCANNED]
                     if p.is_file()]
        except OSError:
            files = []
        branches, subjects = (), ()
    return WorkspaceSources(
        project_name=project,
        identifiers=identifiers_from_paths(files),
        branches=tuple(dict.fromkeys(branches)),
        commit_subjects=subjects,
        head=head,
    )


def sources_for(cwd: str | pathlib.Path | None, *,
                now: float | None = None) -> WorkspaceSources:
    """Workspace vocabulary for `cwd`, cached until HEAD moves or TTL expires.

    Never raises: biasing is an optimisation and the transcription must not
    wait on, or fail because of, a slow or missing repository.
    """
    if not cwd:
        return _EMPTY
    path = pathlib.Path(str(cwd))
    if not path.is_dir():
        return _EMPTY
    key = str(path.resolve())
    t = time.monotonic() if now is None else now
    try:
        with _LOCK:
            entry = _CACHE.get(key)
        head = _head(path)
        if entry is not None and entry.head == head and t - entry.at < CACHE_TTL_SEC:
            return entry.value
        value = _collect(path, head)
        with _LOCK:
            _CACHE[key] = _Entry(at=t, head=head, value=value)
        return value
    except Exception as e:  # noqa: BLE001 - never fail a turn over biasing
        log_exception("workspaceVocabFail", e, detail=key)
        return _EMPTY


def reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
