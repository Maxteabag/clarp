"""Repository test suites must actually be required on pull requests."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_pull_requests_run_the_fast_python_and_javascript_suites():
    workflows = "\n".join(
        path.read_text()
        for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
    )

    assert "make test" in workflows or (
        "pytest" in workflows and "vitest" in workflows
    ), (
        "PR workflows build Docker and run selected installer tests, but the "
        "full fast Python and JavaScript regression suites are not required"
    )
