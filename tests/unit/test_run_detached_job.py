"""The clarp-background-jobs worker: it owns the job and writes the receipt.

Runs the shipped script with the real agent_bg helper on the test database.
"""
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time

import pytest

from lib import agents, background_jobs as jobs, db

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'skills/clarp-background-jobs/scripts/run_detached_job.sh'


@pytest.fixture
def run(tmp_path):
    agents.create_agent(persona='Ziggy fixture', voice_id='', cwd=str(tmp_path), session='ziggy-fixture')
    env = {**os.environ, 'CLAUDE_PWA_DB': str(db.DB_PATH), 'CLARP_CODE_ROOT': str(ROOT / 'server'),
           'CLARP_AGENT_BG': shlex.join([sys.executable, str(ROOT / 'scripts/agent_bg.py')])}
    env.pop('CLARP_BACKGROUND_WORKER_PID', None)

    def start(job_id, *command, log_name=None, expect=0):
        log = tmp_path / 'out' / (log_name or f'{job_id}.log')   # directory made by the script
        out = subprocess.run([str(SCRIPT), '--no-wake', 'ziggy-fixture', job_id, 'render', 'Render fixture', str(log), '--', *command],
                             env=env, capture_output=True, text=True, timeout=40)
        assert out.returncode == expect, out.stderr
        return (out.stdout.strip() if expect == 0 else out.stderr), log
    return start


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize('code,status', [(0, 'succeeded'), (3, 'failed')])
def test_worker_reports_the_commands_own_exit_and_keeps_its_log(run, code, status):
    handle, log = run(f'exit-{code}', 'sh', '-c', f'echo rendering; sleep 1; exit {code}')
    assert handle == f'bg1:1:exit-{code}'
    assert jobs.get(f'exit-{code}', reconcile=False)['status'] == 'running'  # returned while the work runs
    assert wait_for(lambda: jobs.get(f'exit-{code}', reconcile=False)['status'] == status)
    job = jobs.get(f'exit-{code}', reconcile=False)
    assert pathlib.Path(f'{log}.exit').read_text().strip() == str(code)
    assert log.read_text().strip() == 'rendering' and job['log_path'] == str(log)
    assert job['progress_text'] == f'Command exited {code}; log {log}'
    assert job['terminal_reason'] == ('' if code == 0 else f'exit {code}')


def test_cancelling_the_job_stops_the_workers_own_command(run, tmp_path):
    handle, log = run('cancel-me', 'sleep', '30')
    jobs.cancel('cancel-me')
    assert wait_for(lambda: pathlib.Path(f'{log}.exit').exists(), timeout=30)
    assert jobs.get('cancel-me', reconcile=False)['status'] == 'cancelled'


def test_a_cancelled_job_id_is_not_reused_for_a_new_run(run, tmp_path):
    handle, log = run('one-run', 'sleep', '30')
    jobs.cancel('one-run')
    assert wait_for(lambda: pathlib.Path(f'{log}.exit').exists(), timeout=30)
    marker = tmp_path / 'second-run-started'
    error, _ = run('one-run', 'touch', str(marker), log_name='again.log', expect=1)
    assert 'job-upsert failed' in error
    time.sleep(1)
    assert not marker.exists() and jobs.get('one-run', reconcile=False)['status'] == 'cancelled'


def test_a_worker_killed_before_its_receipt_is_never_reported_as_success(run, tmp_path):
    handle, log = run('lost-worker', 'sleep', '5')   # its command ends by itself
    pid = jobs.get('lost-worker', reconcile=False)['worker_pid']
    os.killpg(os.getpgid(pid), signal.SIGKILL)   # the worker's own group: its command lives on in its own session
    assert wait_for(lambda: jobs.get('lost-worker')['status'] == 'failed')
    job = jobs.get('lost-worker')
    assert job['terminal_reason'] == 'worker_vanished' and job['outcome_state'] == 'unknown'
    assert not pathlib.Path(f'{log}.exit').exists()



def test_the_worker_runs_as_its_own_service_with_private_env_and_args(tmp_path, monkeypatch):
    """Ziggy, 2026-10-04 18:49: a render started from a turn lived in the
    runtime's cgroup and the runtime's release handoff killed it; setsid and
    nohup do not leave a cgroup. The launcher hands the worker to systemd-run
    (real survival is proven against the user manager outside this suite).
    systemd expands $ in its command line and shows a unit's arguments, so
    nothing the caller supplies may travel there."""
    agents.create_agent(persona='Ziggy fixture', voice_id='', cwd=str(tmp_path), session='ziggy-fixture')
    shims, runtime_dir, work = tmp_path / 'bin', tmp_path / 'run', tmp_path / 'work'
    for d in (shims, runtime_dir, work):
        d.mkdir()
    recorded = tmp_path / 'systemd-run.argv'
    (shims / 'systemctl').write_text('#!/bin/sh\n[ "$2" = show-environment ] && exit 0\nexit 97\n')
    # Starts the unit's command the way systemd does: own session, in the
    # requested directory, with almost none of the caller's environment.
    (shims / 'systemd-run').write_text(f"""#!/bin/sh
printf '%s\\n' "$@" > {recorded}
for a in "$@"; do case $a in --working-directory=*) cd "${{a#*=}}";; esac; done
while [ "$1" != /bin/bash ]; do shift; done
env -i PATH="$PATH" HOME="$HOME" setsid "$@" >/dev/null 2>&1 < /dev/null &
""")
    for f in shims.iterdir():
        f.chmod(0o755)
    secret = 'private-value-' + os.urandom(6).hex()
    log = tmp_path / 'out' / 'render.log'
    env = {**os.environ, 'PATH': f"{shims}{os.pathsep}{os.environ['PATH']}", 'XDG_RUNTIME_DIR': str(runtime_dir),
           'CLAUDE_PWA_DB': str(db.DB_PATH), 'CLARP_CODE_ROOT': str(ROOT / 'server'),
           'CLARP_AGENT_BG': shlex.join([sys.executable, str(ROOT / 'scripts/agent_bg.py')]),
           'RENDER_TOKEN': secret}
    env.pop('CLARP_BACKGROUND_WORKER_PID', None)
    out = subprocess.run([str(SCRIPT), '--no-wake', 'ziggy-fixture', 'own-service', 'render', 'Render $HOME fixture', str(log), '--',
                          'sh', '-c', 'pwd; echo "$RENDER_TOKEN" | wc -c; echo literal \\$HOME; '
                          'grep SigIgn /proc/self/status'],
                         cwd=work, env=env, capture_output=True, text=True, timeout=40)
    assert out.returncode == 0, out.stderr
    assert wait_for(lambda: jobs.get('own-service', reconcile=False)['status'] == 'succeeded')
    argv = recorded.read_text()
    assert '--expand-environment=no' in argv and f'--working-directory={work}' in argv
    # The runtime's umask (0077) and open-file limit are the command's too, and
    # a failure to start is reported by systemd-run itself (Type=exec).
    umask = subprocess.run(['sh', '-c', 'umask'], capture_output=True, text=True).stdout.strip()
    for prop in ('Type=exec', 'KillMode=control-group', f'UMask={umask}', 'LimitNOFILE='):
        assert prop in argv, prop
    assert secret not in argv and 'RENDER_TOKEN' not in argv and 'Render' not in argv and 'literal' not in argv
    lines = log.read_text().split('\n')
    assert lines[:3] == [str(work), str(len(secret) + 1), 'literal $HOME']
    # Started like a shell starts it: SIGPIPE (bit 13) and SIGXFSZ (bit 25) not ignored.
    ignored = int(lines[3].split()[1], 16)
    assert not ignored & (1 << 12) and not ignored & (1 << 24)
    assert jobs.get('own-service', reconcile=False)['title'] == 'Render $HOME fixture'
    assert pathlib.Path(f'{log}.unit').read_text().startswith('clarp-job-own-service-')
    assert list(runtime_dir.iterdir()) == []   # private env/arg files consumed



@pytest.fixture
def goal_env(tmp_path):
    from lib import task_plans
    aid = agents.create_agent(persona='Ziggy fixture', voice_id='', cwd=str(tmp_path), session='ziggy-fixture')
    agents.bind_backend_session(aid, 'ziggy-native-1')   # goal recovery needs a bound conversation
    plan = task_plans.create(session='ziggy-fixture', plan_id='render', title='Render',
                             items=[{'id': 'r', 'title': 'Render'}],
                             goal={'outcome': 'Deliver the render', 'criteria': ['Delivered'],
                                   'limits': 'none', 'enroll': True})
    env = {**os.environ, 'CLAUDE_PWA_DB': str(db.DB_PATH), 'CLARP_CODE_ROOT': str(ROOT / 'server'),
           'CLARP_AGENT_BG': shlex.join([sys.executable, str(ROOT / 'scripts/agent_bg.py')]),
           'CLARP_GOAL': shlex.join([sys.executable, str(ROOT / 'scripts/agent_tasks.py')])}
    env.pop('CLARP_BACKGROUND_WORKER_PID', None)

    def launch(job_id, *options, command=('sleep', '1'), session='ziggy-fixture', extra_env=None, **popen):
        log = tmp_path / 'out' / f'{job_id}.log'
        return subprocess.run([str(SCRIPT), *options, session, job_id, 'render', 'Render', str(log),
                               '--', *command], env={**env, **(extra_env or {})}, capture_output=True,
                              text=True, timeout=90, **popen)
    launch.tmp = tmp_path
    return plan, launch


def test_goal_dependency_is_attached_without_losing_the_goals_own_plan(goal_env):
    from lib import task_plans
    plan, launch = goal_env
    pid = plan['plan_id']
    task_plans._goal_mutate(pid, revision=plan['revision'], action='checkpoint', data={
        'progress': 'Frames 1-5 approved', 'next_work': 'Compose the board after the render'})
    first = launch('render-a', '--goal', pid, '--deadline', '07200')   # base 10, not octal
    assert first.returncode == 0, first.stderr
    goal = task_plans.get(pid)['goal']
    assert (goal['continuation']['dependency_key'], goal['continuation']['job_handle']) == (
        'render-a', first.stdout.strip())
    # The goal's routine 120 s recovery timer is not the job's deadline.
    assert goal['continuation']['due_at'] > db.now_ms() + 7000_000
    assert goal['checkpoint']['progress'] == 'Frames 1-5 approved'
    assert goal['checkpoint']['next_work'].startswith('Compose the board after the render')
    # A second job never silently takes the first one's wake.
    second = launch('render-b', '--goal', pid)
    assert second.returncode == 4 and 'already waits on render-a' in second.stderr
    assert jobs.get('render-b', reconcile=False) is None   # nothing was started
    # Once render-a has ended (the owner may still be in its turn, so the goal
    # has not recorded it), the refusal says what to do instead of dropping it.
    assert wait_for(lambda: jobs.get('render-a', reconcile=False)['status'] == 'succeeded')
    third = launch('render-b2', '--goal', pid)
    assert third.returncode == 4 and 'already ended' in third.stderr and 'checkpoint' in third.stderr
    signals = [f.name for f in (launch.tmp / 'out').glob('render-a.log.*.*')
               if f.suffix in ('.handle', '.go', '.decided')]
    assert signals == []   # each launch removes its own signal files
    assert task_plans.get(pid)['goal']['continuation']['dependency_key'] == 'render-a'


def test_a_goal_waiting_on_another_kind_of_dependency_keeps_it(goal_env):
    from lib import task_plans
    plan, launch = goal_env
    pid = plan['plan_id']
    task_plans._goal_mutate(pid, revision=plan['revision'], action='checkpoint', data={
        'progress': 'Asked the client', 'next_work': 'Act on the reply',
        'continuation': {'kind': 'dependency', 'key': 'client-reply', 'reason': 'Waiting for a reply',
                         'due_at': db.now_ms() + 3600_000}})
    out = launch('render-c', '--goal', pid)
    assert out.returncode == 4 and 'already waits on client-reply' in out.stderr
    assert 'LOG.exit' not in out.stderr   # job advice only for a job
    assert jobs.get('render-c', reconcile=False) is None
    assert task_plans.get(pid)['goal']['continuation']['dependency_key'] == 'client-reply'


@pytest.mark.parametrize('options', [[], ['--goal'], ['--deadline'], ['--deadline', '1h'], ['--goal', ''],
                                     ['--deadline', '0'], ['--deadline', '99999999999999999999'],
                                     ['--goal', 'x', '--no-wake']])
def test_bad_options_are_refused_before_anything_starts(goal_env, options):
    plan, launch = goal_env
    out = launch('never-started', *options)
    assert out.returncode == 2
    assert jobs.get('never-started', reconcile=False) is None



def test_a_goal_that_cannot_carry_the_wake_starts_nothing(goal_env):
    from lib import task_plans
    plan, launch = goal_env
    agents.create_agent(persona='Other fixture', voice_id='', cwd=str(launch.tmp), session='other-fixture')
    marker = launch.tmp / 'ran'
    foreign = launch('foreign', '--goal', plan['plan_id'], session='other-fixture', command=('touch', str(marker)))
    assert foreign.returncode == 4 and 'belongs to ziggy-fixture' in foreign.stderr
    p = task_plans._goal_mutate(plan['plan_id'], revision=plan['revision'], action='pause', data={'reason': 'User paused'})
    paused = launch('paused', '--goal', plan['plan_id'], command=('touch', str(marker)))
    assert paused.returncode == 4 and 'paused' in paused.stderr
    p = task_plans._goal_mutate(p['plan_id'], revision=p['revision'], action='resume', data={'reason': 'User resumed'})
    task_plans._goal_mutate(p['plan_id'], revision=p['revision'], action='block', data={'reason': 'Needs a decision'})
    blocked = launch('blocked', '--goal', plan['plan_id'], command=('touch', str(marker)))
    assert blocked.returncode == 4 and 'blocked' in blocked.stderr
    manual = task_plans.create(session='ziggy-fixture', plan_id='manual', title='Manual',
                               items=[{'id': 'm', 'title': 'Manual'}],
                               goal={'outcome': 'Tracked by hand', 'criteria': ['Done'], 'limits': 'none', 'enroll': False})
    unenrolled = launch('unenrolled', '--goal', manual['plan_id'], command=('touch', str(marker)))
    missing = launch('missing', '--goal', 'no-such-goal', command=('touch', str(marker)))
    assert unenrolled.returncode == 4 and 'not enrolled' in unenrolled.stderr
    assert missing.returncode == 4 and 'Traceback' not in missing.stderr
    assert not marker.exists()
    assert all(jobs.get(j, reconcile=False) is None for j in ('foreign', 'paused', 'blocked', 'unenrolled', 'missing'))


def _goal_helper(tmp_path, body):
    """A clarp-goal stand-in whose checkpoint misbehaves; everything else is real."""
    script = tmp_path / 'goal-helper.sh'
    real = shlex.join([sys.executable, str(ROOT / 'scripts/agent_tasks.py')])
    script.write_text(f'#!/bin/bash\nif [ "$1" = checkpoint ]; then\n{body}\nfi\nexec {real} "$@"\n')
    script.chmod(0o755)
    return str(script)


def test_the_command_never_runs_when_its_wake_was_not_stored(goal_env):
    plan, launch = goal_env
    marker = launch.tmp / 'ran'
    helper = _goal_helper(launch.tmp, '  echo "database is locked" >&2; exit 1')
    out = launch('unstored', '--goal', plan['plan_id'], command=('touch', str(marker)),
                 extra_env={'CLARP_GOAL': helper})
    assert out.returncode == 3 and 'did NOT run' in out.stderr
    assert wait_for(lambda: jobs.get('unstored', reconcile=False)['status'] == 'failed')
    assert jobs.get('unstored', reconcile=False)['terminal_reason'].startswith('not started')
    time.sleep(1)
    assert not marker.exists()


def test_a_launcher_lost_after_storing_the_wake_still_runs_the_command_once(goal_env):
    """The turn can die between storing the dependency and the go signal; the
    worker reads the goal itself, so the stored wake is honoured exactly once."""
    from lib import task_plans
    plan, launch = goal_env
    marker = launch.tmp / 'runs'
    real = shlex.join([sys.executable, str(ROOT / 'scripts/agent_tasks.py')])
    helper = _goal_helper(launch.tmp, f'  {real} "$@" >/dev/null || exit 1\n  kill -9 -- -$(ps -o pgid= -p $$ | tr -d " ")')
    out = launch('lost-launcher', '--goal', plan['plan_id'], extra_env={'CLARP_GOAL': helper},
                 command=('sh', '-c', f'echo run >> {marker}'), start_new_session=True)
    assert out.returncode == -9
    assert wait_for(lambda: jobs.get('lost-launcher', reconcile=False)['status'] == 'succeeded')
    assert marker.read_text() == 'run\n'
    assert task_plans.get(plan['plan_id'])['goal']['continuation']['job_handle'] == 'bg1:1:lost-launcher'



def test_a_readback_that_never_confirms_reports_not_run_truthfully(goal_env):
    """The dependency is stored but cannot be read back (here: every read fails
    after the store). The launcher and the worker share one decision, so the
    launcher only says "not run" when the command really did not run."""
    plan, launch = goal_env
    marker = launch.tmp / 'ran'
    flag = launch.tmp / 'stored'
    real = shlex.join([sys.executable, str(ROOT / 'scripts/agent_tasks.py')])
    script = launch.tmp / 'goal-reads-fail.sh'
    script.write_text(f'#!/bin/bash\nif [ "$1" = get ] && [ -e {flag} ]; then echo "database is locked" >&2; exit 1; fi\n'
                      f'if [ "$1" = checkpoint ]; then {real} "$@" && : > {flag}; exit $?; fi\nexec {real} "$@"\n')
    script.chmod(0o755)
    out = launch('unconfirmed', '--goal', plan['plan_id'], command=('touch', str(marker)),
                 extra_env={'CLARP_GOAL': str(script)})
    assert out.returncode == 3 and 'did NOT run' in out.stderr
    time.sleep(2)
    assert not marker.exists()
    assert wait_for(lambda: jobs.get('unconfirmed', reconcile=False)['status'] == 'failed')


def test_a_failed_confirmation_never_hides_a_command_that_started(goal_env):
    """The launcher's read-back hangs and fails, while the worker reads the
    stored dependency and starts first. The launcher used to print "did NOT
    run" anyway."""
    plan, launch = goal_env
    marker = launch.tmp / 'ran'
    real = shlex.join([sys.executable, str(ROOT / 'scripts/agent_tasks.py')])
    script = launch.tmp / 'launcher-reads-fail.sh'
    script.write_text(f"""#!/bin/bash
in_worker() {{ p=$$; while [ "$p" -gt 1 ]; do tr '\\\\0' ' ' < /proc/$p/cmdline | grep -q __worker && return 0
  p=$(awk '{{print $4}}' /proc/$p/stat); done; return 1; }}
if [ "$1" = get ] && [ -e {launch.tmp}/stored ] && ! in_worker; then sleep 5; echo "database is locked" >&2; exit 1; fi
if [ "$1" = checkpoint ]; then {real} "$@" && : > {launch.tmp}/stored; exit $?; fi
exec {real} "$@"
""")
    script.chmod(0o755)
    out = launch('confirmed-by-worker', '--goal', plan['plan_id'], command=('touch', str(marker)),
                 extra_env={'CLARP_GOAL': str(script)})
    assert marker.exists() and out.returncode == 0, out.stderr
    assert out.stdout.strip() == 'bg1:1:confirmed-by-worker' and 'worker started the command' in out.stderr
