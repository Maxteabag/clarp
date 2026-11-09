"""Single original-inventory filesystem STORAGE fixture SOURCE, UNEXECUTED.

Actual retained files + SYNTHETIC process records are NOT SQLite/pytest/kernel proof. Fixture setup
and injected module-alias lifetime require isolated admitted setup qualification.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import coverage_originals as collector
from .coverage_copy_read import _root, _snapshot
from .coverage_build_union import _exclusive_copy
from .coverage_publish_index import publish_original_index
from .coverage_receipt_index import FORMAT
from .test_suite_evidence import NODES, DECODE_LIMITS, _decoder_fixture, _decoder_raw
from .suite_evidence import ROLES


def run_originals_storage_control(*, root, coverage_archive_path):
    root = _root(root)
    if any(root.iterdir()):
        raise ValueError('fresh EMPTY original storage fixture root required')
    receipt_root, binding_root, index_root = [root / name for name in ('originals', 'bindings', 'index')]
    for path in (receipt_root, binding_root, index_root):
        path.mkdir(mode=0o700)
    paths, expected, original_bytes = {}, {}, {}
    package = Path(__file__).parent
    for label, source, name in (('catalog', package / 'coverage-source-B.json', 'catalog.json'),
                                ('profile', package / 'coverage.ini', 'coverage.ini'),
                                ('archive', Path(coverage_archive_path), 'coverage.tar.gz')):
        snapshot = _snapshot(source)
        destination = binding_root / name
        _exclusive_copy(source, destination, snapshot)
        paths[label] = str(destination)
        expected[label] = {'bytes': snapshot['bytes'], 'sha256': snapshot['sha256']}
    raw, starts, finishes = _decoder_fixture()  # actual schema; SYNTHETIC process metadata
    spec = json.loads(raw)
    spec['output'] = str(receipt_root)
    spec['coverage_config'] = paths['profile']
    spec['coverage_config_sha256'] = expected['profile']['sha256']
    config_raw = _decoder_raw(spec)
    config_sha = hashlib.sha256(config_raw).hexdigest()
    for role in ROLES:
        data = ('SYNTHETIC_NON_SQLITE_DATA:' + role + '\n').encode()
        name = 'coverage-pid-' + str(finishes[role]['pid']) + '.data'
        original_bytes[role + '.data'] = data
        paths[role + '.data'] = str(receipt_root / name)
        for phase, rows in (('start', starts), ('finish', finishes)):
            row = rows[role]
            row['spec'] = deepcopy(spec)
            row['config_sha256'] = config_sha
            row['coverage']['config_sha256'] = config_sha
            if phase == 'finish':
                row['coverage_data'].update(file=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            label = role + '.' + phase
            original_bytes[label] = _decoder_raw(row)
            paths[label] = str(receipt_root / (role + '.' + phase + '.json'))
    original_bytes['config'] = config_raw
    paths['config'] = str(binding_root / 'config.json')
    for label, raw in original_bytes.items():
        with Path(paths[label]).open('xb') as stream:
            stream.write(raw)
        expected[label] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    owner = receipt_root / 'owner.json'
    owner_before = _decoder_raw({k: spec[k] for k in ('run_id', 'pin', 'arm')})
    with owner.open('xb') as stream:
        stream.write(owner_before)
    limits = {'file_bytes': {label: (DECODE_LIMITS['bytes'] if label == 'config' or
                                     label.endswith(('.start', '.finish')) else expected[label]['bytes'])
                              for label in collector.LABELS},
              'total_bytes': sum(v['bytes'] for v in expected.values()) + len(owner_before),
              'path_bytes': 4096, 'inventory_entries': 32, 'metadata_bytes': 4096}
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
                moved_archive = receipt_root / 'gw7.finish.json-wal'
                Path(paths['archive']).rename(moved_archive)
                bad_paths['archive'] = str(moved_archive)
            else:
                extra = receipt_root / ('controller.finish.json.partial' if label == 'partial' else 'index.json')
                with extra.open('xb') as stream:
                    stream.write(b'UNCERTAIN_PRESERVE\n')
            try:
                try:
                    collector.observe_originals(paths=bad_paths, receipt_root=str(receipt_root), limits=bad_limits)
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
    # One actual31 observation -> strict decoder/full reducer -> public index
    # publication. Replaces the old standalone observation positive and lower
    # private-index positive, retaining both sets of independent oracles.
    index_limits = {'nodes': 10000, 'depth': 64, 'string_characters': 10000, 'wire_bytes': 200000}
    result = publish_original_index(paths=paths, receipt_root=str(receipt_root), index_root=str(index_root),
                                    limits=limits, json_limits=DECODE_LIMITS, index_limits=index_limits,
                                    actual_exit=0, outer_seconds=10.0, cohort=NODES)
    collection = result['collection']
    value, decoded = collection['originals'], collection['decoded']
    assert set(value['observations']) == set(collector.LABELS)
    assert set(value['raw']) == {'config'} | {r + '.' + p for r in ROLES for p in ('start', 'finish')}
    for label, expectation in expected.items():
        observation = value['observations'][label]
        assert observation['path'] == paths[label]
        assert observation['bytes'] == expectation['bytes']
        assert observation['sha256'] == expectation['sha256']
        info = Path(paths[label]).stat()
        assert (observation['dev'], observation['ino'], observation['mtime_ns'], observation['ctime_ns']) == (
            info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
        if label in value['raw']:
            assert value['raw'][label] == original_bytes[label]
    assert decoded['receipt_logic']['node_outcomes'] == {NODES[0]: 'passed', NODES[1]: 'skipped'}
    assert value['source_process_or_closed_writer_authenticated'] is False
    assert decoded['source_process_or_closed_writer_authenticated'] is False
    assert result['source_process_closed_writer_or_measurement_accepted'] is False
    index_raw = (index_root / 'index.json').read_bytes()
    independent_roles = {}
    for role in ROLES:
        independent_roles[role] = {}
        for phase in ('start', 'finish'):
            label = role + '.' + phase
            info = Path(paths[label]).stat()
            independent_roles[role][phase] = {
                'path': paths[label], 'dev': info.st_dev, 'ino': info.st_ino,
                'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
                'bytes': len(original_bytes[label]),
                'sha256': hashlib.sha256(original_bytes[label]).hexdigest()}
    expected_index = {'format': FORMAT, 'pin': spec['pin'], 'run_id': spec['run_id'],
                      'config_sha256': hashlib.sha256(config_raw).hexdigest(),
                      'roles': independent_roles}
    assert json.loads(index_raw) == expected_index
    assert result['index_observation']['sha256'] == hashlib.sha256(index_raw).hexdigest()
    assert result['index_observation']['bytes'] == len(index_raw)
    try:
        publish_original_index(paths=paths, receipt_root=str(receipt_root), index_root=str(index_root),
                               limits=limits, json_limits=DECODE_LIMITS, index_limits=index_limits,
                               actual_exit=0, outer_seconds=10.0, cohort=NODES)
    except ValueError as error:
        assert 'unexpected original' in str(error)
    else:
        raise AssertionError('existing early index overwritten/reused')
    assert (index_root / 'index.json').read_bytes() == index_raw
    assert owner.read_bytes() == owner_before
    assert all(Path(paths[label]).read_bytes() == raw for label, raw in original_bytes.items())
    for label in ('catalog', 'profile', 'archive'):
        current = _snapshot(Path(paths[label]))
        assert current['bytes'] == expected[label]['bytes']
        assert current['sha256'] == expected[label]['sha256']
    return {'scope': 'ACTUAL31_TO_DECODER_INDEX_STORAGE_SYNTHETIC_PROCESSES_NOT_SQLITE_OR_PRODUCER_PROOF',
            'all_controls_unexecuted_at_authoring': True}
