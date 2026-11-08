"""Actual own-pin client / synthetic Unix transport controls, SOURCE ONLY.

UNEXECUTED. No CLI/job/root/provider or measurement launcher. The admitted
isolated owner supplies the actual source-bound native class, guard, empty
physical fixture/output directories, and inclusive setup deadline. These are
transport component controls, NOT actual runtime/Host/manager/startup proof.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
import weakref

from .native_peer_observer import NativePeerObserver

CASES = ('native_success', 'metadata_oserror', 'wrong_peer', 'native_unavailable_metadata_oserror',
         'wrong_uid', 'wrong_gid', 'wrong_instance', 'bool_protocol', 'malformed_instance',
         'missing_attestation')
# Independent expected rejection reasons; do not derive these from observer
# flags or call its metadata validator to manufacture the expected outcome.
REJECTION_REASON = {'metadata_oserror': 'peer_metadata', 'wrong_peer': 'peer_mismatch',
                    'wrong_uid': 'peer_mismatch', 'wrong_gid': 'peer_mismatch',
                    'wrong_instance': 'instance_changed', 'bool_protocol': 'bad_status',
                    'malformed_instance': 'bad_status', 'missing_attestation': 'bad_status'}
REQUEST = b'{"version":1,"method":"status","params":{}}\n'
INSTANCE = 'd' * 32
CASE_SECONDS = 4.0
REAP_RESERVE = 1.0
MAX_WIRE = 1024


def _select(primary, cleanup):
    # Fixture cleanup failures fail the control, never become a passed case.
    # Preserve original error identity; retain any subsequent cleanup failure.
    if primary is None:
        return cleanup
    prior = primary.__cause__
    if cleanup is not primary and prior is not cleanup:
        primary.__cause__ = (cleanup if prior is None else BaseExceptionGroup(
            'peer fixture cleanup failures', [prior, cleanup]))
    return primary


def _publish(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(raw) > 8192:
        raise ValueError('control receipt exceeds fixed byte cap')
    with path.open('xb') as stream:
        if stream.write(raw) != len(raw):
            raise OSError('short control receipt; preserve/no retry')
        stream.flush()
        os.fsync(stream.fileno())


def _finish_joined_case(*, server, joined, error, output, name, wire_response,
                        receipt, path, endpoint_identity):
    """Verify and publish after required cleanup; controls never start archives.

    The caller has already attempted hook restoration, listener close and join.
    An ordinary failed verification retains diagnostic evidence; a process
    control escapes immediately with roots preserved. No retry or native work.
    """
    if error is None:
        try:
            assert server['done'] and not server['errors'], 'synthetic server incomplete/failed'
            assert server['requests'] == [REQUEST], 'native wire/count changed'
        except BaseException as failure:
            error = _select(error, failure)
            if not isinstance(failure, Exception):
                raise error  # required cleanup already attempted; preserve/no optional I/O
    # Retain failed/partial raw input too. An alive worker means this is a
    # diagnostic prefix only, never complete history or a DRAINED receipt.
    raw_prefix = server['wire_prefix']
    try:
        for suffix, raw in (('request', raw_prefix), ('response-input', wire_response)):
            with (output / (name + '.' + suffix + '.bin')).open('xb') as stream:
                if stream.write(raw) != len(raw):
                    raise OSError('short raw control archive; preserve/no retry')
                stream.flush()
                os.fsync(stream.fileno())
        _publish(output / (name + '.json'), {'case': name, 'native_observation': receipt,
                 'request_prefix_bytes': len(raw_prefix),
                 'request_prefix_sha256': hashlib.sha256(raw_prefix).hexdigest(),
                 'response_input_sha256': hashlib.sha256(wire_response).hexdigest(),
                 'server_joined': joined, 'server_done': server['done'], 'server_errors': list(server['errors']),
                 'error_type': type(error).__name__ if error is not None else None,
                 'phase': 'CHECKS_AND_RAW_ARCHIVE_ONLY', 'endpoint_cleanup': 'NOT_YET_DONE',
                 'history': 'COMPLETE_SYNTHETIC_REQUEST' if joined and error is None else 'DIAGNOSTIC_PREFIX_ONLY',
                 'scope': 'native client/synthetic transport only; no root/startup/drain proof'})
    except BaseException as failure:
        error = _select(error, failure)
    if error is not None:
        raise error  # preserve endpoint/archives; do not launch next case
    info = path.lstat()
    if (info.st_dev, info.st_ino) != endpoint_identity or not stat.S_ISSOCK(info.st_mode):
        raise ValueError('endpoint ownership changed; preserve/no next case')
    path.unlink()  # only after raw receipt/archive + positive owned thread exit
    _publish(output / (name + '.terminal.json'), {
        'case': name, 'owned_server_joined': True, 'owned_endpoint_removed': True,
        'result': 'EXPECTED_COMPONENT_OUTCOME_AND_OWNED_CLEANUP',
        'scope': 'this synthetic server only; not remote/native-root/all-descendant drain'})


def run_native_transport_controls(*, native_class, source_file, root, output, absolute_deadline):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source-only until exact admission')
    root, output = Path(root), Path(output)
    for path in (root, output):
        if (not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
                or not path.is_dir() or any(path.iterdir())):
            raise ValueError('fresh exact physical owned control/output directory required')
        info = path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('control directories must already be private/owned; no permission repair')
    if root == output or root.is_relative_to(output) or output.is_relative_to(root):
        raise ValueError('control/output roots overlap')
    if (type(absolute_deadline) not in (int, float) or not math.isfinite(absolute_deadline)
            or sys.implementation.name != 'cpython' or sys.getprofile() is not None or sys.gettrace() is not None):
        raise ValueError('finite admitted deadline and isolated uninstrumented CPython control process required')
    if os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        raise ValueError('credential-changing interpreter context not qualified')
    # No old instrumentation is replaced/chained. These hooks are confined to
    # the current control thread; server thread runs ordinary socket code.
    original_profile, original_trace = sys.getprofile(), sys.gettrace()
    results = []
    for name in CASES:
        began = time.monotonic()
        if absolute_deadline - began < CASE_SECONDS:
            raise TimeoutError('insufficient inclusive setup budget; no next case/retry')
        case_end = min(absolute_deadline, began + CASE_SECONDS)
        io_end = case_end - REAP_RESERVE
        path = root / (name + '.sock')
        if len(os.fsencode(path)) >= 100:
            raise ValueError('declared Unix endpoint too long; no alternate/fallback')
        response = ({'ok': False, 'status': 503, 'error': 'controlled unavailable'}
                    if name == 'native_unavailable_metadata_oserror' else
                    {'ok': True, 'result': {'protocol_version': 1, 'instance': INSTANCE, 'attestation': None}})
        if name == 'wrong_instance':
            response['result']['instance'] = 'e' * 32
        elif name == 'bool_protocol':
            response['result']['protocol_version'] = True  # True == 1 is not valid typed evidence
        elif name == 'malformed_instance':
            response['result']['instance'] = 'not-a-native-instance'
        elif name == 'missing_attestation':
            del response['result']['attestation']
        wire_response = (json.dumps(response, separators=(',', ':')) + '\n').encode('ascii')
        listener = None
        worker = None
        endpoint_identity = None
        server = {'requests': [], 'wire_prefix': b'', 'errors': [], 'done': False}
        observed = {'connect_returns': [], 'peer_ids': [], 'status_calls': 0,
                    'status_return_ids': [], 'native_errors': [], 'identity_unknown': False}
        receipt, error, returned, selected = None, None, None, None
        control_pending = False
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('owned control socket identity/type differs')
            endpoint_identity = (info.st_dev, info.st_ino)
            listener.listen(1)

            def left():
                remaining = io_end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('control server absolute I/O end')
                return remaining

            def serve_once():
                try:
                    listener.settimeout(left())
                    connection, _ = listener.accept()
                    with connection:
                        raw = b''
                        while not raw.endswith(b'\n'):
                            connection.settimeout(left())
                            part = connection.recv(MAX_WIRE - len(raw))
                            if not part:
                                raise EOFError('native client ended before one complete request')
                            raw += part
                            server['wire_prefix'] = raw
                            if len(raw) >= MAX_WIRE and not raw.endswith(b'\n'):
                                raise ValueError('bounded request incomplete')
                        server['requests'].append(raw)
                        connection.settimeout(left())
                        connection.sendall(wire_response)
                except BaseException as failure:
                    server['errors'].append(type(failure).__name__[:64])
                finally:
                    server['done'] = True

            worker = threading.Thread(target=serve_once, name='937-' + name, daemon=False)
            worker.start()
            client = native_class(path, timeout=0.5)
            expected_pid = os.getpid() + (1 if name == 'wrong_peer' else 0)
            # Deliberately wrong declarations only; actual credentials and
            # native socket/response remain untouched. XOR stays in uint32.
            expected_uid = os.getuid() ^ (1 if name == 'wrong_uid' else 0)
            expected_gid = os.getgid() ^ (1 if name == 'wrong_gid' else 0)
            observer = NativePeerObserver(client, native_class=native_class, source_file=source_file,
                                          expected_peer=(expected_pid, expected_uid, expected_gid),
                                          expected_instance=INSTANCE)
            read_peer = observer._read_peer

            def peer_read(native_socket):
                observed['peer_ids'].append(id(native_socket))
                if name in ('metadata_oserror', 'native_unavailable_metadata_oserror'):
                    raise OSError('controlled metadata failure')
                return read_peer(native_socket)

            observer._read_peer = peer_read
            connect_code = native_class._connect.__code__
            status_code = native_class.status_within.__code__
            decode_code = native_class._status.__code__

            def profile(frame, event, arg):
                if frame.f_code is connect_code and event == 'return' and arg is not None:
                    # Weak references + IDs only: never keep a socket/frame.
                    try:
                        observed['connect_returns'].append((id(arg), weakref.ref(arg)))
                    except TypeError:
                        observed['identity_unknown'] = True
                if frame.f_code is status_code:
                    if event == 'call':
                        observed['status_calls'] += 1
                    elif event == 'return' and type(arg) is dict:
                        observed['status_return_ids'].append(id(arg))

            def trace(frame, event, arg):
                if frame.f_code is decode_code:
                    if event == 'exception' and isinstance(arg[1], Exception):
                        # Weak reference only; no exception/traceback/frame is
                        # retained by the callback after handoff. Unsupported
                        # identity evidence fails the control, not the native RPC.
                        try:
                            observed['native_errors'].append(weakref.ref(arg[1]))
                        except TypeError:
                            observed['identity_unknown'] = True
                    return trace
                return None

            sys.setprofile(profile)
            sys.settrace(trace)
            try:
                try:
                    returned = observer.status_within(0.5)
                except Exception as native_error:
                    selected = native_error
            finally:
                sys.settrace(original_trace)
                sys.setprofile(original_profile)
                observer._read_peer = read_peer
            receipt = observer.receipt()
            assert not observed['identity_unknown'], 'runtime/socket weak identity unsupported'
            assert observed['status_calls'] == 1, 'native public call count changed'
            assert len(observed['connect_returns']) == 1, 'native returned socket count changed'
            assert observed['peer_ids'] == [observed['connect_returns'][0][0]], 'peer read used another socket'
            assert observer.connect_calls == observer.socket_returns == 1, 'observer delegation counts changed'
            assert observer.close_attempts == 0, 'ordinary path closed native-owned socket'
            assert '_connect' not in client.__dict__ and receipt['binding_restored'], 'native instance not restored'
            if name == 'native_unavailable_metadata_oserror':
                assert selected is not None and len(observed['native_errors']) == 1
                assert observed['native_errors'][0]() is selected, 'actual native primary lost/replaced'
                assert type(selected).__name__ == 'RuntimeUnavailable', 'wrong native exception/control shape'
                assert receipt['native_outcome'] == 'ERROR' and receipt['observation'] == 'UNKNOWN'
            else:
                assert selected is None, 'unexpected native/control error'
                assert observed['status_return_ids'] == [id(returned)], 'original native result object replaced'
                assert returned == response['result'], 'native response changed'
                expected = 'CAPTURED' if name == 'native_success' else 'UNKNOWN'
                assert receipt['observation'] == expected, 'metadata fault incorrectly qualified'
                reason = REJECTION_REASON.get(name)
                assert receipt['reasons'] == ([] if reason is None else [reason]), 'wrong rejection path'
                assert receipt['full_readiness'] is False
                if name in ('bool_protocol', 'malformed_instance', 'missing_attestation'):
                    # Invalid shape returns before the observer has qualified
                    # status instance/attestation; do not manufacture absence.
                    assert receipt['instance'] is None and receipt['attestation_absent'] is None
                else:
                    assert receipt['attestation_absent'] is True
                if name in ('wrong_uid', 'wrong_gid', 'wrong_instance', 'bool_protocol',
                            'malformed_instance', 'missing_attestation', 'native_success'):
                    assert receipt['peer'] == [os.getpid(), os.getuid(), os.getgid()], 'actual peer changed'
                if name == 'wrong_instance':
                    assert receipt['instance'] == 'e' * 32 and receipt['instance_compared'] is True
            ref = observed['connect_returns'][0][1]
            item = ref()
            try:
                assert item is None or item.fileno() == -1, 'native with did not close returned socket'
            finally:
                del item
        except BaseException as failure:
            error = failure
            control_pending = not isinstance(failure, Exception)
        finally:
            # Every cleanup is attempted once. Unknown lifetime => preserve the
            # endpoint and fail; never reopen/retry or claim all-descendant drain.
            for cleanup in (lambda: sys.settrace(original_trace), lambda: sys.setprofile(original_profile),
                            lambda: listener.close() if listener is not None else None,
                            lambda: worker.join(max(0.0, case_end - time.monotonic()))
                            if worker is not None and worker.ident is not None else None):
                try:
                    cleanup()
                except BaseException as failure:
                    error = _select(error, failure)
                    control_pending = control_pending or not isinstance(failure, Exception)
        if control_pending:
            # Required cleanup was attempted; no optional archive/probe after a
            # process control. Preserve roots/unknown lifetime for outer owner.
            raise error
        joined = False
        try:
            joined = worker is not None and not worker.is_alive()
            if worker is not None and not joined:
                error = _select(error, RuntimeError('UNKNOWN owned server thread; preserve/no next case'))
        except BaseException as failure:
            error = _select(error, failure)
            if not isinstance(failure, Exception):
                raise error
        _finish_joined_case(server=server, joined=joined, error=error, output=output,
                            name=name, wire_response=wire_response, receipt=receipt,
                            path=path, endpoint_identity=endpoint_identity)
        observed['native_errors'].clear()
        selected = None
        returned = None
        results.append({'case': name, 'result': 'EXPECTED_NATIVE_TRANSPORT_OUTCOME'})
    return {'cases': results, 'scope': 'actual own-pin client with synthetic owned server, NOT native runtime readiness'}
