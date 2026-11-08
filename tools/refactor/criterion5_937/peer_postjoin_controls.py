"""Post-join publication boundary controls: SOURCE ONLY, UNEXECUTED.

Lower tooling tier; does not instantiate a native client, listener or worker.
The actual transport owner calls the same publication boundary after required
cleanup. These cases do NOT independently prove that upstream cleanup ran.
No additional job or admission follows from authoring these finite cases.
"""
import json
from pathlib import Path
import socket
from types import SimpleNamespace

from . import peer_transport_controls as transport


def run_postjoin_controls(*, root):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local control authority')
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root
            or not root.is_dir() or any(root.iterdir())):
        raise ValueError('fresh exact physical owned boundary-control root required')
    info = root.stat()
    if info.st_uid != transport.os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private owned root required; no permission repair')
    results = []
    for name in ('keyboard_interrupt', 'system_exit', 'ordinary_assertion'):
        case = root / name
        case.mkdir(mode=0o700)
        output = case / 'out'
        output.mkdir(mode=0o700)
        # A retained marker, NOT a socket/lifetime positive. Neither failed
        # path may reach endpoint inspection/removal, irrespective of its type.
        endpoint = case / 'preserve-endpoint'
        endpoint.write_bytes(b'owned boundary marker')
        identity = endpoint.stat()
        primary = (KeyboardInterrupt('post-join verification cancellation') if name == 'keyboard_interrupt'
                   else SystemExit(9) if name == 'system_exit'
                   else AssertionError('post-join wire verification failed'))
        original_cause = ValueError('retained preexisting cause')
        primary.__cause__ = original_cause
        events = []
        raw = b'failed bounded request prefix\n'
        response = b'{"ok":true,"result":{}}\n'

        class FaultAtVerification(dict):
            def __getitem__(self, key):
                if key == 'done' and 'verification' not in events:
                    events.append('verification')
                    raise primary
                events.append('read:' + key)
                return super().__getitem__(key)

        server = FaultAtVerification(done=True, wire_prefix=raw, errors=[], requests=[])
        # Scope is the TOOL module alias only; no global os/native patch. The
        # admitted owner must run this lower tier in an isolated interpreter.
        original_os, original_publish = transport.os, transport._publish
        syncs, publications = [], []

        def counted_sync(fd):
            syncs.append(fd)
            return original_os.fsync(fd)

        def counted_publish(path, value):
            publications.append(path.name)
            return original_publish(path, value)

        transport.os = SimpleNamespace(fsync=counted_sync)
        transport._publish = counted_publish
        selected = None
        try:
            try:
                transport._finish_joined_case(
                    server=server, joined=True, error=None, output=output, name=name,
                    wire_response=response, receipt=None, path=endpoint,
                    endpoint_identity=(identity.st_dev, identity.st_ino))
                events.append('continued-after-boundary')
            except BaseException as failure:
                selected = failure
        finally:
            transport._publish = original_publish
            transport.os = original_os
        assert selected is primary, name + ': original verification error/control replaced'
        assert selected.__cause__ is original_cause, name + ': original cause lost'
        assert 'continued-after-boundary' not in events, name + ': caller advanced after failed boundary'
        retained = endpoint.stat()
        assert (retained.st_dev, retained.st_ino) == (identity.st_dev, identity.st_ino)
        assert endpoint.read_bytes() == b'owned boundary marker', name + ': endpoint changed'
        if name != 'ordinary_assertion':
            assert events == ['verification'], name + ': optional reads after cancellation'
            assert syncs == publications == [], name + ': optional publication/fsync after cancellation'
            assert list(output.iterdir()) == [], name + ': optional archive started'
        else:
            assert (output / (name + '.request.bin')).read_bytes() == raw
            assert (output / (name + '.response-input.bin')).read_bytes() == response
            receipt = json.loads((output / (name + '.json')).read_text())
            assert receipt['error_type'] == 'AssertionError'
            assert receipt['history'] == 'DIAGNOSTIC_PREFIX_ONLY'
            assert receipt['endpoint_cleanup'] == 'NOT_YET_DONE'
            assert publications == [name + '.json'] and len(syncs) == 3
            assert not (output / (name + '.terminal.json')).exists(), 'failure published success terminal'
        results.append({'case': name, 'result': 'EXPECTED_BOUNDARY_OUTCOME',
                        'tier': 'POSTJOIN_TOOLING_ONLY', 'actual_worker_cleanup_proof': False})
    return {'cases': results, 'scope': 'verification/publication boundary only; no native transport or worker proof'}
