import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import car_mode  # noqa: E402
from lib import codex_runner  # noqa: E402
from lib import send_service  # noqa: E402


def _use_doc(monkeypatch, tmp_path, text):
    doc = tmp_path / "car-mode.md"
    doc.write_text(text, encoding="utf-8")
    monkeypatch.setenv(car_mode.CAR_MODE_DOC_ENV, str(doc))
    car_mode.clear_cache()
    return doc


def test_env_override_wins(monkeypatch, tmp_path):
    _use_doc(monkeypatch, tmp_path, "# Driving\n\nKeep it short.")
    assert car_mode.car_mode_instruction() == "[car-mode] # Driving\n\nKeep it short."


def test_shipped_example_is_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv(car_mode.CAR_MODE_DOC_ENV, raising=False)
    monkeypatch.setattr(car_mode, "CAR_MODE_DOC", tmp_path / "missing.md")
    car_mode.clear_cache()
    active = car_mode.active_doc_path()
    assert active is not None and active.name == "car-mode.example.md"
    text = car_mode.car_mode_instruction()
    assert text.startswith("[car-mode] # Car mode")
    assert "driving" in text.lower()


def test_missing_docs_fall_back_to_a_safety_rule(monkeypatch, tmp_path):
    monkeypatch.setenv(car_mode.CAR_MODE_DOC_ENV, str(tmp_path / "nope.md"))
    monkeypatch.setattr(car_mode, "CAR_MODE_DOC", tmp_path / "missing.md")
    monkeypatch.setattr(car_mode, "_root_candidates", lambda: [tmp_path])
    car_mode.clear_cache()
    assert car_mode.active_doc_path() is None
    text = car_mode.car_mode_instruction()
    assert text.startswith("[car-mode] ")
    assert "irreversible" in text


def test_doc_is_bounded(monkeypatch, tmp_path):
    _use_doc(monkeypatch, tmp_path, "x" * 5000)
    text = car_mode.car_mode_instruction(max_chars=100)
    assert text.endswith("[Car-mode instructions truncated.]")
    assert len(text) < 200


def test_cache_refreshes_on_edit(monkeypatch, tmp_path):
    doc = _use_doc(monkeypatch, tmp_path, "first")
    assert car_mode.read_doc() == "first"
    doc.write_text("second", encoding="utf-8")
    import os
    os.utime(doc, (doc.stat().st_atime, doc.stat().st_mtime + 5))
    assert car_mode.read_doc() == "second"


def test_preamble_folds_in_car_mode_only_when_asked(monkeypatch, tmp_path):
    _use_doc(monkeypatch, tmp_path, "Speak in short sentences while driving.")
    msg = "how far to the office"
    plain = codex_runner.apply_voice_preamble(msg, voice=True)
    assert "[car-mode]" not in plain
    driving = codex_runner.apply_voice_preamble(msg, voice=True, car_mode=True)
    assert "[car-mode] Speak in short sentences while driving." in driving
    # The doc sits after the voice guidance and before the user's message.
    assert driving.index("<speak>") < driving.index("[car-mode]") < driving.index(msg)
    assert codex_runner.strip_voice_preamble(driving) == msg
    # Silent car-mode turns (typed / muted) still get the doc.
    silent = codex_runner.app_turn_instructions(voice=False, car_mode=True)
    assert "[car-mode]" in silent and "<speak>" not in silent


def test_source_marker_carries_car_mode_flag():
    on = send_service.source_marker_text(
        session="s", trace_id="t", now=1.0, synthesize_audio=True, car_mode=True)
    off = send_service.source_marker_text(
        session="s", trace_id="t", now=1.0, synthesize_audio=False)
    assert on.split() == [on.split()[0], "s", "1.000", "t", "1", "1"]
    assert off.split()[-2:] == ["0", "0"]
