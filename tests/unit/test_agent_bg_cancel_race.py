"""CLI cancellation is fenced to its owner and exact run, including receipts."""
import importlib.util
from pathlib import Path

from lib import agents, background_jobs, db

spec = importlib.util.spec_from_file_location('cancel_cli', Path(__file__).parents[2] / 'scripts/agent_bg.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def create():
    agents.create_agent(persona='Owner', voice_id='v', cwd='/tmp', session='owner')
    return background_jobs.upsert(session='owner', job_id='shared-id', kind='worker', title='Owned')


def cancel(handle, owner='owner'):
    return cli.main(['bg', owner, 'job-cancel', handle])


def test_cli_stale_handle_and_wrong_owner_never_cancel_successor(capsys):
    first = create()
    handle = background_jobs.job_handle(first)
    assert cancel(handle) == 0
    assert cancel(handle) == 0  # exact-run retry
    second = background_jobs.restart(session='owner', job_id='shared-id', kind='worker', title='Next')
    before = dict(db.conn().execute("SELECT * FROM background_jobs WHERE job_id='shared-id'").fetchone())
    assert cancel(handle) == 1
    assert 'generation mismatch' in capsys.readouterr().err
    assert cancel(background_jobs.job_handle(second), 'stranger') == 1
    after = dict(db.conn().execute("SELECT * FROM background_jobs WHERE job_id='shared-id'").fetchone())
    assert after == before
    assert after['status'] == 'running'
    assert cancel(background_jobs.job_handle(second)) == 0


def test_cli_snapshot_survives_successor_start_at_commit(monkeypatch):
    first = create()
    connection = db.conn()
    successor = []

    class CommitRace:
        fired = False

        def execute(self, sql, *args, **kwargs):
            result = connection.execute(sql, *args, **kwargs)
            if sql == 'COMMIT' and not self.fired:
                self.fired = True
                successor.append(background_jobs.restart(
                    session='owner', job_id='shared-id', kind='worker', title='Successor'))
            return result

        def __getattr__(self, name):
            return getattr(connection, name)

    proxy = CommitRace()
    monkeypatch.setattr(db, 'conn', lambda: proxy)
    # Success describes generation 1 even though generation 2 is created as
    # soon as its cancellation commits. No post-commit re-read may cancel it.
    assert cancel(background_jobs.job_handle(first)) == 0
    current = background_jobs.get('shared-id', reconcile=False)
    assert current['generation'] == successor[0]['generation'] == first['generation'] + 1
    assert current['status'] == 'running'


def test_cli_missing_and_other_terminal_are_not_success(capsys):
    assert cancel('bg1:1:missing') == 1
    first = create()
    background_jobs.finish('shared-id', generation=first['generation'])
    assert cancel(background_jobs.job_handle(first)) == 1
    assert background_jobs.get('shared-id', reconcile=False)['status'] == 'succeeded'
