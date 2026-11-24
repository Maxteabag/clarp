"""A target belongs to the effective dev/test profile of its last build."""
import os
from pathlib import Path
import runpy
import subprocess
import time

queue = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'build-queue.py'))


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
    monkeypatch.setitem(queue['seed_target'].__globals__, 'QUEUE', tmp_path)
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
    names = queue['last_good_build'].__globals__
    monkeypatch.setitem(names, 'QUEUE', tmp_path)
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
