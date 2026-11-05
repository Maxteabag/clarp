"""Moved out of server/lib/dreaming.py by tools/refactor/split_apply.py (split 'dreaming', recipe tools/refactor/splits/s9-dreaming.toml).
dreaming.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import uuid
from typing import Any
from . import db
from .log import log, log_exception
from .dreaming_settings import _RUN_ID_RE
from .dreaming_run_lookup import get_run


def _dream_scratch_cwd(agent: dict, prompt: str) -> pathlib.Path:
    """Return a disposable worktree for isolated dream investigation."""
    source = _existing_cwd(agent.get("cwd"))
    run_id = _run_id_from_prompt(prompt) or f"dream_{uuid.uuid4().hex}"
    from .launch_paths import validate_workspace_path, workspace_root
    workspace = workspace_root()
    root = ((workspace / ".clarp/dream-worktrees") if workspace is not None
            else pathlib.Path("/var/tmp/clarp-dream-worktrees"))
    target = validate_workspace_path(root / run_id)
    if target.is_dir():
        return target
    git = shutil.which("git")
    if not git:
        return _dream_fallback_cwd(root, run_id)
    try:
        root.mkdir(parents=True, exist_ok=True)
        top = subprocess.run(
            [git, "-C", str(source), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not top:
            return _dream_fallback_cwd(root, run_id)
        subprocess.run(
            [git, "-C", top, "fetch", "origin", "main", "--quiet"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [git, "-C", top, "worktree", "add", "--detach",
             str(target), "origin/main"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        log("dreamingScratchWorktree", f"run={run_id} path={target}")
        return target
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingScratchWorktreeFail", e, detail=str(source))
        return _dream_fallback_cwd(root, run_id)


def harvest_worktree(run_id: str) -> str:
    """Commit whatever a dream built, then take its worktree away.

    A dream worktree is disposable, but what it contains often is not: a run
    at `worktree` altitude writes real code and real tests there, and the only
    record of it was prose in a digest pointing at a directory nothing owned.

    Committing before removing is what makes removal safe. Nothing dirty is
    ever discarded — the work becomes a branch that survives the cleanup and
    can be diffed, cherry-picked or deleted later on its own merits. Returns
    the branch name, or "" when there was nothing to keep.
    """
    run = get_run(run_id) or {}
    cwd = _dream_worktree_path(run_id)
    if cwd is None:
        return ""
    git = shutil.which("git")
    if not git:
        log("dreamHarvestNoGit", f"run={run_id}")
        return ""

    def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [git, "-C", str(cwd), *args], check=check, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    branch = ""
    try:
        dirty = bool(_git("status", "--porcelain").stdout.strip())
        if dirty:
            strategy = str(run.get("seed_strategy") or "dream")
            local_date = str(run.get("local_date") or "")
            branch = f"dream/{local_date}-{strategy}-{run_id[6:14]}"
            _git("checkout", "-b", branch)
            _git("add", "-A")
            _git(
                "-c", "user.name=Clarp Dreaming",
                "-c", "user.email=dreaming@clarp.invalid",
                "commit", "-m",
                f"dream({strategy}): work built during {local_date}\n\n"
                f"Committed by the dreaming harvester so the worktree could be\n"
                f"removed without discarding it. Unreviewed: written by one\n"
                f"overnight run, verified only by whatever that run chose to\n"
                f"execute.\n\nrun_id={run_id}",
            )
            db.conn().execute(
                "UPDATE dream_runs SET artifact_branch = ?, updated_at = ?"
                " WHERE run_id = ?",
                (branch, db.now_ms(), run_id))
            log("dreamHarvestCommitted", f"run={run_id} branch={branch}")
        else:
            log("dreamHarvestClean", f"run={run_id}")
    except (subprocess.SubprocessError, OSError) as e:
        # A harvest that fails must leave the worktree alone. Losing the disk
        # is a smaller problem than losing the only copy of the work.
        log_exception("dreamHarvestFail", e, detail=run_id)
        return branch

    try:
        source = _existing_cwd(run.get("cwd")) if run.get("cwd") else None
        top = _repo_toplevel(git, source or cwd)
        if top:
            subprocess.run(
                [git, "-C", top, "worktree", "remove", str(cwd)],
                check=True, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120)
            subprocess.run(
                [git, "-C", top, "worktree", "prune"], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60)
            log("dreamHarvestRemoved", f"run={run_id} path={cwd}")
    except (subprocess.SubprocessError, OSError) as e:
        log_exception("dreamHarvestRemoveFail", e, detail=run_id)
    return branch


def _dream_worktree_path(run_id: str) -> pathlib.Path | None:
    """The disposable worktree for one run, if it is still on disk."""
    from .launch_paths import workspace_root
    workspace = workspace_root()
    root = ((workspace / ".clarp/dream-worktrees") if workspace is not None
            else pathlib.Path("/var/tmp/clarp-dream-worktrees"))
    target = root / run_id
    return target if target.is_dir() else None


def _repo_toplevel(git: str, path: pathlib.Path) -> str:
    try:
        return subprocess.run(
            [git, "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=15).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _dream_fallback_cwd(root: pathlib.Path, run_id: str) -> pathlib.Path:
    from .launch_paths import validate_workspace_path, workspace_root
    path = validate_workspace_path(root / f"{run_id}-scratch")
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return workspace_root() or pathlib.Path("/tmp")


def _run_id_from_prompt(prompt: str) -> str:
    match = _RUN_ID_RE.search(prompt or "")
    return match.group(1) if match else ""


def _existing_cwd(raw: Any) -> pathlib.Path:
    from .launch_paths import existing_workspace_path
    return existing_workspace_path(raw)
