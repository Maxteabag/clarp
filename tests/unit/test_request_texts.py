"""The shared check of R79: every string of a body, keys included."""
from lib import request_texts


def test_a_lone_half_pair_anywhere_is_found_and_ordinary_text_is_not():
    assert not request_texts.lone_surrogate({"a": ["Café", {"東京": 1}], "b": None, "c": 2.5})
    for body in ({"a": "x\ud800"}, {"x\udc80": 1}, {"a": [{"b": ["\udfff"]}]}):
        assert request_texts.lone_surrogate(body), body


def test_the_sentence_is_fixed_and_says_no_text():
    assert request_texts.SENTENCE == "A text in this request holds a character that cannot be stored."
