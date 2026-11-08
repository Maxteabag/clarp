"""Whole-B report API STORAGE control SOURCE, UNEXECUTED.

Uses the real supplied API and actual frozen B SOURCE bytes for analysis, but
arcs are supplied storage input, NOT actual traced Python or nine pytest data.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket

from .coverage_copy_read import _retain, capture_closed_copy
from .coverage_render import render_report


def run_render_storage_control(*, coverage_module, source_root, root):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local report/control authority')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh physical owned storage-control root required')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned control root required')
    original_root, copy_root, input_root, output_root = [root / n for n in ('original', 'copy', 'input', 'output')]
    for path in (original_root, copy_root, input_root, output_root):
        path.mkdir(mode=0o700)
    original, copy = original_root / 'coverage-aggregate.data', copy_root / 'coverage-aggregate.data'
    producer = coverage_module.CoverageData(basename=str(original))
    primary = None
    try:
        producer.set_context('storage-control-only')
        producer.add_arcs({'server/server.py': {(-1, 1), (1, -1)}})
        producer.write()
    except BaseException as error:
        primary = error
    finally:
        try:
            producer.close()
        except BaseException as cleanup:
            primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary
    raw = original.read_bytes()
    shutil.copyfile(original, copy)
    limits = {'files': 1024, 'contexts': 1024, 'pairs': 8192, 'items': 100000,
              'name_bytes': 4096, 'total_name_bytes': 1024 * 1024}
    capture = capture_closed_copy(original_root=original_root, copy_root=copy_root,
                                  filename=original.name, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                  expected_bytes=len(raw), coverage_data_class=coverage_module.CoverageData,
                                  limits=limits)
    result = render_report(coverage_module=coverage_module, source_root=source_root,
                           aggregate_capture=capture, input_root=input_root,
                           output_root=output_root, limits=limits)
    # Static Git/source inventory is an independent required scope, not an
    # expected percentage copied from the observed report or analysis API.
    inventory = json.loads(Path(__file__).with_name('coverage-source-B.json').read_bytes())
    assert set(result['consistency']['files']) == set(inventory['files'])
    assert result['consistency']['source_file_count'] == 434
    assert result['consistency']['source_input_process_coverage_or_no_worse_accepted'] is False
    assert result['actual_run_input_producer_coverage_or_no_worse_accepted'] is False
    assert original.read_bytes() == raw and copy.read_bytes() == raw
    assert (output_root / 'coverage.json').is_file()
    return {'result': 'EXPECTED_PUBLIC_REPORT_STORAGE_SCOPE',
            'scope': 'real API/source-analysis only, supplied arcs NOT traced pytest or complete measurement'}
