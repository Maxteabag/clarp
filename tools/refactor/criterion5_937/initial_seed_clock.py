"""Initial-fixture-only clock boundary; source preparation, NOT EXECUTED.

C accepted this boundary in criterion5-matched-937-clock-oracle-C.md. This is
not a Host/runtime launcher or isolation guard. The qualified outer runner must
install its private endpoint/provider guard first, verify all physical source
and cache roots, and prove this fresh seed process exits before any measured
startup. Removing the finder cannot undo aliases captured by product modules.
Therefore this process MUST NOT subsequently run a measured/reference-after-
ready mode. Native time is preserved outside synchronous importer entry calls.
"""
from contextlib import contextmanager
import functools
import hashlib
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys
import threading
import time

MODES = frozenset({'initial_seed', 'initial_reference'})
TARGETS = {'lib.clock': 'server/lib/clock.py',
           'lib.transcript_incremental': 'server/lib/transcript_incremental.py'}
MAX_CALLS = 32


class InitialSeedClock(importlib.abc.MetaPathFinder):
    def __init__(self, *, mode, source, files, epoch_ms):
        if mode not in MODES:
            raise ValueError('fixed clock forbidden outside INITIAL fixture operation')
        source = Path(source)
        if (not source.is_absolute() or source.is_symlink()
                or source.resolve(strict=True) != source):
            raise ValueError('seed source must be exact plain physical root')
        if type(epoch_ms) is not int or epoch_ms != 1767225600000:
            raise ValueError('undeclared initial seed epoch; preserve January2026')
        self.mode, self.source, self.files, self.epoch_ms = mode, source, dict(files), epoch_ms
        self.local = threading.local()
        self.lock = threading.RLock()
        self.loaded, self.calls = {}, []
        self.installed = False
        self.retired = False
        for relative in TARGETS.values():
            path = source / relative
            if (path.is_symlink() or path.resolve(strict=True) != path
                    or hashlib.sha256(path.read_bytes()).hexdigest() != self.files[relative]):
                raise ValueError('initial seed hook input source mismatch')

    def install(self):
        if self.installed or self.retired or any(name == 'lib' or name.startswith('lib.') for name in sys.modules):
            raise RuntimeError('INITIAL mode must be selected before ANY product import')
        sys.meta_path.insert(0, self)
        self.installed = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        expected = self.source / TARGETS[fullname]
        if (spec is None or spec.loader is None or spec.origin != str(expected)
                or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)):
            raise ImportError('initial seed native import not from pinned source')
        original = spec.loader
        owner = self
        class BoundLoader(importlib.abc.Loader):
            def create_module(self, module_spec):
                return original.create_module(module_spec)

            def exec_module(self, module):
                relative = TARGETS[fullname]
                if hashlib.sha256(expected.read_bytes()).hexdigest() != owner.files[relative]:
                    raise ImportError('initial seed source changed before native load')
                # Delegate the real loader. Physical no-cache roots/fresh prefix
                # are separately mandatory; file hash alone is NOT bytecode proof.
                original.exec_module(module)
                owner.after(fullname, module)
        spec.loader = BoundLoader()
        return spec

    def after(self, name, module):
        relative = TARGETS[name]
        with self.lock:
            if name in self.loaded:
                raise RuntimeError('duplicate INITIAL hook/native module load')
            self.loaded[name] = {'file': str(self.source / relative),
                                 'source_sha256': self.files[relative]}
        if name == 'lib.clock':
            original = module.now_ms
            @functools.wraps(original)
            def initial_import_clock():
                if self.retired:
                    raise RuntimeError('retired INITIAL clock alias: seed process must exit')
                return self.epoch_ms if getattr(self.local, 'depth', 0) else original()
            module.now_ms = initial_import_clock
        elif name == 'lib.transcript_incremental':
            if 'lib.clock' not in self.loaded:
                raise RuntimeError('native importer did not bind qualified clock')
            original = module.import_transcript
            @functools.wraps(original)
            def initial_import(*args, **kwargs):
                if self.retired:
                    raise RuntimeError('retired INITIAL importer: seed process must exit')
                with self.lock:
                    if len(self.calls) >= MAX_CALLS:
                        raise RuntimeError('bounded INITIAL import call inventory exceeded')
                    row = {'ordinal': len(self.calls), 'thread': threading.get_ident(),
                           'started_mono': time.monotonic(), 'outcome': 'INCOMPLETE',
                           'native_module': original.__module__,
                           'native_qualname': original.__qualname__,
                           'native_code_path': original.__code__.co_filename}
                    self.calls.append(row)
                previous = getattr(self.local, 'depth', 0)
                self.local.depth = previous + 1
                try:
                    value = original(*args, **kwargs)
                except BaseException as error:
                    row.update(outcome='ERROR', error_type=type(error).__name__)
                    raise  # native primary exception, not an observer replacement
                else:
                    row['outcome'] = 'RETURNED'
                    return value
                finally:
                    self.local.depth = previous
                    row['ended_mono'] = time.monotonic()
            module.import_transcript = initial_import

    def receipt(self):
        with self.lock:
            return {'mode': self.mode, 'epoch_ms': self.epoch_ms,
                    'scope': 'INITIAL fixture synchronous native imports only; process exit still required',
                    'loaded': {key: dict(value) for key, value in self.loaded.items()},
                    'calls': [dict(row) for row in self.calls],
                    'all_targets_observed': set(self.loaded) == set(TARGETS),
                    'all_calls_terminal': all(row['outcome'] != 'INCOMPLETE' for row in self.calls),
                    'not_a_process_exit_or_measured_clock_certificate': True}

    def remove_finder(self):
        # Terminal cleanup only. Existing aliases are deliberately not claimed
        # restored; the seed process must exit. Never reuse it for measurements.
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        self.retired = True


@contextmanager
def initial_fixture_clock(**spec):
    hook = InitialSeedClock(**spec)
    hook.install()
    try:
        yield hook
    finally:
        hook.remove_finder()
