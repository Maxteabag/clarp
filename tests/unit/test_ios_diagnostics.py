import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.ios_diagnostics import diagnostic_counts  # noqa: E402


def test_diagnostic_counts_preserve_every_actionable_metrickit_type():
    counts = diagnostic_counts({
        "crashDiagnostics": [{"id": "crash"}],
        "hangDiagnostics": [{"id": "hang-1"}, {"id": "hang-2"}],
        "cpuExceptionDiagnostics": [{"id": "cpu"}],
        "diskWriteExceptionDiagnostics": [{"id": "disk"}],
        "appLaunchDiagnostics": [{"id": "launch"}],
    })

    assert counts == {
        "crashes": 1,
        "hangs": 2,
        "cpu_exceptions": 1,
        "disk_write_exceptions": 1,
        "slow_launches": 1,
        "total": 6,
    }


def test_diagnostic_counts_ignore_missing_and_malformed_collections():
    assert diagnostic_counts({
        "crashDiagnostics": None,
        "hangDiagnostics": "not-an-array",
    }) == {
        "crashes": 0,
        "hangs": 0,
        "cpu_exceptions": 0,
        "disk_write_exceptions": 0,
        "slow_launches": 0,
        "total": 0,
    }
