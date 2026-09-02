from lib import health


def setup_function():
    health.reset_for_tests()


def test_health_snapshot_records_success_and_error_timestamps():
    health.mark_success("tts_worker", now=10.0)
    health.mark_error("tts_worker", "rate limited", now=12.0)

    assert health.snapshot() == {
        "tts_worker": {
            "last_success_at": 10.0,
            "last_error_at": 12.0,
            "last_error": "rate limited",
        }
    }


def test_health_snapshot_is_sorted_for_stable_diagnostics():
    health.mark_success("zeta", now=1.0)
    health.mark_success("alpha", now=2.0)

    assert list(health.snapshot()) == ["alpha", "zeta"]
