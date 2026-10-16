"""The test-only guard against real provider CLI launches and network egress.

Standard library only: tests/conftest.py imports it for the test session, and
a ``sitecustomize`` on the session's PYTHONPATH imports it in every child
Python process, so both install the same refusal under the same module name
(``backend_cli_guard_core``).

Once installed the patches are never undone: a retry timer that fires between
tests, after the last test, or in a child interpreter meets the same guard.
Every refusal is recorded. Inside a test it fails that test at teardown, even
when the application swallowed it. Outside any test it fails the session. In a
child process it is written to ``$CLARP_TEST_BACKEND_CLI_LOG``, which the
owning test (or the session) reads.
"""
from __future__ import annotations

import hashlib
import inspect
import ipaddress
import os
import pathlib
import shlex
import shutil
import socket
import subprocess

BACKEND_CLIS = frozenset({"claude", "clarp", "codex", "agy", "grok", "opencode", "gemini", "tmux"})
# Package specs a runner can fetch and start (npx -y @anthropic-ai/claude-code ...).
BACKEND_PACKAGES = frozenset({"@anthropic-ai/claude-code", "@openai/codex", "@google/gemini-cli",
                              "opencode-ai", "@vibe-kit/grok-cli"})
WRAPPERS = frozenset({"env", "timeout", "nohup", "setsid", "stdbuf", "nice", "ionice", "sudo",
                      "exec", "command", "unbuffer", "xargs", "script", "chrt", "taskset", "flock"})
# Options of a wrapper that take a separate value (``nice -n 10 claude``).
WRAPPER_VALUE_OPTIONS = {
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"},
    "script": {"-c", "-E", "-o", "-T"},
    "chrt": {"-p"},
    "taskset": {"-p"},
    "flock": {"-w", "-E", "-c"},
    "stdbuf": {"-i", "-o", "-e"},
}
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh"})
RUNNERS = frozenset({"node", "npx", "bunx", "bun", "uv", "uvx", "pnpm", "pnpx", "yarn", "deno"})
# A runner's own words before the program it starts (uv run, uv tool run, pnpm dlx, bun x).
RUNNER_SUBCOMMANDS = frozenset({"run", "tool", "dlx", "exec", "x"})
# Runner options that take a separate value. A value that is a provider package spec
# (npx -p @anthropic-ai/claude-code ...) is itself a launch.
RUNNER_VALUE_OPTIONS = frozenset({
    "--with", "--with-requirements", "--with-editable", "--from", "--python", "-p", "--package",
    "--group", "--only-group", "--no-group", "--extra", "--directory", "--project", "--env-file",
    "--index", "--index-url", "--default-index", "--extra-index-url", "--config-file", "--cache-dir",
    "-c", "--call", "--filter", "-C", "--dir", "--cwd", "--require", "-r", "--import", "--loader",
    "--inspect-port", "--max-old-space-size"})
# Shell operators after which the next word is a command.
SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "|&", "&", "(", ")", "{", "}", "!", "\n",
                              "then", "do", "else", "elif", "time", "if", "while", "until", "coproc"})
_EXEC_NAMES = ("execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe")
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", ""})
_CONTENT_SCAN_LIMIT = 1 << 20


class BackendLaunchBlocked(OSError):
    """A test reached a real provider CLI launch."""


class NetworkBlocked(ConnectionRefusedError):
    """A test reached a non-loopback network address."""


def _under(path, root) -> bool:
    return bool(root) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))


def _real_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLARP_TEST_REAL_HOME") or os.path.expanduser("~")).resolve()


def _real_path() -> str:
    return os.environ.get("CLARP_TEST_REAL_PATH") or os.environ.get("PATH", "")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _substitutions(command: str) -> list[str]:
    """The command lines inside backticks and $( ... ) (balanced, nested), quoted or not."""
    found = []
    i = 0
    while i < len(command):
        if command[i] == "`":
            end = command.find("`", i + 1)
            if end < 0:
                found.append(command[i + 1:])
                break
            found.append(command[i + 1:end])
            i = end + 1
            continue
        if command.startswith("$(", i) and not command.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            found.append(command[i + 2:j - 1 if depth == 0 else j])
            i = j
            continue
        i += 1
    return found


class Guard:
    """One per process. ``current`` names the running test (None between tests)."""

    def __init__(self):
        self.installed = False
        self.child = False
        self.pid = os.getpid()
        self.current = None
        self.attempts: list[str] = []
        self.outside: list[str] = []
        self.allowed: set[str] = set()
        self.allowed_dirs: set[str] = set()
        self.log = None
        self.timers: set = set()
        home = _real_home()
        self.toolchains = os.path.realpath(home / ".local" / "share" / "clarp" / "toolchains")
        self.ambient = self._ambient_cli_paths(_real_path())
        self._ambient_ids = set()
        self._ambient_sizes: dict[int, list[str]] = {}
        self._ambient_hashes: dict[str, str] = {}
        for path in self.ambient:
            try:
                st = os.stat(path)
            except OSError:
                continue
            self._ambient_ids.add((st.st_dev, st.st_ino))
            self._ambient_sizes.setdefault(st.st_size, []).append(path)

    def _ambient_cli_paths(self, real_path) -> frozenset:
        found = set()
        for name in BACKEND_CLIS:
            hit = shutil.which(name, path=real_path)
            if hit:
                found.add(os.path.realpath(hit))
        if os.path.isdir(self.toolchains):
            for entry in pathlib.Path(self.toolchains).glob("*/bin/*"):
                found.add(os.path.realpath(entry))
        return frozenset(found)

    # -- per-test bookkeeping (the test session's process only) --

    def begin(self, test, log) -> None:
        self.outside.extend(self.attempts)
        self.attempts = []
        self.allowed = set()
        self.allowed_dirs = set()
        self.current = test
        self.log = log
        self._export_allowed()

    def take(self) -> list[str]:
        """Every refused launch so far in this test (in-process and from child
        processes), cleared: a test that provokes refusals on purpose acknowledges them."""
        taken = list(self.attempts)
        self.attempts.clear()
        if self.log is not None and os.path.exists(self.log):
            taken += [line for line in pathlib.Path(self.log).read_text().splitlines() if line]
            os.unlink(self.log)
        return taken

    def end(self, session_log) -> list[str]:
        taken = self.take()
        self.current = None
        self.log = session_log
        self.allowed = set()
        self.allowed_dirs = set()
        self._export_allowed()
        return taken

    def allow(self, real: str) -> None:
        self.allowed.add(real)
        self._export_allowed()

    def allow_dir(self, real: str) -> None:
        self.allowed_dirs.add(real)
        self._export_allowed()

    def _export_allowed(self) -> None:
        # A child Python process (QA host, runtime child) honours the owning test's opt-ins.
        os.environ["CLARP_TEST_BACKEND_CLI_ALLOWED"] = os.pathsep.join(sorted(self.allowed))
        os.environ["CLARP_TEST_BACKEND_CLI_ALLOWED_DIRS"] = os.pathsep.join(sorted(self.allowed_dirs))

    def _allowed_now(self):
        if not self.child:
            return self.allowed, self.allowed_dirs
        split = lambda name: {p for p in os.environ.get(name, "").split(os.pathsep) if p}
        return split("CLARP_TEST_BACKEND_CLI_ALLOWED"), split("CLARP_TEST_BACKEND_CLI_ALLOWED_DIRS")

    # -- what counts as a provider CLI --

    def is_ambient_copy(self, target: str) -> bool:
        """Same inode or same bytes as an installed provider CLI, or a script that
        names one by absolute path (a fake that would exec the real CLI)."""
        try:
            st = os.stat(target)
        except OSError:
            return False
        if (st.st_dev, st.st_ino) in self._ambient_ids:
            return True
        same_size = self._ambient_sizes.get(st.st_size, [])
        if same_size:
            mine = _sha256(target)
            for path in same_size:
                if path not in self._ambient_hashes:
                    try:
                        self._ambient_hashes[path] = _sha256(path)
                    except OSError:
                        continue
                if self._ambient_hashes[path] == mine:
                    return True
        if st.st_size <= _CONTENT_SCAN_LIMIT:
            try:
                text = pathlib.Path(target).read_bytes().decode("utf-8", "ignore")
            except OSError:
                return False
            needles = [p for p in self.ambient] + [self.toolchains, *BACKEND_PACKAGES]
            return any(needle and needle in text for needle in needles)
        return False

    def forbidden_program(self, program, env_path) -> bool:
        program = os.fsdecode(program) if isinstance(program, bytes) else str(program)
        name = os.path.basename(program).lower()
        for ext in (".exe", ".cmd", ".bat"):
            if name.endswith(ext):
                name = name[:-len(ext)]
        if os.sep in program:
            target = os.path.realpath(program)
        else:
            hit = shutil.which(program, path=env_path)
            target = os.path.realpath(hit) if hit else None
        allowed, allowed_dirs = self._allowed_now()
        if target is not None and os.path.isfile(target) and (
                target in allowed or any(_under(target, root) for root in allowed_dirs)):
            # A registered fake runs only while it is not (a copy of) a real CLI.
            return (target in self.ambient or _under(target, self.toolchains)
                    or self.is_ambient_copy(target))
        if name in BACKEND_CLIS or name in BACKEND_PACKAGES:
            return True
        return target is not None and (target in self.ambient or _under(target, self.toolchains))

    def _package_spec(self, token: str) -> bool:
        spec = token.lower()
        for prefix in ("npm:", "jsr:"):                  # deno run npm:@anthropic-ai/claude-code
            if spec.startswith(prefix):
                spec = spec[len(prefix):]
        if spec.startswith("@"):
            scope, _, rest = spec.partition("/")
            spec = scope + "/" + rest.split("@", 1)[0]
        else:
            spec = spec.split("@", 1)[0]
        return spec in BACKEND_PACKAGES

    def forbidden_argv(self, argv, env_path) -> str:
        """The provider CLI ``argv`` would run (through wrappers, shells and script
        runners), or ""."""
        argv = [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in argv]
        i = 0
        while i < len(argv):
            program = argv[i]
            name = os.path.basename(program).lower()
            if self.forbidden_program(program, env_path) or self._package_spec(program):
                return program
            if name in SHELLS:
                rest = argv[i + 1:]
                for j, arg in enumerate(rest):
                    if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                        if j + 1 < len(rest):
                            return self.forbidden_shell(rest[j + 1], env_path)
                return ""
            if name in RUNNERS:
                # Skip the runner's options (checking any package value) and its own
                # subcommand words; the next word is the program it starts.
                j = i + 1
                while j < len(argv):
                    arg = argv[j]
                    if arg in RUNNER_VALUE_OPTIONS and j + 1 < len(argv):
                        if self._package_spec(argv[j + 1]):
                            return argv[j + 1]
                        if arg in ("-c", "--call"):          # npx -c / pnpm exec -c: a command line
                            hit = self.forbidden_shell(argv[j + 1], env_path)
                            if hit:
                                return hit
                        j += 2
                        continue
                    if arg.startswith("-"):
                        value = arg.split("=", 1)[1] if "=" in arg else ""
                        if value and self._package_spec(value):
                            return value
                        j += 1
                        continue
                    if arg.lower() in RUNNER_SUBCOMMANDS:
                        j += 1
                        continue
                    break
                if j >= len(argv):
                    return ""
                i = j
                continue
            if name == "eval":                              # eval 'claude --version'
                return self.forbidden_shell(" ".join(argv[i + 1:]), env_path)
            if name not in WRAPPERS:
                return ""
            if name == "command" and any(arg in ("-v", "-V") for arg in argv[i + 1:i + 3]):
                return ""                                   # command -v claude only looks it up
            value_options = WRAPPER_VALUE_OPTIONS.get(name, set())
            i += 1
            while i < len(argv):
                arg = argv[i]
                if name == "env" and arg in ("-S", "--split-string") and i + 1 < len(argv):
                    return self.forbidden_shell(argv[i + 1], env_path)
                if arg in value_options:
                    i += 2
                    continue
                if (arg.startswith("-") or (name == "env" and "=" in arg)
                        or (name == "timeout" and arg[:1].isdigit())):
                    i += 1
                    continue
                break
        return ""

    def forbidden_shell(self, command, env_path) -> str:
        """The provider CLI a shell command line would run, judged only at command
        positions (the start, after ; && || | & ( and the like, after VAR=value),
        so ``git log --grep claude`` or ``pytest -k codex`` is not a launch."""
        command = os.fsdecode(command) if isinstance(command, bytes) else str(command)
        for inner in _substitutions(command):              # `claude`, $(claude), "$(claude)"
            hit = self.forbidden_shell(inner, env_path)
            if hit:
                return hit
        try:
            lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = command.split()
        segment: list[str] = []
        segments = [segment]
        for token in tokens:
            if token in SHELL_SEPARATORS or (token and set(token) <= set(";&|()")):
                segment = []
                segments.append(segment)
            else:
                segment.append(token)
        for words in segments:
            k = 0
            while k < len(words) and "=" in words[k] and not words[k].startswith(("=", "-")) \
                    and words[k].split("=", 1)[0].replace("_", "a").isalnum():
                k += 1                                   # VAR=value before the command
            if k < len(words):
                hit = self.forbidden_argv(words[k:], env_path)
                if hit:
                    return hit
        return ""

    # -- refusing --

    def refuse(self, what: str, error=BackendLaunchBlocked) -> None:
        entry = f"{what} [{self.current or 'outside any test'}]"
        if self.child or os.getpid() != self.pid:
            # A child process (sitecustomize) or a forked child: tell the owner through the log.
            log = os.environ.get("CLARP_TEST_BACKEND_CLI_LOG") or self.log
            if log:
                try:
                    with open(log, "a") as handle:
                        handle.write(entry + "\n")
                except OSError:
                    pass
            if os.getpid() != self.pid:
                os._exit(127)
            raise error(f"test attempted to launch a real provider CLI: {entry}")
        if self.current is None:
            self.outside.append(entry)
        else:
            self.attempts.append(entry)
        raise error(f"test attempted to launch a real provider CLI: {entry}")


GUARD = Guard()
_ORIGINALS: dict = {}


def _env_path(env) -> str:
    if env is not None:
        value = env.get("PATH", env.get(b"PATH")) if hasattr(env, "get") else None
        if value is not None:
            return os.fsdecode(value)
        return os.defpath
    return os.environ.get("PATH", "")


def _guarded_popen_init(self, *args, **kwargs):
    bound = _ORIGINALS["popen_signature"].bind(self, *args, **kwargs)
    call = bound.arguments
    command = call.get("args")
    env_path = _env_path(call.get("env"))
    if call.get("shell"):
        text = command if isinstance(command, (str, bytes)) else " ".join(map(str, command))
        hit = GUARD.forbidden_shell(text, env_path)
    else:
        argv = [command] if isinstance(command, (str, bytes, os.PathLike)) else list(command or [])
        if call.get("executable"):
            argv = [call["executable"], *argv[1:]]
        hit = GUARD.forbidden_argv(argv, env_path)
    if hit:
        GUARD.refuse(f"Popen {command!r}")
    return _ORIGINALS["popen_init"](self, *args, **kwargs)


def _guarded_exec(name, original):
    def guarded(path, *rest):
        argv = rest[0] if name in ("execv", "execve", "execvp", "execvpe") and rest else [path, *rest]
        env = rest[1] if name in ("execve", "execvpe") and len(rest) > 1 else None
        env_path = _env_path(env)
        if GUARD.forbidden_program(path, env_path) or GUARD.forbidden_argv(list(argv) or [path], env_path):
            GUARD.refuse(f"os.{name} {path!r}")
        return original(path, *rest)
    guarded.__clarp_guard__ = True
    return guarded


def _guarded_spawn(name, original):
    def guarded(path, argv, *rest, **kwargs):
        env = rest[0] if rest else kwargs.get("env")
        env_path = _env_path(env)
        if GUARD.forbidden_program(path, env_path) or GUARD.forbidden_argv(list(argv) or [path], env_path):
            GUARD.refuse(f"os.{name} {path!r}")
        return original(path, argv, *rest, **kwargs)
    guarded.__clarp_guard__ = True
    return guarded


def _guarded_system(original):
    def guarded(command):
        if GUARD.forbidden_shell(command, os.environ.get("PATH", "")):
            GUARD.refuse(f"os.system {command!r}")
        return original(command)
    guarded.__clarp_guard__ = True
    return guarded


def _loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    host = host.strip("[]").split("%", 1)[0].lower()
    if host in _LOOPBACK_NAMES or host == socket.gethostname().lower():
        return True
    if host.endswith(".localhost"):
        return True                                   # RFC 6761: always loopback, never sent out
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped                              # ::ffff:127.0.0.1
    return address.is_loopback or address.is_unspecified


def _guarded_connect(original, label):
    def guarded(self, address, *rest):
        if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) \
                and not _loopback(address[0]):
            GUARD.refuse(f"socket.{label} {address[0]!r}", error=NetworkBlocked)
        return original(self, address, *rest)
    guarded.__clarp_guard__ = True
    return guarded


def _guarded_sendto(original):
    def guarded(self, data, *rest):
        address = rest[-1] if rest else None
        if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) \
                and not _loopback(address[0]):
            GUARD.refuse(f"socket.sendto {address[0]!r}", error=NetworkBlocked)
        return original(self, data, *rest)
    guarded.__clarp_guard__ = True
    return guarded


def _guarded_sendmsg(original):
    def guarded(self, buffers, *rest):
        address = rest[2] if len(rest) > 2 else None             # sendmsg(buffers, ancdata, flags, address)
        if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) \
                and not _loopback(address[0]):
            GUARD.refuse(f"socket.sendmsg {address[0]!r}", error=NetworkBlocked)
        return original(self, buffers, *rest)
    guarded.__clarp_guard__ = True
    return guarded


def _refuse_lookup(label, host):
    try:
        GUARD.refuse(f"socket.{label} {host!r}", error=NetworkBlocked)
    except NetworkBlocked as blocked:
        raise socket.gaierror(socket.EAI_NONAME, str(blocked)) from None


def _is_literal(host) -> bool:
    try:
        ipaddress.ip_address((host.decode() if isinstance(host, bytes) else str(host)).strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _guarded_forward_lookup(original, label):
    """gethostbyname / gethostbyname_ex: a real name sends a query off the host."""
    def guarded(host, *args, **kwargs):
        if not _loopback(host) and not _is_literal(host):
            _refuse_lookup(label, host)
        return original(host, *args, **kwargs)
    guarded.__clarp_guard__ = True
    return guarded


def _guarded_reverse_lookup(original, label):
    """gethostbyaddr (and so getfqdn) / getnameinfo: a reverse query for a
    non-loopback address, or a forward one for a name, leaves the host too."""
    def guarded(target, *args, **kwargs):
        host = target[0] if isinstance(target, tuple) else target
        if not _loopback(host):
            _refuse_lookup(label, host)
        return original(target, *args, **kwargs)
    guarded.__clarp_guard__ = True
    return guarded


def _guarded_getaddrinfo(original):
    def guarded(host, *args, **kwargs):
        # Resolving a real name already sends a query off the host, so it is
        # refused and recorded like a connect (IP literals resolve locally).
        if host is not None and not _loopback(host):
            try:
                ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
            except ValueError:
                try:
                    GUARD.refuse(f"socket.getaddrinfo {host!r}", error=NetworkBlocked)
                except NetworkBlocked as blocked:
                    raise socket.gaierror(socket.EAI_NONAME, str(blocked)) from None
        return original(host, *args, **kwargs)
    guarded.__clarp_guard__ = True
    return guarded


def install(*, child: bool) -> Guard:
    """Patch every exec primitive and the socket entry points, once per process,
    for the rest of the process's life. Idempotent."""
    if child:
        GUARD.child = True
    else:
        GUARD.child = False
        GUARD.pid = os.getpid()
    if GUARD.installed:
        return GUARD
    _ORIGINALS["popen_init"] = subprocess.Popen.__init__
    _ORIGINALS["popen_signature"] = inspect.signature(subprocess.Popen.__init__)
    subprocess.Popen.__init__ = _guarded_popen_init
    for name in _EXEC_NAMES:
        if hasattr(os, name):
            _ORIGINALS[name] = getattr(os, name)
            setattr(os, name, _guarded_exec(name, getattr(os, name)))
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            _ORIGINALS[name] = getattr(os, name)
            setattr(os, name, _guarded_spawn(name, getattr(os, name)))
    _ORIGINALS["system"] = os.system
    os.system = _guarded_system(os.system)
    _ORIGINALS["connect"] = socket.socket.connect
    _ORIGINALS["connect_ex"] = socket.socket.connect_ex
    _ORIGINALS["sendto"] = socket.socket.sendto
    _ORIGINALS["getaddrinfo"] = socket.getaddrinfo
    for name in ("sendmsg", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        owner = socket.socket if name == "sendmsg" else socket
        if hasattr(owner, name):
            _ORIGINALS[name] = getattr(owner, name)
    socket.socket.connect = _guarded_connect(socket.socket.connect, "connect")
    socket.socket.connect_ex = _guarded_connect(socket.socket.connect_ex, "connect_ex")
    socket.socket.sendto = _guarded_sendto(socket.socket.sendto)
    socket.getaddrinfo = _guarded_getaddrinfo(socket.getaddrinfo)
    if "sendmsg" in _ORIGINALS:
        socket.socket.sendmsg = _guarded_sendmsg(_ORIGINALS["sendmsg"])
    for name in ("gethostbyname", "gethostbyname_ex"):
        if name in _ORIGINALS:
            setattr(socket, name, _guarded_forward_lookup(_ORIGINALS[name], name))
    for name in ("gethostbyaddr", "getnameinfo"):
        if name in _ORIGINALS:
            setattr(socket, name, _guarded_reverse_lookup(_ORIGINALS[name], name))
    GUARD.installed = True
    return GUARD


SITECUSTOMIZE = '''\
# Written by tests/conftest.py for one test session: every child Python
# process installs the same provider-CLI and network guard as the session.
import sys as _sys
_sys.path.insert(0, {tests_dir!r})
try:
    import backend_cli_guard_core as _guard
    _guard.install(child=True)
    # A child that scrubbed $CLARP_TEST_BACKEND_CLI_LOG still reports: the session reads this log.
    _guard.GUARD.log = {fallback_log!r}
finally:
    del _sys.path[0]
'''
