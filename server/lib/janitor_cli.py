"""Noninteractive Janitor administration using the authenticated Host API."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote


def json_object(value: str) -> dict:
    """JSON may be inline or @path; never interpret it as shell input."""
    try:
        raw = Path(value[1:]).read_text() if value.startswith("@") else value
        if len(raw.encode()) > 1_048_576:
            raise ValueError("JSON exceeds 1 MiB")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("expected a JSON object")
        return result
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _emit(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _revision(args, request, path: str) -> int:
    if args.expected_revision is not None:
        return args.expected_revision
    current = request("GET", path)["janitor"]
    return int(current["revision"])


def _send(args, request, method: str, path: str, body=None):
    if getattr(args, "dry_run", False):
        return {"dry_run": True, "method": method, "path": path, "body": body}
    return request(method, path, body)


def _configuration(args, *, create=False) -> dict:
    body = dict(getattr(args, "config", None) or {})
    allowed = {"template_id", "scope", "attachments", "model", "effort", "execution", "backend", "options"}
    if create:
        allowed |= {"name", "backend", "cwd", "session", "request_id"}
    unknown = body.keys() - allowed
    if unknown:
        raise ValueError("unsupported configuration fields: " + ", ".join(sorted(unknown)))
    for arg, key in (("template", "template_id"), ("scope", "scope"),
                     ("model", "model"), ("effort", "effort")):
        value = getattr(args, arg, None)
        if value is not None:
            body[key] = value
    return body


def execute(args, request) -> int:
    """Never retry a mutation: 409 requires renewed intent at the current revision."""
    try:
        return _execute(args, request)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(8192)).get("error", str(exc))
        except (ValueError, AttributeError):
            detail = str(exc)
        if exc.code == 404:
            detail = "Janitor or route unavailable; this Host may not support Janitors. " + detail
        elif exc.code == 409:
            detail += " Inspect the current configuration and repeat your intent; nothing was retried."
        raise SystemExit(f"Janitor request failed ({exc.code}): {detail}") from exc
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit(f"Janitor request failed: {exc}") from exc


def _execute(args, request) -> int:
    cmd = args.janitor_command
    if cmd == "triggers":
        _emit(request("GET", "/janitor-triggers"))
    elif cmd in {"list", "templates"}:
        data = request("GET", "/janitors")
        _emit({"templates": data["templates"]} if cmd == "templates" else data)
    elif cmd == "create":
        body = _configuration(args, create=True)
        if args.agent:
            if any(body.get(k) for k in ("name", "backend", "cwd")):
                raise ValueError("conversion takes --agent without new-agent identity fields")
            body["session"] = args.agent
        else:
            for key in ("name", "backend", "cwd"):
                if getattr(args, key, None) is not None:
                    body[key] = getattr(args, key)
            if not body.get("session") and not all(body.get(k) for k in ("name", "backend", "cwd")):
                raise ValueError("provide --agent or all of --name, --backend and --cwd")
        body.setdefault("template_id", "task-labels")
        supplied_id = args.request_id if args.request_id is not None else body.get("request_id")
        if body.get("session"):
            if supplied_id is not None:
                raise ValueError("--request-id is only for creating a new identity; conversion uses its existing session")
        else:
            try:
                request_id = str(uuid.UUID(supplied_id)) if supplied_id is not None else str(uuid.uuid4())
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("request_id must be a UUID") from exc
            body["request_id"] = request_id
            # Persist a recoverable key in command output even if the HTTP
            # response is lost. Retrying requires this key AND identical input.
            print(json.dumps({"request_id": request_id}), file=sys.stderr, flush=True)
        _emit(_send(args, request, "POST", "/janitors", body))
    elif cmd in {"run-context", "review"}:
        path = "/janitor-runs/" + quote(args.run_id, safe="")
        if cmd == "run-context":
            _emit(request("GET", path + "/context"))
        else:
            if args.outcome == "changed" and not args.label:
                raise ValueError("changed requires --label")
            if args.label is not None and (len(args.label) > 20 or not 2 <= len(args.label.split()) <= 3):
                raise ValueError("a label must have 2–3 words and at most 20 characters")
            body = {"target_session": args.target_session,
                    "observed_state_id": args.observed_state_id,
                    "outcome": args.outcome, "reason": args.reason}
            if args.label is not None:
                body["label"] = args.label
            _emit(_send(args, request, "POST", path + "/review", body))
    elif cmd == "migrate-pilot":
        from .janitor_migration import migrate
        _emit(migrate(args, request))
    else:
        path = "/janitors/" + quote(args.session, safe="")
        if cmd == "inspect":
            _emit(request("GET", path))
        elif cmd == "runs":
            _emit(request("GET", path + f"/runs?limit={args.limit}"))
        elif cmd == "export":
            _emit(request("GET", path + "/migration-state"))
        elif cmd == "configure":
            body = _configuration(args)
            body["expected_revision"] = _revision(args, request, path)
            _emit(_send(args, request, "POST", path + "/configure", body))
        elif cmd == "attach":
            trigger, separator, version = args.trigger.rpartition("@")
            if not separator or not trigger or not version.isdigit() or int(version) < 1:
                raise ValueError("--trigger must be a versioned identifier such as agent-work-completed@1")
            current = request("GET", path)["janitor"]
            revision = args.expected_revision or int(current["revision"])
            attachments = [{k: row[k] for k in (
                "attachment_id", "trigger_id", "trigger_version", "enabled", "config") if k in row}
                for row in current.get("attachments", [])]
            attachments.append({"trigger_id": trigger, "trigger_version": int(version),
                                "enabled": True, "config": args.config or {}})
            body = {"expected_revision": revision, "attachments": attachments}
            if args.scope is not None:
                body["scope"] = args.scope
            _emit(_send(args, request, "POST", path + "/configure", body))
        elif cmd in {"enable", "pause"}:
            body = {"expected_revision": _revision(args, request, path), "enabled": cmd == "enable"}
            _emit(_send(args, request, "POST", path + "/enabled", body))
        elif cmd == "remove":
            revision = _revision(args, request, path)
            _emit(_send(args, request, "DELETE", path + f"?expected_revision={revision}"))
        elif cmd == "release":
            body = {"expected_revision": _revision(args, request, path)}
            if args.successor:
                successor_revision = args.successor_revision
                if successor_revision is None:
                    successor = request("GET", "/janitors/" + quote(args.successor, safe=""))["janitor"]
                    successor_revision = int(successor["revision"])
                body.update(successor_session=args.successor, successor_revision=successor_revision)
            elif args.successor_revision is not None:
                raise ValueError("--successor-revision requires --successor")
            _emit(_send(args, request, "POST", path + "/release", body))
    return 0


def add_parsers(sub, handler) -> None:
    janitor = sub.add_parser("janitor", help="Configure and inspect quiet maintenance agents")
    commands = janitor.add_subparsers(dest="janitor_command", required=True)
    for name in ("list", "templates"):
        commands.add_parser(name).set_defaults(func=handler)
    trigger = sub.add_parser("trigger", help="Inspect reusable Janitor triggers")
    trigger.add_subparsers(dest="trigger_command", required=True).add_parser("list").set_defaults(
        func=handler, janitor_command="triggers")
    for name in ("inspect", "runs", "configure", "attach", "enable", "pause", "remove", "release", "export"):
        command = commands.add_parser(name)
        command.add_argument("session")
        command.set_defaults(func=handler)
        if name in {"configure", "attach", "enable", "pause", "remove", "release"}:
            command.add_argument("--expected-revision", type=_positive)
            command.add_argument("--dry-run", action="store_true")
        if name == "release":
            command.add_argument("--successor", help="Transfer matching maintained labels to this paused Janitor")
            command.add_argument("--successor-revision", type=_positive)
        if name == "runs":
            command.add_argument("--limit", type=_positive, default=30)
        if name in {"configure", "attach"}:
            command.add_argument("--config", type=json_object, help="JSON or @file")
            command.add_argument("--scope", type=json_object, help="JSON or @file")
        if name == "attach":
            command.add_argument("--trigger", required=True, help="identifier@version")
        if name == "configure":
            command.add_argument("--template")
            command.add_argument("--model")
            command.add_argument("--effort")
    create = commands.add_parser("create", help="Create or convert an agent, always paused")
    create.set_defaults(func=handler)
    for name in ("agent", "name", "backend", "cwd", "template", "model", "effort"):
        create.add_argument("--" + name)
    create.add_argument("--config", type=json_object, help="JSON or @file")
    create.add_argument("--scope", type=json_object, help="JSON or @file")
    create.add_argument("--request-id", help="Stable UUID for retrying the same new-agent creation")
    create.add_argument("--paused", action="store_true", help="Explicit spelling of the default")
    create.add_argument("--dry-run", action="store_true")
    context = commands.add_parser("run-context", help="Read bounded evidence for an admitted run")
    context.set_defaults(func=handler)
    context.add_argument("run_id")
    review = commands.add_parser("review", help="Submit one guarded effect or unchanged receipt")
    review.set_defaults(func=handler)
    review.add_argument("run_id")
    review.add_argument("--session", dest="target_session", required=True)
    review.add_argument("--state-id", dest="observed_state_id", type=_positive, required=True)
    review.add_argument("--outcome", required=True,
                        choices=("changed", "same_task", "insufficient_context", "error"))
    review.add_argument("--label")
    review.add_argument("--reason", required=True)
    review.add_argument("--dry-run", action="store_true")
    migration = commands.add_parser("migrate-pilot", help="Preview a fenced in-place pilot migration")
    migration.set_defaults(func=handler)
    for name in ("session", "agent-id", "pilot-dir", "service", "cron-id"):
        migration.add_argument("--" + name, required=True)
    migration.add_argument("--backup", help="New private backup file; required for --apply")
    migration.add_argument("--resume-backup", help="Retry the exact frozen import from a stopped cutover backup")
    migration_mode = migration.add_mutually_exclusive_group()
    migration_mode.add_argument("--apply", action="store_true", help="Stop the verified pilot and import paused")
    migration_mode.add_argument("--dry-run", action="store_true", help="Explicit spelling of the read-only default")
