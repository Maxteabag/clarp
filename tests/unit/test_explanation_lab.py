"""Lab plumbing checks never invoke a model."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("explanation_lab", Path(__file__).parents[2] / "labs/explanations/run.py")
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)


def test_fixtures_cover_read_versus_run_and_unknown_purpose():
    cases = {c["case"]: c for c in lab.fixtures()}
    assert len(cases) == 8
    assert cases["run_grocery_script"]["activity"]["scripts"] == cases["inspect_grocery_script"]["activity"]["scripts"]
    assert cases["run_grocery_script"]["activity"]["command"] != cases["inspect_grocery_script"]["activity"]["command"]
    assert "scripts" not in cases["unknown_script"]["activity"]


def test_schema_validation_does_not_award_missing_or_oversized_answers():
    assert lab.validate_result({"1": "Read the build settings."}, 1)
    for response in [{}, {"2": "Read files."}, {"1": ""}, {"1": "x" * 161}, {"1": 3}]:
        assert not lab.validate_result(response, 1)


def test_summary_reports_failures_and_wall_time_not_per_item_latency():
    rows = [{"transport": "exec", "prompt": "baseline", "batch_size": 8,
             "total_ms": time, "valid": valid} for time, valid in [(100, True), (300, False)]]
    assert lab.summarize(rows)[0]["median_ms"] == 200
    assert lab.summarize(rows)[0]["valid"] == 1


def test_focused_row_waits_behind_history_without_priority():
    spec = importlib.util.spec_from_file_location("scheduler_lab", Path(__file__).parents[2] / "labs/explanations/scheduler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.contention(False)["live_batch_number"] == 9
    assert module.contention(True)["live_batch_number"] == 2
