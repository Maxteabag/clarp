"""Linux direct-child group custody. No waitpid until the last signal decision."""
import os
import hashlib
import stat
import math
from dataclasses import dataclass
from pathlib import Path
import signal
import subprocess
import time

class CustodyUnknown(RuntimeError):
    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.child_evidence = evidence or {}

@dataclass(frozen=True)
class DispatchPermit:
    deadline: float
    owner: object

    def check(self):
        if not math.isfinite(self.deadline) or time.monotonic() >= self.deadline:
            raise CustodyUnknown('work_permit_expired_before_exec')
        if self.owner.abort_requested or signal.sigpending() & {signal.SIGTERM, signal.SIGINT}:
            raise CustodyUnknown('work_permit_aborted_before_exec')

class Owned:
    def __init__(self, argv, output, env, native_sources=None, work_permit=None):
        self.p = None
        self.identity = None
        self.reap_started = False
        self.terminal = None
        self.cleanup_started = False
        self.signals = []
        self.errors = []
        self.privileged = False
        self.gate_released = False
        self.source_bindings = []
        if argv[0] == '/usr/bin/sudo':
            allowed = [
                ['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'status', '/'],
                ['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'cancel', '/'],
                ['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'start', '-dusage=10', '/'],
                ['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'balance', 'start', '-dusage=25', '/'],
                ['/usr/bin/sudo', '-n', '/usr/bin/btrfs', 'filesystem', 'usage', '-b', '/'],
            ]
            if argv not in allowed or native_sources is None:
                raise CustodyUnknown('sudo_command_or_native_binding')
            for name in ('/usr/bin/sudo', '/usr/bin/btrfs'):
                path = Path(name)
                before = path.stat()
                expected = native_sources[name]
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 32 * 1024**2:
                    raise CustodyUnknown('native_binary_type_or_size')
                raw = path.read_bytes()
                after = path.stat()
                observed = (after.st_dev, after.st_ino, after.st_size,
                            after.st_mtime_ns, after.st_ctime_ns, after.st_uid, after.st_mode)
                original = (before.st_dev, before.st_ino, before.st_size,
                            before.st_mtime_ns, before.st_ctime_ns, before.st_uid, before.st_mode)
                if observed != original or not stat.S_ISREG(after.st_mode) or after.st_uid != 0 or after.st_mode & 0o022:
                    raise CustodyUnknown('native_binary_identity_mode')
                if hashlib.sha256(raw).hexdigest() != expected['sha256'] or list(observed) != expected['stat_version']:
                    raise CustodyUnknown('native_binary_binding_changed')
                self.source_bindings.append((name, observed, expected['sha256']))
            self.privileged = True
        read_fd = write_fd = None
        blocked = None
        primary = None
        cleanup_errors = []
        try:
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
            intended = ','.join(str(int(x)) for x in sorted(set(blocked) - {signal.SIGTERM, signal.SIGINT}))
            trampoline = str(Path(__file__).with_name('child_exec.py').resolve(strict=True))
            deadline = repr(work_permit.deadline) if work_permit is not None else '-'
            self.p = subprocess.Popen(['/usr/bin/python3', '-I', '-S', '-B', trampoline,
                                       str(read_fd), intended, deadline] + argv,
                                      stdout=output, stderr=subprocess.STDOUT, env=env,
                                      start_new_session=True, pass_fds=(read_fd,))
            self.identity = self.observe()
            if self.initial_uid != os.getuid():
                raise CustodyUnknown('gate_initial_uid')
            # AFTER all hash/setup/birth prerequisites, BEFORE the exec capability.
            if work_permit is not None:
                work_permit.check()
            os.write(write_fd, b'G')
            self.gate_released = True
        except BaseException as exc:
            primary = exc
        finally:
            # Known FDs attempted exactly once; an uncertain close is NOT retried.
            try:
                for label, fd in (('read', read_fd), ('write', write_fd)):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except BaseException as exc:
                            cleanup_errors.append(label + '_close:' + type(exc).__name__ + ':' + str(exc))
            finally:
                if blocked is not None:
                    try:
                        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
                    except BaseException as exc:
                        cleanup_errors.append('mask_restore:' + type(exc).__name__ + ':' + str(exc))
        if primary is not None or cleanup_errors:
            evidence = {'pid': self.p.pid if self.p else None, 'birth': self.identity,
                        'gate_released': self.gate_released, 'source_bindings': self.source_bindings,
                        'primary': type(primary).__name__ + ':' + str(primary) if primary else None,
                        'cleanup_errors': cleanup_errors,
                        'outcome': 'LAUNCH_CUSTODY_UNKNOWN_NOT_CLEANUP_PROOF'}
            if self.identity is not None:
                try:
                    self.cleanup()
                except BaseException as exc:
                    evidence['child_cleanup_error'] = type(exc).__name__ + ':' + str(exc)
            self.errors.append(evidence)
            raise CustodyUnknown('launch_or_cleanup_unknown', evidence) from primary

    def observe(self):
        if self.p is None or self.reap_started:
            raise CustodyUnknown('no_unreaped_reservation')
        try:
            # ECHILD means reservation is gone: never signal afterwards.
            os.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            root = Path('/proc') / str(self.p.pid)
            fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
            uid = root.stat().st_uid
            result = (self.p.pid, fields[19], int(fields[2]), int(fields[3]),
                      Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            if result[2:4] != (self.p.pid, self.p.pid):
                raise CustodyUnknown('not_owned_session_group')
            if not self.gate_released:
                self.initial_uid = uid
                if uid != os.getuid():
                    raise CustodyUnknown('initial_uid_changed')
            elif uid != os.getuid():
                # Exact gated direct-child exec lineage only; NOT a generic root exemption.
                if not (self.privileged and self.source_bindings and uid == 0):
                    raise CustodyUnknown('unexpected_child_credentials')
            return result
        except (OSError, ValueError, IndexError) as exc:
            raise CustodyUnknown('identity_unknown') from exc

    def signal_group(self, sig):
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            if self.identity is None or self.observe() != self.identity:
                raise CustodyUnknown('identity_changed')
            os.killpg(self.p.pid, sig)
            self.signals.append(sig)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)

    def terminal_before(self, deadline):
        while True:
            if self.observe() != self.identity:
                raise CustodyUnknown('identity_changed')
            event = os.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if event is not None:
                self.terminal = event
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(.025, left))

    def reap(self):
        # Signals blocked until the terminal result is retained; no signal after this flag.
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            if self.terminal is None:
                raise CustodyUnknown('no_terminal')
            self.reap_started = True
            pid, status = os.waitpid(self.p.pid, os.WNOHANG)
            if pid != self.p.pid:
                raise CustodyUnknown('reap_unknown')
            self.p.returncode = os.waitstatus_to_exitcode(status)
            return self.p.returncode
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)

    def cleanup(self):
        if self.cleanup_started or self.reap_started:
            return
        self.cleanup_started = True
        try:
            self.signal_group(signal.SIGTERM)
            self.terminal_before(time.monotonic() + 3)
            # Final group signal while the leader is still reserved, including a zombie.
            self.signal_group(signal.SIGKILL)
            if not self.terminal_before(time.monotonic() + 3):
                raise CustodyUnknown('kill_terminal_unknown')
            self.reap()
        except BaseException as exc:
            self.errors.append(type(exc).__name__ + ':' + str(exc))
            # UNKNOWN never grants another numeric group signal/retry.
            raise

    def wait(self, seconds):
        try:
            if not self.terminal_before(time.monotonic() + seconds):
                raise TimeoutError('owned_command_deadline')
            # Retire any owned group descendants before releasing the reservation.
            self.signal_group(signal.SIGKILL)
            return self.reap()
        except BaseException:
            self.cleanup()
            raise
