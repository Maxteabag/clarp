"""An agent forks itself into helper agents that start from its conversation.

``POST /agents/{session}/fork`` creates one helper per child (role helper,
parent = the forking agent) and sends each its own task from the parent, so
the child reports back like any helper. Each child begins where the parent
is now rather than from a blank prompt:

* **native**: the parent's backend copies its conversation into a new native
  session the child resumes (Claude Code: the session jsonl under a new id in
  the child's project dir; Codex: the app-server's ``thread/fork``);
* **seed**: when the backend has no native fork, or the child runs on another
  backend, the child starts a fresh session whose first message carries the
  most recent part of the parent's transcript (at most ``SEED_MAX_CHARS``)
  ahead of its task.

A child may get its own git worktree (``worktree: {repo, branch}``); the
worktree is made before the agent, so a failed ``git worktree add`` creates
nothing. Children are independent: one failing is reported in its row and
does not stop its siblings. A request that is malformed creates nothing.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from . import agents as agents_db
from . import backends, identity
from .backend.base import Unsupported
from .log import log, log_exception
from .trace import new_trace_id

MAX_CHILDREN = 16
SEED_MAX_CHARS = 60_000
_TURN_MAX_CHARS = 4_000


class ForkError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = ""):
        self.status = status
        self.code = code
        super().__init__(message or code)

    def response(self) -> dict:
        return {"error": self.code, "message": str(self)}


@dataclass(frozen=True)
class ChildSpec:
    name: str
    task: str
    index: int
    total: int
    cwd: str = ""
    repo: str = ""
    branch: str = ""
    base: str = ""
    worktree_path: str = ""
    backend: str = ""
    model: str = ""
    effort: str = ""


@dataclass
class _Parent:
    agent_id: str
    session: str
    backend: str
    cwd: str
    conversation: str
    _seed: str | None = field(default=None, repr=False)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse(data: dict) -> list[ChildSpec]:
    """The children a fork request asks for; ``ForkError`` when malformed.

    Either ``children: [{name, task, cwd?, worktree?: {repo, branch, base?,
    path?}, backend?, model?, effort?}]`` or ``count`` copies of one ``task``
    named ``<name>-1`` .. ``<name>-N`` (``name`` defaults to ``fork``).
    ``backend``/``model``/``effort`` at the top apply to every child that
    does not name its own.
    """
    if not isinstance(data, dict):
        raise ForkError(400, "bad_json")
    shared = {key: _text(data.get(key)) for key in ("backend", "model", "effort")}
    raw = data.get("children")
    if raw is None and data.get("count") is not None:
        try:
            count = int(data.get("count"))
        except (TypeError, ValueError):
            raise ForkError(400, "children_required", "count must be a number") from None
        if count > MAX_CHILDREN:
            raise ForkError(400, "too_many_children", f"At most {MAX_CHILDREN} children")
        base = _text(data.get("name")) or "fork"
        raw = [{"name": f"{base}-{i}", "task": data.get("task"),
                "cwd": data.get("cwd"), "worktree": data.get("worktree")}
               for i in range(1, count + 1)] if count > 0 else []
        if raw and isinstance(data.get("worktree"), dict):
            # One branch per sibling: two worktrees cannot share a branch.
            for item in raw:
                tree = dict(item["worktree"])
                if _text(tree.get("branch")):
                    tree["branch"] = f"{_text(tree['branch'])}-{item['name'].rsplit('-', 1)[1]}"
                tree.pop("path", None)
                item["worktree"] = tree
    if not isinstance(raw, list) or not raw:
        if raw is not None and not isinstance(raw, list):
            raise ForkError(400, "children_required", "children must be a list")
        if data.get("count") is not None and not _text(data.get("task")):
            raise ForkError(400, "task_required", "Every child needs a task")
        raise ForkError(400, "children_required", "Give children or a count")
    if len(raw) > MAX_CHILDREN:
        raise ForkError(400, "too_many_children", f"At most {MAX_CHILDREN} children")
    specs: list[ChildSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ForkError(400, "children_required", "Each child must be an object")
        name, task = _text(item.get("name")), _text(item.get("task"))
        if not task:
            raise ForkError(400, "task_required", "Every child needs a task")
        if not name:
            raise ForkError(400, "name_required", "Every child needs a name")
        if len(name) > 64:
            raise ForkError(400, "name_too_long", f"Child name too long: {name[:64]}…")
        if name.casefold() in seen:
            raise ForkError(400, "duplicate_name", f"Two children are named {name}")
        seen.add(name.casefold())
        tree = item.get("worktree")
        repo = branch = base = path = ""
        if tree is not None:
            if not isinstance(tree, dict):
                raise ForkError(400, "worktree_invalid", "worktree must be {repo, branch}")
            repo, branch = _text(tree.get("repo")), _text(tree.get("branch"))
            base, path = _text(tree.get("base")), _text(tree.get("path"))
            if not repo:
                raise ForkError(400, "worktree_repo_required", f"{name}: worktree needs a repo")
            if not branch:
                raise ForkError(400, "worktree_branch_required",
                                f"{name}: worktree needs a branch")
        specs.append(ChildSpec(
            name=name, task=task, index=index, total=len(raw),
            cwd=_text(item.get("cwd")), repo=repo, branch=branch, base=base,
            worktree_path=path,
            **{key: _text(item.get(key)) or shared[key]
               for key in ("backend", "model", "effort")}))
    return specs


def _parent(session: str) -> _Parent:
    row = identity.lookup(session)
    if not row:
        raise ForkError(404, "agent_not_found", f"No live agent: {session}")
    row = agents_db.get_by_agent_id(row["agent_id"]) or row
    conversation = identity.backend_session(row)
    if not conversation:
        raise ForkError(409, "no_conversation",
                        f"{row['session']} has no conversation to fork yet")
    return _Parent(agent_id=row["agent_id"], session=row["session"],
                   backend=backends.normalize(row.get("backend")),
                   cwd=str(row.get("cwd") or ""), conversation=conversation)


# ---- worktrees ---------------------------------------------------------

def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=120)


def default_worktree_path(repo: pathlib.Path, branch: str) -> pathlib.Path:
    """``<parent>/<repo>-worktrees/<branch with / as ->``, beside the repo."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.") or "fork"
    return repo.parent / f"{repo.name}-worktrees" / slug


def make_worktree(spec: ChildSpec) -> str:
    """Create (or reuse) the child's worktree; return its path."""
    repo = pathlib.Path(spec.repo).expanduser()
    top = _git(repo, "rev-parse", "--show-toplevel") if repo.is_dir() else None
    if top is None or top.returncode != 0:
        raise ForkError(400, "worktree_failed", f"{spec.repo} is not a git repository")
    repo = pathlib.Path(top.stdout.strip())
    path = (pathlib.Path(spec.worktree_path).expanduser() if spec.worktree_path
            else default_worktree_path(repo, spec.branch))
    if path.exists():
        current = _git(path, "branch", "--show-current")
        if current.returncode == 0 and current.stdout.strip() == spec.branch:
            return str(path)
        raise ForkError(409, "worktree_failed",
                        f"{path} exists and is not a worktree on {spec.branch}")
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{spec.branch}")
    args = (["worktree", "add", str(path), spec.branch] if exists.returncode == 0
            else ["worktree", "add", "-b", spec.branch, str(path), spec.base or "HEAD"])
    done = _git(repo, *args)
    if done.returncode != 0:
        detail = (done.stderr.strip().splitlines() or ["git worktree add failed"])[-1]
        raise ForkError(409, "worktree_failed", detail)
    return str(path)


# ---- the conversation the child starts from ----------------------------

def _seed(parent: _Parent) -> str:
    """The most recent part of the parent's transcript as plain text."""
    if parent._seed is not None:
        return parent._seed
    lines: list[str] = []
    try:
        backend = backends.get(parent.backend)
        path = backend.find_transcript(parent.conversation, cwd=parent.cwd)
        turns = backend.parse_transcript(path) if path is not None else []
    except Exception as exc:  # noqa: BLE001 - a seed is best effort
        log_exception("forkSeedReadFail", exc, detail=parent.session)
        turns = []
    for turn in turns:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = str(turn.get("text") or "").strip()
        if len(text) > _TURN_MAX_CHARS:
            text = text[:_TURN_MAX_CHARS] + " […]"
        tools = [f"  → {tool.get('name') or 'tool'}"
                 + (f": {str(tool.get('summary'))[:160]}" if tool.get("summary") else "")
                 for tool in turn.get("tools") or [] if isinstance(tool, dict)]
        if text or tools:
            lines.append("\n".join([f"{role}: {text}" if text else f"{role}:", *tools]))
    kept: list[str] = []
    size = 0
    for block in reversed(lines):
        if size + len(block) + 2 > SEED_MAX_CHARS:
            break
        kept.append(block)
        size += len(block) + 2
    parent._seed = "\n\n".join(reversed(kept))
    return parent._seed


def _native_fork(parent: _Parent, backend: str, cwd: str) -> str:
    """The child's forked native conversation, or '' to seed instead."""
    if backend != parent.backend or not backends.capabilities(backend).supports_fork:
        return ""
    try:
        return backends.get(backend).fork_conversation(
            parent.conversation, source_cwd=parent.cwd, cwd=cwd)
    except (Unsupported, FileNotFoundError, OSError, RuntimeError, TimeoutError) as exc:
        log_exception("forkNativeFail", exc, detail=f"{parent.session} -> {cwd}")
        return ""


def message(*, parent: str, helper: str, spec: ChildSpec, cwd: str,
            seed: str | None) -> str:
    """The child's first message: what it is, the seed if any, its task and
    how to report back."""
    where = (f"your own git worktree {cwd} on branch {spec.branch}" if spec.branch
             else cwd)
    siblings = (f" You are child {spec.index} of {spec.total} forked together; "
                "the others work on their own tasks." if spec.total > 1 else "")
    if seed is None:
        intro = (f"You are the Clarp helper agent `{helper}` ({spec.name}), a fork of "
                 f"`{parent}`. The conversation above is {parent}'s, up to the moment "
                 "it forked you; any tool call it was making then was the fork itself.")
    else:
        intro = (f"You are the Clarp helper agent `{helper}` ({spec.name}), a fork of "
                 f"`{parent}`. Below is the most recent part of {parent}'s conversation, "
                 "up to the moment it forked you. Treat it as your own memory.")
    parts = [intro + f"{siblings} {parent} carries on with its own work: do not continue "
             f"it, only the task below. Work in {where} and stay there."]
    if seed is not None:
        parts.append(f"--- {parent}'s conversation so far ---\n"
                     f"{seed or '(no transcript was readable)'}\n--- end ---")
    parts.append(f"Your task:\n{spec.task}")
    parts.append(
        "When you are finished (or blocked), send your final report to your parent "
        "in one message:\n"
        f"  clarp-admin prompt --to {parent} --from {helper} --text \"<report>\"\n"
        "That message marks you reported. Do not message your parent for routine progress.")
    return "\n\n".join(parts)


# ---- the fork --------------------------------------------------------------

def _default_dispatch(ctx) -> Callable:
    from .turn_dispatch import TurnDispatchService
    return TurnDispatchService(ctx).submit


def fork(ctx, parent_session: str, data: dict, *, dispatch: Callable | None = None) -> dict:
    """Fork ``parent_session`` into the children ``data`` asks for.

    Returns ``{"parent": session, "children": [row, ...]}``; each row has
    ``name``, and either ``session``, ``cwd``, ``backend``, ``fork``
    (``native``/``seed``), ``delivered`` (and ``branch``), or ``error``.
    """
    specs = parse(data)
    parent = _parent(parent_session.strip("/"))
    for spec in specs:
        if spec.backend and not backends.is_valid(spec.backend):
            raise ForkError(400, "invalid_backend", f"Unknown backend: {spec.backend}")
    send = dispatch or _default_dispatch(ctx)
    rows = []
    for spec in specs:
        try:
            rows.append(_fork_child(ctx, parent, spec, send))
        except ForkError as exc:
            log("forkChildFail", f"{parent.session} {spec.name}: {exc.code} {exc}")
            rows.append({"name": spec.name, "error": exc.code, "message": str(exc)})
    log("forkDone", f"{parent.session}: " + ", ".join(
        f"{row['name']}={row.get('fork') or row.get('error')}" for row in rows))
    return {"parent": parent.session, "children": rows}


def _fork_child(ctx, parent: _Parent, spec: ChildSpec, send: Callable) -> dict:
    from .agent_lifecycle import AgentLifecycleError, AgentLifecycleService
    from .turn_dispatch import DispatchCommand
    cwd = (make_worktree(spec) if spec.repo
           else str(pathlib.Path(spec.cwd or parent.cwd).expanduser()))
    if not pathlib.Path(cwd).is_dir():
        raise ForkError(400, "cwd_missing", f"No such directory: {cwd}")
    backend = backends.normalize(spec.backend or parent.backend)
    conversation = _native_fork(parent, backend, cwd)
    request = {"name": spec.name, "cwd": cwd, "backend": backend,
               "parent": parent.session, "role": "helper",
               "voice_id": "{}", "synthesize_audio": False}
    if conversation:
        request["resume_session_id"] = conversation
    for key in ("model", "effort"):
        if getattr(spec, key):
            request[key] = getattr(spec, key)
    try:
        created = AgentLifecycleService(ctx).create(request)
    except AgentLifecycleError as exc:
        raise ForkError(exc.status, exc.code, exc.message) from exc
    helper = created.session
    row = {"name": spec.name, "session": helper, "cwd": cwd, "backend": backend,
           "fork": "native" if conversation else "seed", "delivered": False}
    if spec.branch:
        row["branch"] = spec.branch
    text = message(parent=parent.session, helper=helper, spec=spec, cwd=cwd,
                   seed=None if conversation else _seed(parent))
    try:
        send(DispatchCommand(
            text=text, requested_session=helper, forced_session=helper,
            trace_id=new_trace_id(), synthesize_audio=False, origin="agent",
            sender_agent_id=parent.agent_id, queue_if_busy=True))
        row["delivered"] = True
    except Exception as exc:  # noqa: BLE001 - the helper exists; say so
        log_exception("forkDeliverFail", exc, detail=helper)
        row["message"] = f"created, but its task was not delivered: {exc}"
    return row
