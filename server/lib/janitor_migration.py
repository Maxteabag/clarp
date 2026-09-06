"""Explicit, default-read-only cutover of an owned local task-label pilot.

No database writes, process-name killing, model changes, automatic enable or
rollback. If anything fails after stopping the old service, leave both listeners
off for inspection; restarting either side is a separate deliberate operation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


MAX_BYTES = 1_048_576
MAX_TARGETS = 1000
MAX_RECEIPTS = 1000


def _identify(payload: dict) -> None:
    frozen = {key: payload[key] for key in ("progress", "ownership", "receipts")}
    payload["import_id"] = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()


def _read(path: Path, default=None):
    if not path.exists() and default is not None:
        return default
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError(f"missing or oversized pilot file: {path.name}")
    return json.loads(path.read_text())


def _only(row, keys):
    if not isinstance(row, dict):
        raise ValueError("pilot row must be an object")
    return {key: row[key] for key in keys if key in row}


def _bounded_map(value, keys):
    if not isinstance(value, dict) or len(value) > MAX_TARGETS:
        raise ValueError("pilot target map exceeds 1000 records or is invalid")
    return {session: _only(row, keys) for session, row in value.items()}


def read_pilot(root: Path, session: str) -> dict:
    """Keep revisions and receipts, never copy prompts, transcripts or credentials."""
    identity = _read(root / "session.json")
    if identity.get("session") != session:
        raise ValueError("pilot session.json does not match the requested session")
    state = _read(root / "event-watch-state.json")
    if state.get("delivery"):
        raise ValueError("pilot has an in-flight or ambiguous delivery; let it settle before migrating")
    cursor = state.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("pilot cursor must be a nonnegative integer")
    seen = state.get("seen", [])
    if not isinstance(seen, list) or len(seen) > 256 or any(not isinstance(s, str) for s in seen):
        raise ValueError("pilot seen trace IDs are invalid or exceed 256")
    event_keys = ("state_id", "trace_id", "origin", "first_pending_at", "next_check_at", "reason")
    review_keys = ("task_key", "change_key", "fingerprint", "label", "outcome", "unchanged_streak",
                   "reviewed_at", "next_eligible_at", "state_id", "batch_id")
    progress = {"cursor": cursor, "seen": seen,
                "pending": _bounded_map(state.get("pending", {}), event_keys),
                "deferred": _bounded_map(state.get("deferred", {}), event_keys),
                "reviews": _bounded_map(_read(root / "reviews.json", {}), review_keys)}
    owned = _read(root / "owned.json", {})
    if not isinstance(owned, dict) or len(owned) > MAX_TARGETS:
        raise ValueError("pilot ownership exceeds 1000 records or is invalid")
    ownership = [{"target_session": target, **_only(row, ("label", "source_state_id", "at"))}
                 for target, row in owned.items()]
    receipt_path = root / "review-results.jsonl"
    if not receipt_path.exists():
        receipt_path = root / "changes.jsonl"
    receipts = []
    if receipt_path.exists():
        # Read a bounded tail even when the complete historical journal is large.
        with receipt_path.open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(max(0, size - MAX_BYTES))
            if size > MAX_BYTES:
                source.readline()
            lines = source.read(MAX_BYTES).decode().splitlines()[-MAX_RECEIPTS:]
        for line in lines:
            row = json.loads(line)
            outcome = row.get("outcome", "changed" if row.get("changed") else "same_task")
            receipts.append({"target_session": row.get("session"),
                             "before": row.get("before", row.get("old", "")),
                             "after": row.get("after", row.get("label", "")),
                             "outcome": outcome, "reason": str(row.get("reason", ""))[:300],
                             "source_state_id": row.get("state_id", row.get("source_state_id")),
                             "at": row.get("at", row.get("reviewed_at")),
                             "batch_id": row.get("batch_id", "")})
    result = {"progress": progress, "ownership": ownership, "receipts": receipts}
    encoded = json.dumps(result, sort_keys=True).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("bounded pilot import exceeds 1 MiB; reduce the retained history explicitly")
    _identify(result)
    return result


def prepare_import(source: dict, snapshot: dict, scope: dict) -> tuple[dict, dict]:
    """An obsolete ownership claim is retained in the backup, never reclaimed."""
    payload = json.loads(json.dumps(source))
    agents = {a.get("session"): a for a in snapshot.get("agents", [])}

    def in_scope(session):
        agent = agents.get(session, {})
        identity = agent.get("agent_id")
        return (identity is not None and not agent.get("is_janitor") and not agent.get("archived_at")
                and identity not in scope.get("exclude_agent_ids", [])
                and (not scope.get("agent_ids") or identity in scope["agent_ids"]))

    retained, omitted = [], []
    for row in source["ownership"]:
        target = row["target_session"]
        agent = agents.get(target, {})
        label = agent.get("status_text", agent.get("custom_status", "")) or ""
        reason = "outside current scope" if not in_scope(target) else "label no longer matches"
        if in_scope(target) and label == row.get("label") and label:
            retained.append(row)
        else:
            omitted.append({"target_session": target, "reason": reason})
    payload["ownership"] = retained
    dropped_pending = []
    for key in ("pending", "deferred"):
        for target in list(payload["progress"][key]):
            if not in_scope(target):
                dropped_pending.append(target)
                del payload["progress"][key][target]
    _identify(payload)
    return payload, {"omitted_ownership": omitted, "omitted_pending": sorted(set(dropped_pending))}


def _service(args, runner):
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", args.service):
        raise ValueError("--service must name one exact systemd user service")
    result = runner(["systemctl", "--user", "show", args.service,
                     "--property=Id,FragmentPath,WorkingDirectory,ExecStart,ActiveState,MainPID",
                     "--no-pager"], capture_output=True, text=True, check=True)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    root = Path(args.pilot_dir).resolve()
    if values.get("Id") != args.service or values.get("WorkingDirectory") != str(root):
        raise ValueError("service identity or working directory does not match this pilot")
    job = _read(root / "event-watch-job.json")
    handle = str(job.get("handle", ""))
    if not re.fullmatch(r"bg1:[1-9][0-9]*:[A-Za-z0-9_.-]+", handle):
        raise ValueError("pilot has no valid generation-specific background-job handle")
    job_id = handle.split(":", 2)[2]
    expected = (f"/usr/bin/python3 {root}/watch_events.py --state-dir {root} "
                f"--agent-id {args.agent_id} --job-id {job_id}")
    command = values.get("ExecStart", "")
    # systemd show renders bash -c's argument without shell quotes. Match the
    # entire executable argument field; appended commands cannot pass.
    arguments = re.search(r"argv\[\]=(.*?) ; ignore_errors=", command)
    allowed = [expected, f"/bin/bash -c {expected}"]
    if not arguments or arguments.group(1) not in allowed or command.count("argv[]=") != 1:
        raise ValueError("service ExecStart does not exactly match this pilot and agent")
    if values.get("ActiveState") == "active" and str(job.get("pid")) != values.get("MainPID"):
        raise ValueError("service PID does not match the pilot's recorded worker")
    return values


def _agent(snapshot: dict, args):
    matches = [a for a in snapshot.get("agents", []) if a.get("session") == args.session]
    if len(matches) != 1 or matches[0].get("agent_id") != args.agent_id:
        raise ValueError("Host session does not resolve to the exact expected agent ID")
    agent = matches[0]
    if agent.get("busy") or agent.get("queued_turn_count", 0) or agent.get("archived_at"):
        raise ValueError("pilot agent is busy, queued or archived; migration requires an idle agent")
    return agent


def _schedule(request, args):
    rows = request("GET", "/agent-schedules?session=" + quote(args.session, safe="")).get("schedules", [])
    rows = [s for s in rows if s.get("schedule_id") == args.cron_id]
    if len(rows) != 1 or rows[0].get("session") != args.session:
        raise ValueError("legacy cron does not belong to the exact pilot session")
    if rows[0].get("agent_id") and rows[0]["agent_id"] != args.agent_id:
        raise ValueError("legacy cron agent ID does not match")
    if rows[0].get("cron_expression") != "*/5 * * * *":
        raise ValueError("legacy cron is not the known five-minute pilot schedule")
    return rows[0]


def _resume(args, request, current, service, schedule) -> dict:
    """Recover an ambiguous response without regenerating the import's identity."""
    backup = Path(args.resume_backup).expanduser()
    if not backup.is_file() or backup.stat().st_size > 4 * MAX_BYTES:
        raise ValueError("resume backup is missing or exceeds 4 MiB")
    saved = json.loads(backup.read_text())
    summary = saved.get("summary", {})
    if saved.get("phase") != "stopped-before-import" or any(summary.get(key) != getattr(args, key)
            for key in ("session", "agent_id", "service", "cron_id")):
        raise ValueError("backup does not describe this exact stopped pilot")
    if not current or current.get("enabled") or service.get("ActiveState") not in {"inactive", "failed"} \
            or service.get("MainPID") != "0" or schedule.get("enabled"):
        raise ValueError("resume requires the exact old service and cron off, and replacement paused")
    payload = saved.get("import", {})
    expected_hash = payload.get("import_id")
    _identify(payload)
    if not expected_hash or payload["import_id"] != expected_hash:
        raise ValueError("frozen import hash does not match the backup")
    revision = saved.get("host_export", {}).get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("backup has no guarded Host revision")
    result = {"dry_run": not args.apply, "resume_backup": str(backup), "session": args.session,
              "import_id": expected_hash, "expected_revision": revision, "result_state": "paused"}
    if not args.apply:
        return result
    path = "/janitors/" + quote(args.session, safe="")
    receipt = request("POST", path + "/import-pilot", {**payload,
                      "expected_revision": revision, "expected_agent_id": args.agent_id})
    after = request("GET", path)["janitor"]
    if after.get("enabled") or after.get("agent_id") != args.agent_id:
        raise ValueError("replacement changed during recovery; inspect it before enabling either listener")
    return {**result, "receipt": receipt, "janitor": after}


def migrate(args, request, *, runner=subprocess.run) -> dict:
    """Apply only with explicit flag; default makes no API/filesystem/service writes."""
    root = Path(args.pilot_dir).resolve(strict=True)
    args.pilot_dir = str(root)
    catalog = request("GET", "/janitors")  # Prove support before touching the old listener.
    current = next((j for j in catalog["janitors"] if j.get("session") == args.session), None)
    if current and (current.get("agent_id") != args.agent_id or current.get("enabled")):
        raise ValueError("replacement must be the same agent and paused before migration")
    if current and current.get("template_id") != "task-labels":
        raise ValueError("replacement must use the task-labels template")
    if current and len([a for a in current.get("attachments", [])
                        if a.get("trigger_id") == "agent-work-completed"]) != 1:
        raise ValueError("replacement must have exactly one event trigger before migration")
    snapshot = request("GET", "/agents/snapshot")
    original = _agent(snapshot, args)
    schedule = _schedule(request, args)
    service = _service(args, runner)
    if getattr(args, "resume_backup", None):
        return _resume(args, request, current, service, schedule)
    settings = _read(root / "settings.json", {})
    by_session = {a.get("session"): a.get("agent_id") for a in snapshot.get("agents", [])}
    exclusions = settings.get("exclude_sessions", [])
    if not isinstance(exclusions, list) or any(not isinstance(s, str) or s not in by_session for s in exclusions):
        raise ValueError("a pilot scope exclusion cannot be resolved")
    scope = current["scope"] if current else {
        "agent_ids": [], "exclude_agent_ids": [by_session[s] for s in exclusions]}
    source = read_pilot(root, args.session)
    payload, omissions = prepare_import(source, snapshot, scope)
    summary = {"dry_run": not args.apply, "session": args.session, "agent_id": args.agent_id,
               "service": args.service, "service_state": service.get("ActiveState"),
               "cron_id": args.cron_id, "cron_enabled": bool(schedule.get("enabled")),
               "cursor": payload["progress"]["cursor"], "ownership_count": len(payload["ownership"]),
               "receipt_count": len(payload["receipts"]), **omissions,
               "result_state": "paused", "import_id": payload["import_id"]}
    if not args.apply:
        return summary
    if not args.backup:
        raise ValueError("--apply requires a new --backup path")
    backup = Path(args.backup).expanduser()
    # Exclusive private backup before any mutations; never overwrite recovery evidence.
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"phase": "before-stop", "summary": summary, "pilot": source, "import": payload,
                   "janitor": current, "schedule": schedule, "service": service}, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    _service(args, runner)  # Revalidate ownership immediately before stopping.
    runner(["systemctl", "--user", "disable", "--now", args.service],
           capture_output=True, text=True, check=True)
    stopped = _service(args, runner)
    if stopped.get("ActiveState") not in {"inactive", "failed"} or stopped.get("MainPID") != "0":
        raise ValueError("pilot service has not stopped; replacement remains paused")
    if _schedule(request, args).get("enabled"):
        request("POST", "/agent-schedules/toggle", {"schedule_id": args.cron_id, "enabled": False})
    if _schedule(request, args).get("enabled"):
        raise ValueError("legacy cron is still enabled; replacement remains paused")
    snapshot = request("GET", "/agents/snapshot")
    _agent(snapshot, args)
    source = read_pilot(root, args.session)  # Final frozen cursor after the old writer stops.
    payload, omissions = prepare_import(source, snapshot, scope)
    path = "/janitors/" + quote(args.session, safe="")
    if current is None:
        current = request("POST", "/janitors", {"session": args.session,
                          "template_id": "task-labels",
                          "scope": scope,
                          "attachments": [{"trigger_id": "agent-work-completed", "trigger_version": 1,
                                           "enabled": True, "config": {"coalesce_seconds": 8, "max_targets": 3}}]})["janitor"]
    export = request("GET", path + "/migration-state")
    with backup.open("w") as output:
        json.dump({"phase": "stopped-before-import", "summary": summary, "pilot": source, "import": payload,
                   "host_export": export, "schedule": schedule, "service": service}, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    latest = request("GET", path)["janitor"]
    if latest.get("enabled") or latest.get("agent_id") != args.agent_id:
        raise ValueError("replacement changed before import; it must remain paused with the same identity")
    receipt = request("POST", path + "/import-pilot", {**payload,
                      "expected_revision": latest["revision"], "expected_agent_id": args.agent_id})
    after = request("GET", path)["janitor"]
    final_agent = _agent(request("GET", "/agents/snapshot"), args)
    if after.get("enabled") or any(final_agent.get(k) != original.get(k)
                                   for k in ("backend", "backend_session_id", "model", "effort")):
        raise ValueError("migration verification failed; inspect the backup, do not enable either listener")
    return {**summary, **omissions, "dry_run": False, "backup": str(backup), "import_id": payload["import_id"],
            "cursor": payload["progress"]["cursor"], "ownership_count": len(payload["ownership"]),
            "receipt_count": len(payload["receipts"]),
            "janitor": after, "receipt": receipt}
