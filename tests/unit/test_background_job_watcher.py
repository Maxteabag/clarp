from lib import agents, background_jobs
from lib.background_job_watcher import BackgroundJobWatcher


class FakeStream:
    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, event: dict) -> None:
        self.events.append(event)


def test_watcher_emits_typed_persisted_job_change():
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    stream = FakeStream()
    watcher = BackgroundJobWatcher(stream)
    watcher._last_id = background_jobs.latest_event_id()
    job = background_jobs.upsert(
        session="nadia-test", job_id="watch", kind="email", title="Watch")

    watcher._poll_once()

    assert stream.events == [{
        "type": "background-job-updated",
        "change_revision": job["revision"],
        "observed_at": job["updated_at"],
        "job_id": "watch",
        "session": "nadia-test",
        "agent_id": job["agent_id"],
        "status": "running",
        "job": background_jobs.get(
            "watch", reconcile=False, observed_at=job["updated_at"]),
    }]


def test_watcher_reconnect_starts_at_tail_and_snapshot_reconciles():
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    background_jobs.upsert(
        session="nadia-test", job_id="existing", kind="ci", title="Existing")
    stream = FakeStream()
    watcher = BackgroundJobWatcher(stream)

    watcher.start()
    watcher.stop()

    assert stream.events == []
    snapshot = background_jobs.snapshot()
    assert [job["job_id"] for job in snapshot["jobs"]] == ["existing"]
    assert snapshot["snapshot_revision"] == background_jobs.latest_event_id()


def test_watcher_does_not_persist_or_emit_liveness_only_heartbeat():
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    background_jobs.upsert(
        session="nadia-test", job_id="heartbeat", kind="email", title="Watch")
    stream = FakeStream()
    watcher = BackgroundJobWatcher(stream)
    watcher._last_id = background_jobs.latest_event_id()

    background_jobs.heartbeat("heartbeat")
    watcher._poll_once()

    assert stream.events == []
