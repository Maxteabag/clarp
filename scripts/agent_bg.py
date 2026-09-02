#!/usr/bin/env python3
"""Set or clear an agent's custom out-of-band status.

Usage:
    agent_bg.py <session> on  [label]   # persist short label and mark BACKGROUND
    agent_bg.py <session> off           # clear label and mark idle
    agent_bg.py <session> job-upsert <id> <kind> <title> [detail]
    agent_bg.py <session> job-restart <id> <kind> <title> [detail]
    agent_bg.py <session> job-start <handle>
    agent_bg.py <session> job-heartbeat <handle>
    agent_bg.py <session> job-finish <handle>
    agent_bg.py <session> job-fail <handle> [reason]
    agent_bg.py <session> job-active <handle>
    agent_bg.py <session> job-cancelled <handle>

Run on the server host; writes to the live claude-pwa DB so the running server
broadcasts the state change. The label is durable agent metadata, so it keeps
showing while the agent is idle/live and survives ordinary turn state changes.
Keep labels to 2-3 words / under 20 chars, e.g. "Awaiting Domi" or "Building".
Long labels are shortened at the source. Call `off` when the task finishes.
"""
import os
import sys
import pathlib

# Import the deployed lib + use the live DB (same store the running server uses).
sys.path.insert(0, os.environ.get(
    "CLARP_CODE_ROOT", os.environ.get(
        "CLARP_SHARE_DIR", str(pathlib.Path.home() / ".local/share/clarp"))))

from lib import agents               # noqa: E402
from lib.protocol import AgentState  # noqa: E402
from lib.status_labels import shorten_status_label  # noqa: E402


def job_handle(job: dict) -> str:
    from lib import background_jobs
    return background_jobs.job_handle(job)


def parse_job_handle(raw: str) -> tuple[str, int]:
    from lib import background_jobs
    return background_jobs.parse_job_handle(raw)


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[2] == "job-upsert":
        if len(argv) < 6:
            print("usage: agent_bg.py <session> job-upsert <job-id> <kind> <title> [detail]", file=sys.stderr)
            return 2
        from lib import background_jobs
        worker_pid, worker_token = background_jobs.current_worker_identity()
        job = background_jobs.upsert(
            session=argv[1], job_id=argv[3], kind=argv[4], title=argv[5],
            detail=argv[6] if len(argv) > 6 else "",
            status="running", worker_pid=worker_pid,
            worker_start_token=worker_token,
        )
        print(job_handle(job))
        return 0 if job["status"] in background_jobs.ACTIVE_STATUSES else 1
    if len(argv) >= 3 and argv[2] == "job-restart":
        if len(argv) < 6:
            print("usage: agent_bg.py <session> job-restart <job-id> <kind> <title> [detail]", file=sys.stderr)
            return 2
        from lib import background_jobs
        worker_pid, worker_token = background_jobs.current_worker_identity()
        job = background_jobs.restart(
            session=argv[1], job_id=argv[3], kind=argv[4], title=argv[5],
            detail=argv[6] if len(argv) > 6 else "",
            worker_pid=worker_pid, worker_start_token=worker_token,
        )
        print(job_handle(job))
        return 0 if job["status"] in background_jobs.ACTIVE_STATUSES else 1
    if len(argv) >= 4 and argv[2] == "job-start":
        from lib import background_jobs
        job_id, generation = parse_job_handle(argv[3])
        job = background_jobs.start(job_id, generation=generation)
        return 0 if job and job["status"] == "running" else 1
    if len(argv) >= 4 and argv[2] == "job-heartbeat":
        from lib import background_jobs
        job_id, generation = parse_job_handle(argv[3])
        worker_pid, worker_token = background_jobs.current_worker_identity()
        job = background_jobs.heartbeat(
            job_id, worker_pid=worker_pid, worker_start_token=worker_token,
            generation=generation)
        return 0 if job and job["status"] in background_jobs.ACTIVE_STATUSES else 1
    if len(argv) >= 4 and argv[2] == "job-finish":
        from lib import background_jobs
        job_id, generation = parse_job_handle(argv[3])
        worker_pid, worker_token = background_jobs.current_worker_identity()
        return 0 if background_jobs.finish(
            job_id, generation=generation, worker_pid=worker_pid,
            worker_start_token=worker_token) else 1
    if len(argv) >= 4 and argv[2] == "job-fail":
        from lib import background_jobs
        job_id, generation = parse_job_handle(argv[3])
        reason = argv[4] if len(argv) > 4 else "worker_failed"
        worker_pid, worker_token = background_jobs.current_worker_identity()
        return 0 if background_jobs.finish(
            job_id, generation=generation, status="failed", reason=reason,
            worker_pid=worker_pid, worker_start_token=worker_token) else 1
    if len(argv) >= 4 and argv[2] == "job-active":
        from lib import background_jobs
        job_id, generation = parse_job_handle(argv[3])
        worker_pid, worker_token = background_jobs.current_worker_identity()
        job = background_jobs.heartbeat(
            job_id, worker_pid=worker_pid, worker_start_token=worker_token,
            generation=generation)
        return 0 if (
            job and job["status"] in background_jobs.ACTIVE_STATUSES
            and int(job.get("generation") or 1) == generation
        ) else 1
    if len(argv) >= 4 and argv[2] == "job-cancelled":
        from lib import background_jobs
        job_id, generation = parse_job_handle(argv[3])
        return 0 if background_jobs.is_cancelled(
            job_id, generation=generation) else 1
    if len(argv) < 3 or argv[2] not in {"on", "off"}:
        print(
            "usage: agent_bg.py <session> on|off [label] | "
            "job-upsert|job-restart|job-start|job-heartbeat|job-finish|"
            "job-fail|job-active|job-cancelled ...",
            file=sys.stderr,
        )
        return 2
    session, mode = argv[1], argv[2]
    label = argv[3] if len(argv) > 3 else ""
    if mode == "on":
        label, shortened = shorten_status_label(label)
        if shortened:
            print(f"agent_bg: shortened status to {label!r}", file=sys.stderr)
    else:
        label = ""
    a = agents.get_by_session(session)
    if not a:
        print(f"agent_bg: no agent for session {session!r}", file=sys.stderr)
        return 1
    kind = AgentState.BACKGROUND if mode == "on" else AgentState.IDLE
    agents.set_custom_status(a["agent_id"], label if mode == "on" else "")
    agents.record_state(a["agent_id"], kind, {"label": label} if label else None)
    if mode == "on":
        from lib import background_jobs
        worker_pid, worker_token = background_jobs.current_worker_identity()
        if worker_pid and worker_token:
            background_jobs.heartbeat_jobs(
                background_jobs.adopted_worker_job_ids(worker_pid, session),
                worker_pid=worker_pid, worker_start_token=worker_token,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
