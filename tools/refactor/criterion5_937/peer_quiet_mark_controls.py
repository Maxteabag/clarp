"""F1 diagnostic-priority control SOURCE, NOT EXECUTED/admitted.

LOWER TOOLING ADAPTER TIER: a controlled native_status slot supplies return/error
objects. This does NOT exercise actual native status transport, a socket,
kernel credentials, provider, root/startup or interpreter handoff gaps.
The outer admitted isolated control process supplies the OWN-pin native class.
"""
import os
import socket
from types import SimpleNamespace

from . import native_peer_observer as peer


def run_quiet_mark_controls(*, native_class, source_file, socket_path):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local control authority')
    # Constructor only; none of the native request/connect methods run.
    # The admitted control process must contain no concurrent observer activity.
    original_clock_module = peer.time
    outcomes = []
    try:
        for case in ('ordinary_primary', 'no_primary_restore_error', 'already_control'):
            clock_calls = []
            def clock():
                clock_calls.append(len(clock_calls) + 1)
                return len(clock_calls)
            # TOOLING module alias only, not stdlib or native product clocks.
            peer.time = SimpleNamespace(monotonic_ns=clock)
            client = native_class(socket_path, timeout=0.5)
            if case == 'already_control':
                client._connect = client._connect
            had_own = '_connect' in client.__dict__
            own_value = client.__dict__.get('_connect')
            observer = peer.NativePeerObserver(
                client, native_class=native_class, source_file=source_file,
                expected_peer=(os.getpid(), os.getuid(), os.getgid()))
            mark, restore, public = observer.mark, observer._restore, observer.native_status
            primary = (RuntimeError('controlled ordinary primary') if case == 'ordinary_primary'
                       else KeyboardInterrupt('controlled original cancellation') if case == 'already_control'
                       else None)
            prior_cause = ValueError('controlled original cause') if case == 'ordinary_primary' else None
            if primary is not None:
                primary.__cause__ = prior_cause
            diagnostic = SystemExit(7) if case == 'already_control' else KeyboardInterrupt('diagnostic cancellation')
            native_calls, injected = [], []
            reason = 'restore_failure' if case == 'no_primary_restore_error' else 'native_error'
            value = {'protocol_version': 1, 'instance': 'a' * 32, 'attestation': None}

            def controlled_public(*args, **kwargs):
                native_calls.append((args, kwargs))
                if primary is not None:
                    raise primary
                return value

            def faulty_mark(actual_reason):
                if actual_reason == reason and not injected:
                    injected.append(actual_reason)
                    raise diagnostic
                return mark(actual_reason)

            def ordinary_restore_error():
                restore()
                raise OSError('controlled post-restore diagnostic failure')

            observer.native_status = controlled_public
            observer.mark = faulty_mark
            if case == 'no_primary_restore_error':
                observer._restore = ordinary_restore_error
            selected = None
            try:
                try:
                    observer.status_within(0.5)
                except BaseException as error:
                    selected = error
                expected = diagnostic if primary is None else primary
                assert selected is expected, case + ': wrong selected primary/control'
                assert injected == [reason], case + ': diagnostic fault not reached exactly once'
                if prior_cause is not None:
                    assert isinstance(selected.__cause__, BaseExceptionGroup), case + ': prior cause/control not retained'
                    assert selected.__cause__.exceptions == (prior_cause, diagnostic), case + ': cause identities/order changed'
                elif primary is not None:
                    assert selected.__cause__ is diagnostic, case + ': secondary diagnostic control lost'
                assert observer.pending_control, case + ': cancellation not latched'
                assert clock_calls == [1], case + ': optional end clock ran after cancellation'
                assert ('_connect' in client.__dict__) == had_own, case + ': binding presence changed'
                if had_own:
                    assert client.__dict__['_connect'] is own_value, case + ': own value not restored'
                assert client._connect.__func__ is native_class._connect, case + ': native binding not restored'
                released = observer.lock.acquire(blocking=False)
                assert released, case + ': independent release did not happen'
                observer.lock.release()
                assert native_calls == [((0.5,), {})], case + ': delegated slot count/arguments changed'
                assert observer.connect_calls == observer.socket_returns == observer.close_attempts == 0
                assert observer.receipt()['observation'] == 'UNKNOWN', case + ': control accepted as captured'
                outcomes.append({'case': case, 'selected_type': type(selected).__name__,
                                 'diagnostic_type': type(diagnostic).__name__,
                                 'result': 'EXPECTED_PRIORITY_AND_RESTORE', 'socket_evidence': 'NONE'})
            finally:
                # Remove callback/exception closures. No native class, socket or
                # stdlib clock was patched. Assertions precede this cleanup.
                observer.mark = mark
                observer._restore = restore
                observer.native_status = public
    finally:
        peer.time = original_clock_module
    return {'cases': outcomes, 'tier': 'LOWER_TOOLING_ADAPTER_ONLY',
            'scope': 'F1 priority/restore sensitivity; no native transport/socket/startup proof'}
