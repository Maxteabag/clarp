from lib import diagnostics_settings, resource_telemetry


def test_resource_sample_is_bounded_and_content_free():
    value = resource_telemetry.sample()
    assert value["process_rss_kb"] > 0
    assert value["process_threads"] >= 1
    assert value["state_bytes"] >= 0
    assert value["telemetry_bytes"] >= 0
    assert all("text" not in key and "prompt" not in key for key in value)


def test_resource_worker_emits_only_when_enabled(monkeypatch):
    seen = []
    monkeypatch.setattr(
        resource_telemetry.eventlog, "emit",
        lambda source, event, **kw: seen.append((source, event, kw)))
    diagnostics_settings.update({"enabled": False, "categories": []})
    worker = resource_telemetry.ResourceTelemetryWorker(interval_sec=0.01)
    worker.start()
    assert worker._stop.wait(0.03) is False
    worker.stop()
    assert seen == []

    diagnostics_settings.update({"enabled": True, "categories": ["resources"]})
    worker = resource_telemetry.ResourceTelemetryWorker(interval_sec=0.01)
    worker.start()
    assert worker._stop.wait(0.03) is False
    worker.stop()
    assert seen and seen[0][0:2] == ("resources", "sample")
