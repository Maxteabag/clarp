"""lib.decision_delivery: the polling worker that wakes agents with answers."""
import threading

from lib import artifacts
from lib import decision_delivery
from lib.decision_delivery import DecisionDeliveryWorker
from lib.timing import SERVER_TIMING


def test_worker_thread_keeps_its_diagnostic_name_and_cadence():
    worker = DecisionDeliveryWorker(lambda: None)
    assert worker.interval_sec == SERVER_TIMING.decision_delivery_interval_sec == 5.0

    worker.start()
    try:
        names = {thread.name for thread in threading.enumerate()}
        assert "decision-delivery" in names
    finally:
        worker.stop(timeout=1.0)
    assert not worker._thread.is_alive()


def test_run_once_materializes_attention_before_delivering(monkeypatch):
    order = []
    monkeypatch.setattr(artifacts, "attention", lambda: order.append("attention"))
    DecisionDeliveryWorker(lambda: order.append("deliver")).run_once()
    assert order == ["attention", "deliver"]


def test_run_once_logs_and_swallows_delivery_failures(monkeypatch):
    logged = []
    monkeypatch.setattr(artifacts, "attention", lambda: None)
    monkeypatch.setattr(decision_delivery, "log_exception",
                        lambda event, exc, **kw: logged.append((event, str(exc))))

    def fail():
        raise RuntimeError("db locked")

    DecisionDeliveryWorker(fail).run_once()
    assert logged == [("decisionDeliveryWorkerFail", "db locked")]
