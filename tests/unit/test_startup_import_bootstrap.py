"""The real Host root owns the fixed import-stagger origin, not cache reset."""
import builtins
import importlib.util
import pathlib
import sys

import pytest

from lib import transcript_import_cache as cache


def test_real_host_bootstrap_registers_before_lib_imports_and_alias_reset_cannot_renew(monkeypatch):
    monkeypatch.setattr(cache, '_host_started_at', None)
    notes, early_imports = [], []
    real_note = cache.note_host_started
    real_import = builtins.__import__
    def note(origin):
        accepted = real_note(origin)
        notes.append((origin, accepted))
        return accepted
    def import_(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'lib' and set(fromlist) & {'clip_store', 'viz_normalize'}:
            assert notes, 'Host started loading lib before registering its captured startup origin'
            early_imports.append(fromlist)
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(cache, 'note_host_started', note)
    monkeypatch.setattr(builtins, '__import__', import_)
    path = pathlib.Path(__file__).resolve().parents[2] / 'server' / 'server.py'
    def load(alias):
        spec = importlib.util.spec_from_file_location(alias, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, alias, module)
        spec.loader.exec_module(module)
        return module
    first = load('stagger_host_first')
    assert early_imports
    assert notes == [(first._HOST_PROCESS_STARTED_AT, True)]
    assert cache._host_started_at == first._HOST_PROCESS_STARTED_AT
    assert cache.reset_for_tests()
    second = load('stagger_host_second')
    assert notes[-1] == (second._HOST_PROCESS_STARTED_AT, False)
    assert cache._host_started_at == first._HOST_PROCESS_STARTED_AT


@pytest.mark.parametrize('origin', [-1, float('nan'), float('inf'), True, None])
def test_invalid_startup_origin_cannot_install_or_renew_the_anchor(monkeypatch, origin):
    monkeypatch.setattr(cache, '_host_started_at', None)
    with pytest.raises(ValueError, match='invalid Host startup'):
        cache.note_host_started(origin)
    assert cache._host_started_at is None
