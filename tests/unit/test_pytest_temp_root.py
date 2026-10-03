"""Test temp folders stay off /tmp, a small RAM-backed tmpfs with a quota.

Several gigabytes of per-run pytest folders there broke unrelated work
(installs, other agents' shells) on 2026-10-02 and 2026-10-03.
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "clarp_tests_conftest", pathlib.Path(__file__).resolve().parents[1] / "conftest.py")
conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conftest)


def test_without_an_explicit_temp_dir_pytest_uses_var_tmp():
    assert conftest.default_temp_root({}, var_tmp_exists=True) == "/var/tmp"


def test_an_explicit_choice_is_respected():
    assert conftest.default_temp_root({"TMPDIR": "/scratch"}, var_tmp_exists=True) is None
    assert conftest.default_temp_root({"PYTEST_DEBUG_TEMPROOT": "/x"}, var_tmp_exists=True) is None
    assert conftest.default_temp_root({}, var_tmp_exists=False) is None


def test_this_run_does_not_write_under_tmp(tmp_path):
    assert not str(tmp_path).startswith("/tmp/")
