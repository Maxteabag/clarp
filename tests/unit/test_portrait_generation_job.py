from __future__ import annotations

import importlib.util
import pathlib
import threading

from lib import background_jobs, portrait_generation


SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/portrait_generation_job.py"
SPEC = importlib.util.spec_from_file_location("portrait_generation_job_test", SCRIPT)
worker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worker)


def _job():
    return background_jobs.upsert_computer(
        computer_id="computer-a", job_id="portrait-generation-agent",
        kind="portrait-generation", title="Generate", status="queued")


def test_worker_adopts_generation_and_finishes(monkeypatch):
    job = _job()
    monkeypatch.setattr(
        background_jobs, "process_start_token", lambda _pid: "boot:worker")
    seen = []
    monkeypatch.setattr(
        portrait_generation, "generate_two",
        lambda session, **kwargs: seen.append((session, kwargs["handle"])) or {})

    result = worker.run(
        handle=background_jobs.job_handle(job), session="bella",
        stop=threading.Event())

    assert result == 0
    assert seen == [("bella", background_jobs.job_handle(job))]
    assert background_jobs.get(job["job_id"], reconcile=False)["status"] == "succeeded"


def test_worker_does_not_commit_after_cancellation(monkeypatch):
    job = _job()
    monkeypatch.setattr(
        background_jobs, "process_start_token", lambda _pid: "boot:worker")

    def cancel(_session, **_kwargs):
        background_jobs.cancel(job["job_id"])
        raise portrait_generation.GenerationCancelled("cancelled")

    monkeypatch.setattr(portrait_generation, "generate_two", cancel)

    result = worker.run(
        handle=background_jobs.job_handle(job), session="bella",
        stop=threading.Event())

    assert result == 130
    assert background_jobs.get(job["job_id"], reconcile=False)["status"] == "cancelled"


def test_missing_worker_identity_fails_queued_generation(monkeypatch):
    job = _job()
    monkeypatch.setattr(
        background_jobs, "process_start_token", lambda _pid: "")

    result = worker.run(
        handle=background_jobs.job_handle(job), session="bella",
        stop=threading.Event())

    assert result == 2
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "portrait_worker_identity_unavailable"


def test_local_interruption_terminalizes_running_generation(monkeypatch):
    job = _job()
    monkeypatch.setattr(
        background_jobs, "process_start_token", lambda _pid: "boot:worker")
    monkeypatch.setattr(
        portrait_generation, "generate_two",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            portrait_generation.GenerationCancelled("signal")))

    result = worker.run(
        handle=background_jobs.job_handle(job), session="bella",
        stop=threading.Event())

    assert result == 130
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "portrait_worker_interrupted"
