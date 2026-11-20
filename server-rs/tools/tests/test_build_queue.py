"""A target belongs to the effective dev/test profile of its last build."""
from pathlib import Path
import runpy

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
