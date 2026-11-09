"""Single original-inventory filesystem STORAGE fixture SOURCE, UNEXECUTED.

Plain supplied bytes are NOT SQLite/pytest/plugin/process proof. Fixture setup
and injected module-alias lifetime require isolated admitted setup qualification.
"""
import hashlib
from pathlib import Path

from . import coverage_originals as collector
from .coverage_copy_read import _root
from .suite_evidence import ROLES


def run_originals_storage_control(*, root):
    root = _root(root)
    if any(root.iterdir()):
        raise ValueError('fresh EMPTY original storage fixture root required')
    paths, original_bytes = {}, {}
    for label in collector.LABELS:
        if label in ('catalog', 'archive', 'profile', 'config'):
            name = label + '.fixture'
        else:
            role, phase = label.split('.')
            name = ('coverage-pid-' + str(100 + ROLES.index(role)) + '.data'
                    if phase == 'data' else role + '.' + phase + '.json')
        path = root / name
        raw = ('SYNTHETIC_STORAGE_ONLY:' + label + '\n').encode()
        with path.open('xb') as stream:
            stream.write(raw)
        paths[label] = str(path)
        original_bytes[label] = raw
    owner = root / 'owner.json'
    with owner.open('xb') as stream:
        stream.write(b'{"synthetic_storage_only":true}\n')
    owner_before = owner.read_bytes()
    limits = {'file_bytes': {label: 4096 for label in collector.LABELS},
              'total_bytes': 200000, 'path_bytes': 4096,
              'inventory_entries': 32, 'metadata_bytes': 4096}
    value = collector.observe_originals(paths=paths, receipt_root=str(root), limits=limits)
    assert set(value['observations']) == set(collector.LABELS)
    assert set(value['raw']) == {'config'} | {r + '.' + p for r in ROLES for p in ('start', 'finish')}
    for label, raw in original_bytes.items():
        observation = value['observations'][label]
        assert observation['path'] == paths[label]
        assert observation['bytes'] == len(raw)
        assert observation['sha256'] == hashlib.sha256(raw).hexdigest()
        if label in value['raw']:
            assert value['raw'][label] == raw
    assert value['source_process_or_closed_writer_authenticated'] is False
    # Negatives must refuse at complete preflight/inventory BEFORE any stream.
    original_observer = collector.observe_file
    calls = []
    def forbidden_stream(*args, **kwargs):
        calls.append(True)
        raise AssertionError('stream before complete31 preflight')
    try:
        collector.observe_file = forbidden_stream
        for label, reason in (('small_file', 'original size'),
                              ('small_total', 'aggregate pre-read'),
                              ('alias', 'path/inode'),
                              ('wrong_channel', 'role/phase'),
                              ('inventory_cap', 'entry bound'),
                              ('partial', 'unexpected original'),
                              ('existing_index', 'unexpected original'),
                              ('declared_sidecar', 'original associated sidecar')):
            bad_paths = dict(paths)
            bad_limits = {**limits, 'file_bytes': dict(limits['file_bytes'])}
            extra, moved_archive = None, None
            if label == 'small_file':
                bad_limits['file_bytes']['catalog'] = 1
            elif label == 'small_total':
                bad_limits['total_bytes'] = 1
            elif label == 'alias':
                bad_paths['archive'] = bad_paths['catalog']
            elif label == 'wrong_channel':
                bad_paths['gw0.start'] = paths['controller.start']
            elif label == 'inventory_cap':
                bad_limits['inventory_entries'] = 1
            elif label == 'declared_sidecar':
                # Valid declared binding basename hides a different original's
                # sidecar from an ordinary exact-name inventory. No new file.
                moved_archive = root / 'gw7.finish.json-wal'
                Path(paths['archive']).rename(moved_archive)
                bad_paths['archive'] = str(moved_archive)
            else:
                extra = root / ('controller.finish.json.partial' if label == 'partial' else 'index.json')
                with extra.open('xb') as stream:
                    stream.write(b'UNCERTAIN_PRESERVE\n')
            try:
                try:
                    collector.observe_originals(paths=bad_paths, receipt_root=str(root), limits=bad_limits)
                except ValueError as error:
                    assert reason in str(error), (label, str(error))
                else:
                    raise AssertionError('original preflight negative accepted: ' + label)
                if extra is not None:
                    assert extra.read_bytes() == b'UNCERTAIN_PRESERVE\n'
            finally:
                # Only the fixture removes its own newly introduced diagnostic.
                # The collector never deletes/retries/repairs any candidate.
                if moved_archive is not None:
                    moved_archive.rename(paths['archive'])
                if extra is not None:
                    extra.unlink()
    finally:
        collector.observe_file = original_observer
    assert not calls
    assert collector.observe_file is original_observer
    assert owner.read_bytes() == owner_before
    assert all(Path(paths[label]).read_bytes() == raw for label, raw in original_bytes.items())
    return {'scope': 'ORIGINAL31_FILESYSTEM_STORAGE_ONLY_NOT_DECODER_OR_PRODUCER_PROOF',
            'all_controls_unexecuted_at_authoring': True}
