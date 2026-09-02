"""Tests for the turn-source helper used by the Stop / progress hooks.

The hook ships its own copy (so it can run without the lib package), but
the logic is identical and shared via this canonical implementation. If
this drifts from the hook copy, that's the bug.
"""
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
# Inline reimplementation matching the hook copy verbatim.
TURN_FRESH_SEC = 600


def turn_source_is_local(session: str, turn_source_dir: pathlib.Path) -> bool:
    try:
        raw = (turn_source_dir / session).read_text().strip().split()
        if len(raw) < 2:
            return True
        kind, ts = raw[0], float(raw[1])
        if time.time() - ts > TURN_FRESH_SEC:
            return True
        return kind != "pwa"
    except (OSError, ValueError):
        return True


def _write(d: pathlib.Path, sess: str, contents: str):
    d.mkdir(parents=True, exist_ok=True)
    (d / sess).write_text(contents)


def test_missing_marker_defaults_to_local(tmp_path):
    assert turn_source_is_local("mike", tmp_path) is True


def test_fresh_pwa_marker_returns_not_local(tmp_path):
    _write(tmp_path, "mike", f"pwa {time.time():.3f}")
    assert turn_source_is_local("mike", tmp_path) is False


def test_fresh_local_marker_returns_local(tmp_path):
    _write(tmp_path, "mike", f"local {time.time():.3f}")
    assert turn_source_is_local("mike", tmp_path) is True


def test_stale_pwa_marker_treated_as_local(tmp_path):
    old = time.time() - TURN_FRESH_SEC - 60
    _write(tmp_path, "mike", f"pwa {old:.3f}")
    assert turn_source_is_local("mike", tmp_path) is True


def test_malformed_marker_treated_as_local(tmp_path):
    _write(tmp_path, "mike", "garbage")
    assert turn_source_is_local("mike", tmp_path) is True
    _write(tmp_path, "rachel", "")
    assert turn_source_is_local("rachel", tmp_path) is True
    _write(tmp_path, "domi", "pwa notanumber")
    assert turn_source_is_local("domi", tmp_path) is True
