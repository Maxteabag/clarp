"""A target belongs to the effective dev/test profile of its last build."""
import fcntl
import inspect
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'build-queue.py'
queue = runpy.run_path(str(SCRIPT))


def places(root):
    """Where the tool works, moved into a test's own directory: this version's copies in root, the older tools' in
    root/.legacy. The jobs a test starts in processes of their own are given it as PLACES."""
    legacy = root / '.legacy'
    return dict(ROOT=root, COPY_LOCKS=root / '.copy-locks', STAGES=root / '.stage', LEGACY_ROOT=legacy,
                LEGACY_SLOT=legacy / 'lock', LEGACY_HOLDER=legacy / 'holder', LEGACY_EVICTION=legacy / 'eviction-lock')


PLACES = inspect.getsource(places)


def at(root, monkeypatch):
    """The tool's functions, working in root instead of the build machine's /var/tmp/clarp-build."""
    names = queue['main'].__globals__
    for name, value in places(root).items():
        monkeypatch.setitem(names, name, value)
    (root / '.legacy').mkdir(exist_ok=True)
    return names


def test_debug_generation(tmp_path):
    profile = ['line-tables-only', 'line-tables-only']
    debug = tmp_path / 'target/debug'
    release = tmp_path / 'target/release'
    release.mkdir(parents=True)
    binary = release / 'program'
    binary.write_bytes(b'release')
    with queue['directory'](tmp_path) as workspace:
        prepare = queue['prepare_debug']
        # No mark and no debug output. The first build can populate this generation.
        assert not prepare(workspace, profile)
        assert not debug.exists()
        debug.mkdir()
        built = debug / 'program'
        built.write_bytes(b'warm')
        assert not prepare(workspace, profile)
        assert built.read_bytes() == b'warm'
        # Either setting can change independently.
        for changed in (['2', profile[1]], ['2', '0']):
            assert prepare(workspace, changed)
            assert not debug.exists()
            debug.mkdir()
            built.write_bytes(b'old')
        # A populated target from a tool that predates the mark.
        (debug.parent / '.clarp-debug-profile').unlink()
        assert prepare(workspace, profile)
        assert not debug.exists()
    assert binary.read_bytes() == b'release'


def lane(root, name, stamp, target=True, built=True, good=True):
    """A checkout's copy as tools/build leaves it on the build machine."""
    copy = root / name
    (copy / 'contract/schemas').mkdir(parents=True)
    (copy / 'contract/schemas/server-info.json').write_text('{}')
    (copy / 'scripts').mkdir()
    (copy / 'scripts/clarp-media-publish.py').write_text('')
    for crate in ('clarp-host', 'clarp-kernel', 'clarp-shim'):
        (copy / 'server-rs/crates' / crate).mkdir(parents=True)
        (copy / 'server-rs/crates' / crate / 'Cargo.toml').write_text('')
    (copy / '.clarp-last-build').write_text('')
    os.utime(copy / '.clarp-last-build', ns=(stamp, stamp))
    # A good build's stamp has a second name; a failed build's stamp is a file of its own.
    if good:
        os.link(copy / '.clarp-last-build', copy / '.clarp-last-good-build')
    else:
        (copy / '.clarp-last-good-build').write_text('')
    if target:
        deps = copy / 'server-rs/target/debug/deps'
        deps.mkdir(parents=True)
        (deps / 'libserde-1111111111111111.rlib').write_text(name)
        os.link(deps / 'libserde-1111111111111111.rlib', copy / 'server-rs/target/debug/libserde.rlib')
        if built:
            (deps / 'libclarp_host-2222222222222222.rlib').write_text(name)
    return copy / 'server-rs'


def reflinks(tmp_path):
    (tmp_path / 'probe').write_text('x')
    cloned = subprocess.run(['cp', '--reflink=always', tmp_path / 'probe', tmp_path / 'probe2'], capture_output=True)
    return cloned.returncode == 0


def test_a_checkout_without_a_target_starts_from_the_last_good_build_with_libraries(tmp_path, monkeypatch):
    at(tmp_path, monkeypatch)
    lane(tmp_path, 'old', 10**18)
    newer = lane(tmp_path, 'newer', 2 * 10**18)
    lane(tmp_path, 'not-a-checkout', 3 * 10**18).parent.joinpath('scripts/clarp-media-publish.py').unlink()
    # Built last, but only checked: its target holds the dependencies' metadata, not their libraries.
    lane(tmp_path, 'checked', 4 * 10**18, built=False)
    # Built last with the Host's library, but that build failed.
    lane(tmp_path, 'failed', 5 * 10**18, good=False)
    here = lane(tmp_path, 'here', 10**17, target=False)
    # Sources that arrived before the other checkout's build.
    now = time.time_ns()
    for path in here.rglob('*'):
        os.utime(path, ns=(now - 10**12, now - 10**12))
    for path in newer.rglob('*'):
        os.utime(path, ns=(now - 10**11, now - 10**11))
    with queue['directory'](tmp_path) as root, queue['directory'](here) as workspace:
        source = queue['seed_target'](root, 'here', workspace)
        if reflinks(tmp_path):
            assert source == 'newer'
            seeded = here / 'target/debug/deps/libserde-1111111111111111.rlib'
            assert seeded.read_text() == 'newer'
            # Hard links inside the target stay links, as cargo made them.
            assert seeded.stat().st_ino == (here / 'target/debug/libserde.rlib').stat().st_ino
            assert seeded.stat().st_ino != (newer / 'target/debug/deps/libserde-1111111111111111.rlib').stat().st_ino
            # This checkout's sources are newer than any build in the clone, so cargo builds its
            # workspace crates from them; the clone keeps the times cargo recorded.
            source = here / 'crates/clarp-host/Cargo.toml'
            assert source.stat().st_mtime_ns > seeded.stat().st_mtime_ns
            assert seeded.stat().st_mtime_ns == (newer / 'target/debug/deps/libserde-1111111111111111.rlib').stat().st_mtime_ns
        else:
            # Without clones the build starts cold rather than copying the data.
            assert source is None
            assert not (here / 'target').exists()
        assert not (here / '.target-seed').exists() and not (here / '.target-seed.clone').exists()
        # A checkout with a target keeps it.
        (here / 'target').mkdir(exist_ok=True)
        (here / 'target/own').write_text('own')
        assert queue['seed_target'](root, 'here', workspace) is None
        assert (here / 'target/own').read_text() == 'own'
    assert (newer / 'target/debug/deps/libserde-1111111111111111.rlib').read_text() == 'newer'


def test_a_build_marks_its_checkout_good_only_when_it_succeeds(tmp_path, monkeypatch):
    # build_space is wrapped by contextmanager: reach the module through a plain function.
    names = at(tmp_path, monkeypatch)
    workspace = lane(tmp_path, 'here', 10**17)
    monkeypatch.chdir(workspace)
    monkeypatch.setitem(names, 'make_room', lambda root, current, floor: True)
    for code, good in ((0, True), (101, False), (None, False), (0, True)):
        outcome = {}
        with queue['build_space'](0, False, outcome) as ready:
            assert ready
            if code is not None:
                outcome['code'] = code
        with queue['directory'](workspace.parent) as copy:
            assert (queue['last_good_build'](copy) is not None) == good


@pytest.fixture
def held_elsewhere():
    """Hold a lock file the way another process does (its own open file). Returns what lets it go; the
    test's end lets go of the rest."""
    held = set()

    def hold(path, mode=fcntl.LOCK_EX):
        path.parent.mkdir(exist_ok=True)
        fd = os.open(path, os.O_RDONLY | os.O_CREAT)
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        held.add(fd)
        return lambda: (held.remove(fd), os.close(fd))
    yield hold
    for fd in held:
        os.close(fd)


def test_a_seed_never_comes_from_a_target_in_use(tmp_path, monkeypatch, held_elsewhere, capsys):
    if not reflinks(tmp_path):
        return  # without clones nothing is ever copied; the test above holds that
    at(tmp_path, monkeypatch)
    lane(tmp_path, 'older', 10**18)
    lane(tmp_path, 'newer', 2 * 10**18)
    here = lane(tmp_path, 'here', 10**17, target=False)
    with queue['directory'](tmp_path) as root, queue['directory'](here) as workspace:
        # The best source has a job of its own running (or a drop): the next is taken, without waiting.
        held_elsewhere(tmp_path / '.copy-locks/newer.lock')
        assert queue['seed_target'](root, 'here', workspace) == 'older'
        assert (here / 'target/debug/deps/libserde-1111111111111111.rlib').read_text() == 'older'
        (here / 'target').rename(tmp_path / '.discard')
        # Every good source in use: a cold build, and it says so.
        held_elsewhere(tmp_path / '.copy-locks/older.lock')
        assert queue['seed_target'](root, 'here', workspace) is None
        assert 'in use (newer,older)' in capsys.readouterr().out
        assert not (here / 'target').exists()


def test_the_older_tools_targets_seed_only_while_none_of_their_builds_or_housekeeping_runs(tmp_path, monkeypatch, held_elsewhere, capsys):
    if not reflinks(tmp_path):
        return
    at(tmp_path, monkeypatch)
    legacy = tmp_path / '.legacy'
    lane(legacy, 'other', 2 * 10**18)
    lane(legacy, 'here', 10**18)
    here = lane(tmp_path, 'here', 10**17, target=False)
    with queue['directory'](tmp_path) as root, queue['directory'](here) as workspace:
        # One of their builds holds their slot; their eviction or drop holds their eviction lock. Neither is waited for.
        for lock in ('lock', 'eviction-lock'):
            let_go = held_elsewhere(legacy / lock)
            started = time.monotonic()
            assert queue['seed_target'](root, 'here', workspace) is None
            assert time.monotonic() - started < 1
            assert "in use (the older tools' copies); building cold" in capsys.readouterr().out
            assert not (here / 'target').exists()
            let_go()
        # Idle: this checkout's own old target first, and it stays theirs, untouched.
        assert queue['seed_target'](root, 'here', workspace) == 'here (older tools)'
        assert (here / 'target/debug/deps/libserde-1111111111111111.rlib').read_text() == 'here'
    assert (legacy / 'here/server-rs/target/debug/deps/libserde-1111111111111111.rlib').read_text() == 'here'


def test_room_is_made_only_from_targets_not_in_use(tmp_path, monkeypatch, held_elsewhere, capsys):
    names = at(tmp_path, monkeypatch)
    for name, stamp in (('oldest', 10**17), ('old', 10**18), ('recent', 2 * 10**18)):
        lane(tmp_path, name, stamp)
    here = lane(tmp_path, 'here', 3 * 10**18)
    # The older tools' copies are theirs to free, whatever they hold.
    lane(tmp_path / '.legacy', 'ancient', 10)
    monkeypatch.chdir(here)
    # Room enough once two targets are gone.
    monkeypatch.setitem(names, 'free_after_removal', lambda floor: sum(
        not (tmp_path / n / 'server-rs/target').exists() for n in ('oldest', 'old', 'recent')) * floor // 2)
    real = names['shutil'].disk_usage
    monkeypatch.setattr(names['shutil'], 'disk_usage', lambda path: real(path)._replace(free=0))
    with queue['directory'](tmp_path) as root:
        held_elsewhere(tmp_path / '.copy-locks/oldest.lock', fcntl.LOCK_SH)
        assert queue['make_room'](root, 'here', 100)
        out = capsys.readouterr().out
        assert 'removed targets=old,recent, targets in use kept=oldest' in out
    assert (tmp_path / 'oldest/server-rs/target').exists() and (here / 'target').exists()
    assert (tmp_path / '.legacy/ancient/server-rs/target').exists()


def staged(root, name, machine, marker):
    """What tools/build on `machine` uploads for checkout `name`: its stage, with `marker` in its sources."""
    stage = root / '.stage' / f'{name}@{machine}.00000000'
    src = lane(stage, 'src', 0, target=False).parent
    (src / 'skills/clarp-sub-agents').mkdir(parents=True)
    (src / 'skills/clarp-sub-agents/run.py').write_text('')
    (src / 'skills/clarp-background-jobs/scripts').mkdir(parents=True)
    (src / 'skills/clarp-background-jobs/scripts/run_detached_job.sh').write_text('')
    mark(stage, marker)
    return stage


def test_each_invocation_stages_its_own_upload_from_the_last_finished_one(tmp_path, monkeypatch, held_elsewhere, capsys):
    at(tmp_path, monkeypatch)
    stages = tmp_path / '.stage'

    def new(bare=False):
        queue['new_stage']('a', 'm1', bare=bare)
        return Path(capsys.readouterr().out.strip())
    first = new()
    assert first.parent == stages and first.name.startswith('a@m1.') and not (first / 'src').exists()
    (first / 'src/server-rs').mkdir(parents=True)
    (first / 'src/server-rs/marker').write_text('first')
    # Its job began, so its upload ended; a later one starts from it, as a copy of its own.
    (first / 'uploaded').touch()
    second = new()
    assert (second / 'src/server-rs/marker').read_text() == 'first'
    (second / 'src/server-rs/marker').write_text('second')
    assert (first / 'src/server-rs/marker').read_text() == 'first'
    # The second's client went before its job began (no mark); its job, still alive, holds it.
    held_elsewhere(second / '.lock')
    other_machine = staged(tmp_path, 'a', 'm2', 'B')
    lost = new()  # a third whose client is lost before its job begins: nothing holds it
    fourth = new()
    # The basis stays, the held one stays, the lost one goes; another machine's is never touched.
    assert first.exists() and second.exists() and not lost.exists() and other_machine.exists()
    assert (fourth / 'src/server-rs/marker').read_text() == 'first'
    assert not (new(bare=True) / 'src').exists()


def mark(stage, marker):
    (stage / 'src/server-rs/marker').write_text(marker)
    (stage / 'src/server-rs/Cargo.lock').write_text(f'lock {marker}')


def start(root, stage, *command):
    """One job, run by the tool as tools/build runs it on the build machine."""
    wrapper = ('import runpy, sys\nfrom pathlib import Path\n' + PLACES +
               'names = runpy.run_path(sys.argv[1])["main"].__globals__\n'
               'names.update(places(Path(sys.argv[2])))\nsys.exit(names["main"](sys.argv[3:]))\n')
    name = stage.name.split('@')[0]
    env = dict(os.environ, CARGO_PROFILE_DEV_DEBUG='line-tables-only', CARGO_PROFILE_TEST_DEBUG='line-tables-only')
    return subprocess.Popen([sys.executable, '-c', wrapper, str(SCRIPT), str(root), '--build-debug', '0', name, str(stage),
                             f'agent {name} test', *command],
                            cwd=stage / 'src/server-rs', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_checkouts_build_side_by_side_and_one_checkouts_jobs_take_turns_on_their_own_sources(tmp_path, monkeypatch):
    at(tmp_path, monkeypatch)
    for name in ('a', 'b'):
        lane(tmp_path, name, 10**18)
    (tmp_path / 'a/server-rs/target/kept').write_text('')
    a1 = staged(tmp_path, 'a', 'm1', 'A from m1')
    a2 = staged(tmp_path, 'a', 'm2', 'A from m2')
    b1 = staged(tmp_path, 'b', 'm1', 'B from m1')
    gate = tmp_path / 'gate'
    gate.mkdir()
    # A job that says it started and what it compiles, waits for the test to let it end, and says again.
    job = ['sh', '-c', f'cat marker > {gate}/$0.seen; touch {gate}/$0; while [ ! -e {gate}/$0.go ]; do sleep 0.05; done; '
           f'echo >> {gate}/$0.seen; cat marker >> {gate}/$0.seen; exit $1']
    first = start(tmp_path, a1, *job, 'a1', '0')
    other = start(tmp_path, b1, *job, 'b1', '0')
    jobs = [first, other]
    try:
        until(lambda: (gate / 'a1').exists() and (gate / 'b1').exists())
        # The same checkout's job from another machine waits for the copy, and says for whom.
        second = start(tmp_path, a2, *job, 'a2', '101')
        jobs.append(second)
        time.sleep(1)
        assert not (gate / 'a2').exists() and second.poll() is None
        # That machine uploads again meanwhile: only its own stage changes.
        mark(a2, 'A from m2, again')
        shown = subprocess.run([sys.executable, '-c',
                                'import runpy, sys\nfrom pathlib import Path\n' + PLACES +
                                'names = runpy.run_path(sys.argv[1])["main"].__globals__\n'
                                'names.update(places(Path(sys.argv[2])))\nnames["main"](["status"])\n',
                                str(SCRIPT), str(tmp_path)], capture_output=True, text=True, check=True).stdout
        # Both running copies, with how long; the waiting job holds nothing yet.
        assert sorted(line.split(' since ')[0] for line in shown.splitlines()) == [
            'building: agent a test', 'building: agent b test']
        assert all(' (for 0m0' in line for line in shown.splitlines())
        (gate / 'a1.go').touch()
        assert first.wait(10) == 0
        assert (gate / 'a1.seen').read_text() == 'A from m1\nA from m1'
        assert (a1 / 'out/Cargo.lock').read_text() == 'lock A from m1'
        until(lambda: (gate / 'a2').exists())
        (gate / 'a2.go').touch()
        # A failure comes back as the command's code and marks the copy not good.
        assert second.wait(10) == 101
        assert (gate / 'a2.seen').read_text() == 'A from m2, again\nA from m2, again'
        assert (a2 / 'out/Cargo.lock').read_text() == 'lock A from m2, again'
        assert "waiting for this checkout's running job to finish (agent a test since" in second.stdout.read()
        with queue['directory'](tmp_path / 'a') as copy:
            assert queue['last_good_build'](copy) is None
        # Publishing keeps the copy's target and stamps.
        assert (tmp_path / 'a/server-rs/target/kept').exists()
        (gate / 'b1.go').touch()
        assert other.wait(10) == 0
        assert (gate / 'b1.seen').read_text() == 'B from m1\nB from m1'
        with queue['directory'](tmp_path / 'b') as copy:
            assert queue['last_good_build'](copy) is not None
        assert not [n for n in os.listdir(tmp_path / '.copy-locks') if n.endswith('.holder')]
    finally:
        for process in jobs:
            process.kill()
            process.wait()
