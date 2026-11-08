"""One-shot own-pin native status/socket observation; SOURCE ONLY/UNEXECUTED.

No product import, socket creation, RPC encoding, timeout policy or root launch.
The admitted owner supplies the actual native class/client and immutable source
and launch bindings. Filename/type checks below do NOT prove loaded-code/source
authenticity. Parent PID/start/boot/namespace and socket-path evidence remain
separate. No result from this component is full readiness or drain proof.
"""
import re
import socket
import struct
import sys
import threading
import time
from pathlib import PurePosixPath

INSTANCE = re.compile(r'[0-9a-f]{32}\Z')
UCRED = struct.Struct('@iII')  # Linux pid_t/int32, uid_t/uint32, gid_t/uint32
REASONS = ('unexpected_use', 'binding_conflict', 'restore_failure', 'peer_metadata',
           'peer_mismatch', 'multiple_connects', 'native_error', 'bad_status',
           'instance_changed', 'socket_cleanup_unknown')


class ObserverOwnershipRefusal(RuntimeError):
    """Tool-owned preflight/concurrency refusal, never a product finding."""


class NativePeerObserver:
    """ONE status_within call; native return/primary and socket stay native.

    Only this instance's _connect attribute is scoped, never the class/socket.
    Preexisting own binding is accepted only when it resolves to the exact
    native _connect method; arbitrary stand-ins are not qualified clients.
    Reuse/concurrency is UNKNOWN, not a new native wait or an observer retry.
    """
    def __init__(self, client, *, native_class, source_file, expected_peer,
                 expected_instance=None):
        source_file = str(source_file)
        path = PurePosixPath(source_file)
        if (type(client) is not native_class or native_class.__name__ != 'RuntimeClient'
                or native_class.__module__ != 'lib.runtime_bridge'
                or not path.is_absolute() or str(path) != source_file
                or '..' in path.parts):
            raise ValueError('actual own-pin native client/source declaration required')
        for name in ('_connect', '_request', 'status_within'):
            method = getattr(native_class, name)
            if str(PurePosixPath(method.__code__.co_filename)) != source_file:
                raise ValueError('native client method/source path differs')
        original = client._connect
        if (getattr(original, '__self__', None) is not client
                or getattr(original, '__func__', None) is not native_class._connect):
            raise ValueError('connect binding is not the declared actual native method')
        for name in ('_request', 'status_within'):
            bound = getattr(client, name)
            if (getattr(bound, '__self__', None) is not client
                    or getattr(bound, '__func__', None) is not getattr(native_class, name)):
                raise ValueError('public/request binding is not the actual native method')
        if (type(expected_peer) is not tuple or len(expected_peer) != 3
                or any(type(v) is not int for v in expected_peer)
                or not 1 < expected_peer[0] < 2**31
                or any(not 0 <= v < 2**32 for v in expected_peer[1:])):
            raise ValueError('declared parent-bound pid/uid/gid required')
        if expected_instance is not None and (type(expected_instance) is not str
                                               or INSTANCE.fullmatch(expected_instance) is None):
            raise ValueError('declared previous native instance malformed')
        self.client = client
        self.native_connect = original
        self.native_status = client.status_within
        self.expected_peer = expected_peer
        self.expected_instance = expected_instance
        self.source_file = source_file
        self.owner_thread = threading.get_ident()
        self.lock = threading.Lock()
        self.had_own = '_connect' in client.__dict__
        self.prior_own = client.__dict__.get('_connect')
        self.used = False
        self.active = False
        self.unknown = False
        self.flags = 0
        self.connect_calls = 0
        self.socket_returns = 0
        self.peer = None
        self.instance = None
        self.attestation_absent = None
        self.return_intent = False
        self.close_attempts = 0
        self.restore_complete = False
        self.pending_control = False
        self.observer_control = False
        self.diagnostic_control_type = None
        self.ownership_refusal = False
        self.outcome = 'NOT_STARTED'
        self.error_type = None
        self.started_ns = None
        self.ended_ns = None
        # A stored function (not a class descriptor) receives exactly native
        # _request's args. Restore the precise absent-vs-own state afterwards.
        self.wrapper = self._observed_connect

    def mark(self, reason):
        self.unknown = True
        try:
            self.flags |= 1 << REASONS.index(reason)
        except Exception:
            pass  # UNKNOWN is already sticky; missing reason cannot be green

    def _quiet_mark(self, reason):
        # Return, never discard, a diagnostic control. The explicit original
        # primary belongs to the caller, NOT sys.exc_info() in a cleanup handler.
        # No exception/frame/socket object is saved on this observer or receipt.
        self.unknown = True
        try:
            self.mark(reason)
        except Exception:
            return None
        except BaseException as control:
            self.pending_control = True
            self.diagnostic_control_type = type(control).__name__[:64]
            return control
        return None

    def _retain_control(self, primary, control):
        self.pending_control = True
        if primary is None:
            self.observer_control = True
            return control
        if control is not primary and primary.__cause__ is not control:
            prior = primary.__cause__
            # Preserve the exact selected primary and prior cause objects.
            # A group retains both when a native explicit cause already exists;
            # it is a cause, never a new top-level primary/result or receipt.
            primary.__cause__ = (control if prior is None else BaseExceptionGroup(
                'observer cleanup control with prior cause', [prior, control]))
        return primary

    def _diagnostic(self, reason, *, primary, failure=None):
        """Explicit selection: existing primary wins; otherwise first control.

        Ordinary metadata/cleanup errors never replace a native result/primary.
        Cleanup's control predates any new control during its diagnostic mark.
        """
        if primary is not None and not isinstance(primary, Exception):
            self.pending_control = True
        if failure is not None and not isinstance(failure, Exception):
            primary = self._retain_control(primary, failure)
        control = self._quiet_mark(reason)
        if control is not None:
            primary = self._retain_control(primary, control)
        return primary

    def _refuse(self, reason, message):
        control = self._diagnostic(reason, primary=None)
        if control is not None:
            raise control
        self.ownership_refusal = True
        raise ObserverOwnershipRefusal(message)

    def _read_peer(self, native_socket):
        if (sys.platform != 'linux' or not hasattr(socket, 'SO_PEERCRED')
                or UCRED.size != 12 or struct.calcsize('=iII') != 12):
            raise ValueError('unsupported Linux credentials ABI')
        raw = native_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, UCRED.size)
        if type(raw) is not bytes or len(raw) != 12:
            raise ValueError('missing/malformed peer credentials')
        peer = UCRED.unpack(raw)
        if not 1 < peer[0] < 2**31 or any(not 0 <= v < 2**32 for v in peer[1:]):
            raise ValueError('peer credentials outside native ranges')
        return peer

    def _observed_connect(self, *args, **kwargs):
        # A private-client ownership violation stops before allocating a new
        # socket. It is tool-owned, not a native failure or a new native wait.
        if not self.active or threading.get_ident() != self.owner_thread:
            self._refuse('unexpected_use', 'unexpected private-client use')
        self.connect_calls += 1
        if self.connect_calls != 1:
            self._refuse('multiple_connects', 'multiple connect delegations refused')
        native_socket = None
        try:
            # Cover delegation/assignment itself as well as metadata. Until a
            # returned object is bound locally, original native/interpreter
            # cleanup applies; the return-to-assignment gap is NOT certified.
            native_socket = self.native_connect(*args, **kwargs)
            self.socket_returns += 1
            try:
                self.peer = self._read_peer(native_socket)
                if self.peer != self.expected_peer:
                    self.mark('peer_mismatch')
            except Exception:
                self.mark('peer_metadata')
            self.return_intent = True
            return native_socket  # SAME object, no socket proxy/extra RPC
        except BaseException as primary:
            if native_socket is None:
                # No known returned socket to close/retry. Preserve original
                # connect failure or unqualified interpreter-boundary control.
                raise
            # Control prevents handoff. Close this exact owned object ONCE.
            # A close error/control cannot replace the pending primary. No
            # raw-fd fallback or repeated cleanup; ownership then UNKNOWN.
            if not isinstance(primary, Exception):
                self.pending_control = True
                self.observer_control = True
            self.close_attempts += 1
            try:
                native_socket.close()
            except BaseException as cleanup:
                primary = self._diagnostic('socket_cleanup_unknown', primary=primary, failure=cleanup)
            raise

    def _restore(self):
        if self.client.__dict__.get('_connect') is not self.wrapper:
            self.mark('binding_conflict')
            return  # do not clobber an unexpected actor's replacement
        if self.had_own:
            self.client.__dict__['_connect'] = self.prior_own
        else:
            del self.client.__dict__['_connect']
        if (('_connect' in self.client.__dict__) != self.had_own
                or (self.had_own and self.client.__dict__['_connect'] is not self.prior_own)):
            self.mark('restore_failure')
            return
        restored = self.client._connect
        if (getattr(restored, '__self__', None) is not self.client
                or getattr(restored, '__func__', None) is not self.native_connect.__func__):
            self.mark('restore_failure')
            return
        self.restore_complete = True

    def _status_metadata(self, value):
        if (type(value) is not dict or type(value.get('protocol_version')) is not int
                or value['protocol_version'] != 1 or type(value.get('instance')) is not str
                or INSTANCE.fullmatch(value['instance']) is None or 'attestation' not in value):
            self.mark('bad_status')
            return
        self.instance = value['instance']
        self.attestation_absent = value.get('attestation') is None
        if self.expected_instance is not None and self.instance != self.expected_instance:
            self.mark('instance_changed')

    def status_within(self, *args, **kwargs):
        """Delegate the actual public method once with its exact arguments."""
        if not self.lock.acquire(blocking=False):
            self._refuse('unexpected_use', 'concurrent observer use refused')
        primary = None
        value = None
        binding_attempted = False
        try:
            try:
                if self.used or threading.get_ident() != self.owner_thread:
                    self._refuse('unexpected_use', 'one-shot/owner-thread contract violated')
                self.used = True
                if (('_connect' in self.client.__dict__) != self.had_own
                        or (self.had_own and self.client.__dict__['_connect'] is not self.prior_own)
                        or self.client._connect != self.native_connect):
                    self._refuse('binding_conflict', 'private client binding changed before call')
                # Finally covers the binding mutation itself, not just the
                # native call. No metadata call sits outside restoration.
                binding_attempted = True
                self.client.__dict__['_connect'] = self.wrapper
                self.active = True
                try:
                    self.started_ns = time.monotonic_ns()
                except BaseException as metadata:
                    primary = self._diagnostic('peer_metadata', primary=primary, failure=metadata)
                if primary is None:
                    try:
                        value = self.native_status(*args, **kwargs)
                    except BaseException as error:
                        self.outcome = 'ERROR'
                        self.error_type = type(error).__name__[:64]
                        primary = self._diagnostic('native_error', primary=error)
                    else:
                        self.outcome = 'RETURNED'
                        try:
                            self._status_metadata(value)
                        except BaseException as metadata:
                            primary = self._diagnostic('bad_status', primary=primary, failure=metadata)
            except BaseException as error:
                # Installation/preflight or another helper control: preserve
                # this exact object, never infer a native product failure.
                primary = error
                if not isinstance(error, Exception):
                    self.pending_control = True
                    self.observer_control = True
        finally:
            try:
                self.active = False
                if binding_attempted:
                    try:
                        self._restore()
                    except BaseException as cleanup:
                        primary = self._diagnostic('restore_failure', primary=primary, failure=cleanup)
                    if not self.pending_control:
                        try:
                            self.ended_ns = time.monotonic_ns()
                        except BaseException as cleanup:
                            primary = self._diagnostic('peer_metadata', primary=primary, failure=cleanup)
            finally:
                # Independent release even if restoration/diagnostics fail.
                try:
                    self.lock.release()
                except BaseException as cleanup:
                    primary = self._diagnostic('restore_failure', primary=primary, failure=cleanup)
        if primary is not None:
            raise primary
        return value  # exact original native return object

    def receipt(self):
        """Primitive observation only; never a readiness/source/drain verdict."""
        complete = (self.outcome == 'RETURNED' and self.restore_complete
                    and self.connect_calls == 1 and self.socket_returns == 1
                    and self.peer is not None and self.instance is not None
                    and self.return_intent and self.close_attempts == 0
                    and not self.pending_control and not self.unknown)
        return {'observation': 'CAPTURED' if complete else 'UNKNOWN',
                'reasons': [name for i, name in enumerate(REASONS) if self.flags & (1 << i)],
                'native_outcome': self.outcome, 'native_error_type': self.error_type,
                'call_error_origin': ('OBSERVER_OWNERSHIP_REFUSAL' if self.ownership_refusal
                                      else 'OBSERVER_CONTROL' if self.observer_control
                                      else 'NATIVE_OR_UNCLASSIFIED' if self.outcome == 'ERROR' else None),
                'pending_control_seen': self.pending_control,
                'diagnostic_control_type': self.diagnostic_control_type,
                'connect_calls': self.connect_calls, 'socket_returns': self.socket_returns,
                'peer': list(self.peer) if self.peer is not None else None,
                'instance': self.instance, 'instance_compared': self.expected_instance is not None,
                'attestation_absent': self.attestation_absent,
                'wrapper_return_intent': self.return_intent, 'owned_close_attempts': self.close_attempts,
                'binding_restored': self.restore_complete,
                'started_ns': self.started_ns, 'ended_ns': self.ended_ns,
                'source_path_declaration': self.source_file,
                'scope': 'socket observation only; parent/source/namespace/instance brackets still owed',
                'handoff_and_drain': 'NOT_CERTIFIED; connect-return/assignment and native-with gaps/outer containment owed',
                'full_readiness': False}
