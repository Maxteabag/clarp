"""A model-run Janitor closes its run with worked/idle/failed and one line
(`clarp-admin janitor outcome`); only the active run can report."""
from __future__ import annotations

import pytest

from lib import agents, db, janitors
from lib.janitor_context import build_context_from_connection


@pytest.fixture
def run():
    for session in ("labels", "worker"):
        aid = agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session, backend="codex")
        agents.record_state(aid, "done")
    config = janitors.create("labels")
    config = janitors.set_enabled("labels", config["revision"], True)
    return janitors.create_run(config["attachments"][0]["attachment_id"], config["generation"],
                               [build_context_from_connection(db.conn(), "worker")])


def test_the_active_run_records_its_activity_and_summary(run):
    assert janitors.report_activity(run["run_id"], "worked", "Relabelled  one\ntask") == \
        {"run_id": run["run_id"], "activity": "worked", "summary": "Relabelled one task"}
    janitors.report_activity(run["run_id"], "idle", "Nothing needed changing")
    stored = janitors.get_run(run["run_id"])
    assert (stored["activity"], stored["activity_summary"]) == ("idle", "Nothing needed changing")


@pytest.mark.parametrize("activity, summary", [("busy", ""), ("idle", "x" * 301)])
def test_an_invalid_report_is_refused(run, activity, summary):
    with pytest.raises(janitors.JanitorError):
        janitors.report_activity(run["run_id"], activity, summary)


def test_a_finished_run_cannot_report(run):
    janitors.finish_run(run["run_id"], "cancelled")
    with pytest.raises(janitors.JanitorError) as error:
        janitors.report_activity(run["run_id"], "idle", "late")
    assert error.value.status == 409


def test_the_cli_posts_the_closing_report():
    import argparse
    from lib import janitor_cli
    parser = argparse.ArgumentParser()
    janitor_cli.add_parsers(parser.add_subparsers(dest="command"), handler=None)
    args = parser.parse_args(["janitor", "outcome", "run-1", "--status", "idle", "--summary", "Nothing to do"])
    sent = []
    janitor_cli.execute(args, lambda method, path, body=None: sent.append((method, path, body)) or {"ok": True})
    assert sent == [("POST", "/janitor-runs/run-1/activity", {"activity": "idle", "summary": "Nothing to do"})]


def test_an_existing_database_gains_the_activity_columns():
    columns = {row[1] for row in db.conn().execute("PRAGMA table_info(janitor_runs)")}
    assert {"activity", "activity_summary"} <= columns
