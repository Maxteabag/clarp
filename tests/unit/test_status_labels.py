import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib.status_labels import MAX_STATUS_LABEL_CHARS, shorten_status_label  # noqa: E402


def test_short_status_label_passes_unchanged():
    label, changed = shorten_status_label("Awaiting Domi")

    assert label == "Awaiting Domi"
    assert changed is False


def test_long_status_label_shortens_to_header_safe_words():
    label, changed = shorten_status_label(
        "Awaiting Domi notification policy review after deploy")

    assert label == "Awaiting Domi"
    assert len(label) <= MAX_STATUS_LABEL_CHARS
    assert changed is True


def test_long_single_word_status_is_capped():
    label, changed = shorten_status_label("supercalifragilisticexpialidocious")

    assert label == "supercalifragilistic"
    assert len(label) == MAX_STATUS_LABEL_CHARS
    assert changed is True
