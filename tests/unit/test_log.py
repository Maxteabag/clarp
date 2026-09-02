"""Tests for lib.log."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.log import log, log_exception  # noqa: E402


def test_log_emits_event_and_detail(capsys):
    log("startup", "ok")
    err = capsys.readouterr().err
    assert "startup" in err
    assert "ok" in err


def test_log_exception_includes_type_and_message(capsys):
    try:
        raise ValueError("bad thing happened")
    except ValueError as e:
        log_exception("parseFail", e, detail="while reading config")
    err = capsys.readouterr().err
    assert "parseFail" in err
    assert "ValueError" in err
    assert "bad thing happened" in err
    assert "while reading config" in err
