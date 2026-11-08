"""Pre-import experiment guard. Unexecuted WIP; no readiness/launch grant.

The owning coordinator must provide exact source, endpoints and immutable search
namespace. This module never imports a product module or manufactures ENOENT.
"""
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time

_NATIVE_POPEN = subprocess.Popen
_NATIVE_OS_OPEN = os.open
_NATIVE_OS_WRITE = os.write
_NATIVE_OS_CLOSE = os.close


class IsolationFailure(RuntimeError):
    pass


class Guard:
    """One process-owned guard; failure record survives swallowed exceptions.

    No generic process allowlist: owned Host/runtime children are created by the
    coordinator, not by product workers. Product gets only the C-qualified real
    absent-Codex route, and only from its exact per-arm source-qualified callsite.
    """

    def __init__(self, *, source: Path, usage_file: str, usage_sha256: str,
                 failure_file: Path, unix_paths: tuple[str, ...],
                 tcp_endpoints: tuple[tuple[str, int], ...],
                 search_dirs: tuple[str, ...], expected_cwd: Path):
        self.source = source.resolve(strict=True)
        self.usage_path = self.source / usage_file
        self.usage_sha256 = usage_sha256
        self.failure_file = failure_file
        self.unix_paths = frozenset(unix_paths)
        self.tcp_endpoints = frozenset(tcp_endpoints)
        self.search_dirs = search_dirs
        self.expected_cwd = expected_cwd.resolve(strict=True)
        self.local = threading.local()
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.failed = False
        self.installed = False

    def count(self, code: str) -> None:
        with self.lock:
            self.counts[code] = self.counts.get(code, 0) + 1

    def fail(self, code: str) -> None:
        self.failed = True
        self.count(code)
        # Counts/codes only, never argv/env/content. Open outside the state DB;
        # failure path only, not per-event file I/O inside a measured writer.
        raw = (json.dumps({'code': code, 'pid': os.getpid(),
                           't': time.monotonic()}, separators=(',', ':'))+'\n').encode()
        fd = _NATIVE_OS_OPEN(self.failure_file,
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                             0o600)
        try:
            _NATIVE_OS_WRITE(fd, raw)
        finally:
            _NATIVE_OS_CLOSE(fd)
        raise IsolationFailure(code)

    def namespace(self, environment: dict[str, str]) -> tuple[str, ...]:
        """Require source-qualified, ordinary-user-immutable lookup directories.

        These are actual VM facts, not assumptions about /usr/bin contents.
        No relative/dot/empty path or writable provider location is accepted.
        """
        if os.geteuid() == 0:
            self.fail('root_cannot_prove_nonwritable_namespace')
        if tuple(os.get_exec_path(environment)) != self.search_dirs:
            self.fail('executable_search_changed')
        seen = set()
        canonical = []
        for raw in self.search_dirs:
            p = Path(raw)
            if not raw or not p.is_absolute() or '..' in p.parts:
                self.fail('ambiguous_executable_search')
            p = p.resolve(strict=True)
            if p in seen:
                continue
            seen.add(p)
            canonical.append(str(p))
            for ancestor in (p, *p.parents):
                st = ancestor.stat()
                if (st.st_uid != 0 or st.st_mode & 0o022
                        or os.access(ancestor, os.W_OK)):
                    self.fail('mutable_executable_namespace')
            for name in ('codex', 'claude', 'clarp', 'agy', 'grok', 'muse', 'opencode'):
                # Even a dangling symlink is ambiguous, never a safe absent CLI.
                candidate = p / name
                if candidate.exists() or candidate.is_symlink():
                    self.fail('provider_present_or_ambiguous')
        if any(environment.get(k) for k in ('CLARP_AGY_BIN', 'CODEX_CHATGPT_BASE_URL')):
            self.fail('provider_override_not_empty')
        if environment.get('CLAUDE_PWA_CLAUDE_CLI', 'claude') not in ('', 'claude'):
            self.fail('claude_executable_override')
        return tuple(canonical)

    def _usage_callsite(self) -> bool:
        frame = inspect.currentframe()
        try:
            # Immediate caller of replacement Popen, not arbitrary ancestor.
            caller = frame.f_back.f_back if frame and frame.f_back else None
            return bool(caller
                and caller.f_code.co_name == 'fetch_codex_usage_app_server'
                and Path(caller.f_code.co_filename).resolve() == self.usage_path
                and hashlib.sha256(self.usage_path.read_bytes()).hexdigest()
                    == self.usage_sha256)
        finally:
            del frame

    def popen(self, args, *positional, **kwargs):
        expected = ['codex', '-s', 'read-only', '-a', 'never', 'app-server']
        if (not self._usage_callsite() or type(args) is not list or args != expected
                or positional or set(kwargs) != {'stdin', 'stdout', 'stderr', 'text'}
                or kwargs.get('shell', False)
                or kwargs.get('executable') is not None
                or kwargs.get('start_new_session', False)):
            self.fail('unapproved_product_popen')
        if Path.cwd().resolve() != self.expected_cwd or kwargs.get('cwd') is not None:
            self.fail('usage_cwd_changed')
        if kwargs.get('env') is not None:
            self.fail('usage_explicit_environment')
        if (kwargs.get('stdin') != subprocess.PIPE
                or kwargs.get('stdout') != subprocess.PIPE
                or kwargs.get('stderr') != subprocess.PIPE
                or kwargs.get('text') is not True):
            self.fail('usage_popen_shape_changed')
        environment = dict(os.environ)
        canonical = self.namespace(environment)
        # Native exec must search the same canonical immutable directories that
        # were proved above, never a mutable original lexical PATH symlink.
        environment['PATH'] = os.pathsep.join(canonical)
        self.count('qualified_usage_attempt')
        self.local.missing_usage = True
        try:
            # Freeze precisely the just-qualified inherited environment for
            # native exec search. No fake result or pre-raised exception.
            result = _NATIVE_POPEN(args, env=environment, **kwargs)
        except FileNotFoundError as exc:
            if exc.errno != errno.ENOENT or exc.filename != 'codex':
                self.fail('usage_noncanonical_missing_executable')
            self.count('native_usage_enoent')
            raise
        except BaseException:
            self.fail('usage_unexpected_native_exception')
        else:
            # Namespace proof should make this impossible. Never use the result.
            # Coordinator still owns exact process-tree cleanup and cell refusal.
            self.count('unexpected_returned_process')
            try:
                result.terminate()
            finally:
                self.fail('provider_execution_not_excluded')
        finally:
            self.local.missing_usage = False

    def audit(self, event: str, values: tuple) -> None:
        if event == 'subprocess.Popen':
            if not getattr(self.local, 'missing_usage', False):
                self.fail('unapproved_native_popen')
            executable, argv, cwd, environment = values
            if executable != 'codex' or list(argv) != [
                    'codex', '-s', 'read-only', '-a', 'never', 'app-server']:
                self.fail('native_usage_argv_changed')
            if cwd is not None:
                self.fail('native_usage_cwd_changed')
            self.namespace(environment)
        elif event in {'os.system', 'os.posix_spawn', 'os.exec', 'os.fork', 'os.forkpty'}:
            # CPython may use fork/vfork internally before native ENOENT. The
            # exception applies only inside the exact native call above.
            if event in {'os.fork', 'os.posix_spawn'} and getattr(
                    self.local, 'missing_usage', False):
                return
            self.fail('unapproved_exec_api')
        elif event in {'socket.connect', 'socket.bind'}:
            sock, endpoint = values
            if sock.family == socket.AF_UNIX:
                if not isinstance(endpoint, str) or endpoint not in self.unix_paths:
                    self.fail('unowned_unix_endpoint')
            elif sock.family == socket.AF_INET:
                if (not isinstance(endpoint, tuple) or len(endpoint) != 2
                        or endpoint not in self.tcp_endpoints):
                    self.fail('unowned_tcp_endpoint')
            else:
                self.fail('unapproved_socket_family')
        elif event in {'socket.sendto','socket.sendmsg'}:
            sock, endpoint = values
            if sock.family != socket.AF_UNIX or endpoint not in self.unix_paths:
                self.fail('unowned_datagram_or_ancillary_send')
        elif event in {'socket.gethostbyname','socket.gethostbyname_ex'}:
            if values != ('127.0.0.1',):
                self.fail('unowned_forward_lookup')
        elif event == 'socket.gethostbyaddr':
            # HTTPServer.server_bind asks getfqdn(127.0.0.1). Permit only the
            # immutable system 'files' lookup, not arbitrary native DNS.
            if values != ('127.0.0.1',):
                self.fail('unowned_reverse_lookup')
            for name in ('/etc/hosts','/etc/nsswitch.conf'):
                path = Path(name).resolve(strict=True)
                st = path.stat()
                if st.st_uid != 0 or st.st_mode & 0o022 or os.access(path,os.W_OK):
                    self.fail('mutable_local_name_service')
            hosts = Path('/etc/hosts').read_text().splitlines()
            if not any(parts and parts[0]=='127.0.0.1' and 'localhost' in parts[1:]
                       for parts in (line.split('#',1)[0].split() for line in hosts)):
                self.fail('local_loopback_name_missing')
            lines = [line.split('#',1)[0].split(':',1)[1].split() for line in
                     Path('/etc/nsswitch.conf').read_text().splitlines()
                     if line.split('#',1)[0].strip().startswith('hosts:')]
            if len(lines)!=1 or not lines[0] or lines[0][0]!='files':
                self.fail('unqualified_local_resolver_order')
            if any(Path(p).exists() for p in ('/run/nscd/socket','/var/run/nscd/socket')):
                self.fail('unowned_name_service_socket')
        elif event == 'socket.getaddrinfo':
            host, port, *_ = values
            if (host, port) not in self.tcp_endpoints:
                self.fail('unapproved_name_resolution')

    def install(self) -> None:
        if self.installed:
            self.fail('guard_installed_twice')
        if hashlib.sha256(self.usage_path.read_bytes()).hexdigest() != self.usage_sha256:
            self.fail('usage_source_changed')
        canonical = self.namespace(dict(os.environ))
        # Install before product imports so availability probes and inherited
        # child environments see this exact same immutable namespace too.
        self.search_dirs = canonical
        os.environ['PATH'] = os.pathsep.join(canonical)
        self.namespace(dict(os.environ))
        sys.addaudithook(self.audit)
        subprocess.Popen = self.popen
        self.installed = True
