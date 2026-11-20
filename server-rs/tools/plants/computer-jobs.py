#!/usr/bin/env python3
"""A computer job survives restart, has one owner, and only its current run may end."""
import runner

RUNS = {
    "recovery": ["test", "-p", "clarp-host", "--lib", "computer_job_corruption_preserves_originals"],
    "wait": ["test", "-p", "clarp-host", "--lib", "computer_job_producers_wake_only_after_their_generation"],
    "rows": ["test", "--test", "host", "computer_job_transitions_and_wire_rows_match_the_reference"],
    "http": ["test", "--test", "host", "computer_jobs_can_be_seen_and_cancelled_without_an_agent"],
    "store": ["test", "-p", "clarp-host", "--lib", "computer_job_ownership_survives_concurrent_registration"],
}
PLANTS = []


def change(file, run, pairs):
    for old, new in pairs:
        PLANTS.append((file, run, f"{old} becomes {new}", old, new))


F = "clarp-host/src/fleet/computer_jobs.rs"
H = "clarp-host/src/edge.rs"
W = "clarp-host/src/wire.rs"
K = "clarp-kernel/src/jobs.rs"
change(F, "http", [
    ('"kept.computer-jobs"', '"kept.unused-jobs"'),
    ('jobs.timelines.insert(id, row.timeline)', 'jobs.timelines.insert(id, vec![])'),
    ('revisions.observe(row.job.revision);','revisions.observe(0);'),
    ('self.book.jobs().cloned().collect()', 'self.book.active().cloned().collect()'),
    ('if row.job.id != id', 'if row.job.id == id'),
    ('timeline.push(entry.clone())', 'timeline.clear()'),
    ('self.store\n            .put(TABLE, &job.id, &bytes)', 'self.store\n            .put(TABLE, "different-id", &bytes)'),
])
change(F, "store", [
    ('self.book.get(id).is_some()', 'self.book.get(id).is_none()'),
    ('self.jobs.get(id)', 'self.jobs.get(id).filter(|_| false)'),
    ('self.computer_jobs.book.apply(record);', 'let _ = record;'),
    ('"background-job-updated"', '"job-updated"'),
    ('"change_revision":job.revision', '"change_revision":0'),
    ('"job_id":job.id','"job_id":job.kind'),
    ('wire::computer_job(computer_id,job,now)','wire::computer_job("",job,now)'),
    ('record.job.revision = revision;', 'record.job.revision = revision + 1;'),
])
change("clarp-host/src/fleet.rs", "store", [
    ('if key == "server_id"', 'if key == "computer_id"'),
    ('if self.computer_jobs.contains(&job_id)', 'if !self.computer_jobs.contains(&job_id)'),
    ('Ok(Err(JobOwner::Computer))','Ok(Ok(()))'),
])
change(H, "http", [
    ('job.status.is_active() || job.updated_at_ms', 'job.status.is_terminal() || job.updated_at_ms'),
    ('now - crate::agent::JOB_SHOWN_FOR_MS','now + crate::agent::JOB_SHOWN_FOR_MS'),
    ('ended.iter().take(100)', 'ended.iter().take(99)'),
    ('ended.iter().take(100)', 'ended.iter().take(101)'),
    ('std::cmp::Reverse(job.updated_at_ms)', 'job.updated_at_ms'),
    ('"owner_session": owner.map_or("",', '"owner_session": owner.map_or("Computer",'),
    ('"owner_agent_id": owner.map_or("",', '"owner_agent_id": owner.map_or("Computer",'),
    ('"helper_session": "",','"helper_session": "Computer",'),
    ('"expected_generation"] = json!(expected)', '"expected_generation"] = json!(job.generation)'),
    ('"current_generation"] = json!(job.generation)', '"current_generation"] = json!(expected)'),
    ('"job_handle": job.handle(),','"job_handle": job.id,'),
])
change(W, "rows", [
    ('"", "", "Computer", Some(computer_id)', '"", "", "Host", Some(computer_id)'),
    ('"agent_id": agent_id,\n        "session": session,\n        "kind": job.kind',
     '"agent_id": job.id,\n        "session": session,\n        "kind": job.kind'),
    ('"owner_kind": if computer_id.is_some()', '"owner_kind": if computer_id.is_none()'),
    ('"computer_id": computer_id,','"computer_id": null,'),
])
change(K,"rows",[
    ('status: if queued {','status: if !queued {'),
    ('job.generation = known.generation + 1','job.generation = known.generation'),
    ('job.status = known.status;', 'job.status = JobStatus::Running;'),
    ('if starts && job.status == JobStatus::Queued','if !starts && job.status == JobStatus::Queued'),
    ('job.cancelled_at_ms = Some(now_ms);', 'job.cancelled_at_ms = None;'),
])

change(F, "recovery", [
    ('"clarp-host: skipped {} unreadable computer job records; original bytes retained"', '"skipped {}"'),
    ('|| self.unreadable.contains(id)', '|| !self.unreadable.contains(id)'),
    ('self.computer_jobs.unreadable.remove(&job.id)', 'self.computer_jobs.unreadable.remove("other")'),
    ('jobs.unreadable.len()', '1'),
    ('"clarp-host: registration replaced an unreadable computer job record"', '"computer job changed"'),
])
change(F, "wait", [
    ('job.generation != generation || !job.status.is_active()', 'job.generation == generation || !job.status.is_active()'),
    ('|| !job.status.is_active()', '|| job.status.is_active()'),
    ('*generation != job.generation || job.status.is_terminal()', 'job.status.is_terminal()'),
    ('*generation != job.generation || job.status.is_terminal()', '*generation != job.generation'),
    ('self.computer_jobs.notify_producers(job);', 'let _ = job;'),
    ('sender.send_replace(true)', 'sender.send_replace(false)'),
])
change(H, "http", [
    ('app.fleet.computer_job(job_id).await?.is_none()', 'app.fleet.computer_job(job_id).await?.is_none() && false'),
])
change(F, "store", [
    ('Ok(Err(ComputerJobRefusal::Kernel(why)))', 'Err(Unavailable(why.to_string()))'),
])
if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
