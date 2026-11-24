"""A target belongs to the effective dev/test profile of its last build."""
import os
from pathlib import Path
import runpy
import shutil

import pytest

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


def lane(root, name, stamp, target_bytes):
    """A checkout's copy as tools/build leaves it, with a target of its own from before the shared one."""
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
    (copy / 'server-rs/target').mkdir()
    (copy / 'server-rs/target/blob').write_bytes(b'x' * target_bytes)
    return copy


def used(root):
    return sum(f.stat().st_size for f in root.rglob('*') if f.is_file() and not f.is_symlink())


@pytest.mark.parametrize('needed, gone', [
    # Superseded test executables first.
    (1000, ['deps/host-1111111111111111']),
    # Then checkouts' own targets, oldest built first, never this checkout's.
    (3000, ['deps/host-1111111111111111', 'old']),
    (5000, ['deps/host-1111111111111111', 'old', 'newer']),
    # Then the shared incremental data, and the whole shared target last.
    (8000, ['deps/host-1111111111111111', 'old', 'newer', 'incremental']),
    (10000, ['deps/host-1111111111111111', 'old', 'newer', 'incremental', 'shared']),
])
def test_make_room_order(tmp_path, monkeypatch, capsys, needed, gone):
    names = queue['make_room'].__globals__
    monkeypatch.setitem(names, 'QUEUE', tmp_path)
    monkeypatch.chdir(tmp_path)
    copies = [lane(tmp_path, name, stamp, 2000) for name, stamp in (('old', 10**18), ('newer', 2 * 10**18), ('here', 10**17))]
    deps = tmp_path / 'target/debug/deps'
    deps.mkdir(parents=True)
    for name, age in (('host-1111111111111111', 1), ('host-2222222222222222', 2)):
        (deps / name).write_bytes(b'x' * 1000)
        (deps / name).chmod(0o755)
        os.utime(deps / name, ns=(age, age))
    (deps / 'libserde-3333333333333333.rlib').write_bytes(b'x' * 1000)
    (tmp_path / 'target/debug/incremental').mkdir()
    (tmp_path / 'target/debug/incremental/clarp_host').write_bytes(b'x' * 3000)
    capacity = 10**6
    monkeypatch.setattr(names['shutil'], 'disk_usage', lambda path: shutil._ntuple_diskusage(capacity, 0, capacity - used(tmp_path)))
    floor = capacity - used(tmp_path) + needed
    sources = {f: f.read_bytes() for copy in copies for f in copy.rglob('*') if f.is_file() and 'target' not in f.parts}
    with queue['directory'](tmp_path) as root:
        assert queue['make_room'](root, 'here', floor)
    present = {
        'deps/host-1111111111111111': (deps / 'host-1111111111111111').exists(),
        'old': (tmp_path / 'old/server-rs/target').exists(),
        'newer': (tmp_path / 'newer/server-rs/target').exists(),
        'incremental': (tmp_path / 'target/debug/incremental').exists(),
        'shared': (tmp_path / 'target').exists(),
    }
    assert [step for step, exists in present.items() if not exists] == gone
    # This checkout's own target is its build's to remove, and no checkout's copy is touched.
    assert (tmp_path / 'here/server-rs/target/blob').exists()
    assert {f: f.read_bytes() for f in sources} == sources
    assert 'ready' in capsys.readouterr().out


def test_make_room_stops_the_build_when_nothing_more_can_go(tmp_path, monkeypatch, capsys):
    names = queue['make_room'].__globals__
    monkeypatch.setitem(names, 'QUEUE', tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'target/debug').mkdir(parents=True)
    monkeypatch.setattr(names['shutil'], 'disk_usage', lambda path: shutil._ntuple_diskusage(100, 100, 0))
    with queue['directory'](tmp_path) as root:
        assert not queue['make_room'](root, 'here', 1)
    assert not (tmp_path / 'target').exists()
    assert 'Build stopped before cargo' in capsys.readouterr().err
