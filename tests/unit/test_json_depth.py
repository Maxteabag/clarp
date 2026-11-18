"""The reader's depth bound (issue 360, R73): brackets inside strings never count."""
import pytest

from lib import json_depth


def nest(depth, open_="[", close="]"):
    return (open_ * depth + close * depth).encode()


@pytest.mark.parametrize("depth,deep", [(1, False), (512, False), (513, True), (20_000, True)])
def test_arrays_and_objects_are_counted_to_the_bound(depth, deep):
    assert json_depth.too_deep(nest(depth)) is deep
    assert json_depth.too_deep(('{"a":' * depth + "1" + "}" * depth).encode()) is deep


def test_brackets_inside_strings_do_not_count():
    assert not json_depth.too_deep(('["' + "[{" * 600 + '"]').encode())
    # An escaped quote does not end the string, nor does one after an escaped backslash start one.
    assert not json_depth.too_deep(('["a\\"' + "[" * 600 + '"]').encode())
    assert json_depth.too_deep(('["a\\\\", ' + "[" * 513 + "]" * 513 + "]").encode())


def test_closing_brackets_bring_the_depth_back_down():
    side_by_side = b"[" + b",".join([nest(300)] * 4) + b"]"
    assert not json_depth.too_deep(side_by_side)


# The reviewer's corpus and seeded fuzz (PR 438 review): the counter agrees with a real parse.
import json  # noqa: E402
import random  # noqa: E402


def _depth(value):
    deepest, stack = 0, [(value, 0)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, (dict, list)):
            level += 1
            deepest = max(deepest, level)
            stack.extend((child, level) for child in (item.values() if isinstance(item, dict) else item))
    return deepest


def _rand_string(rng):
    alphabet = ['[', ']', '{', '}', '"', '\\', 'a', ' ', ',', ':', 'é', '\n']
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))


def _gen(rng, target):
    """A value nesting exactly to target along one spine, with noise beside it (built bottom-up)."""
    value = rng.choice([_rand_string(rng), 7, None, True, "\\\\\"[{" * rng.randint(0, 3)])
    for _ in range(target):
        noise = [_rand_string(rng) for _ in range(rng.randint(0, 2))]
        if rng.random() < 0.5:
            items = noise + [value] + ([[]] if rng.random() < 0.3 else []) + ([{}] if rng.random() < 0.3 else [])
            rng.shuffle(items)
            value = items
        else:
            obj = {f"k{i}" + _rand_string(rng): s for i, s in enumerate(noise)}
            obj["spine" + _rand_string(rng)] = value
            value = obj
    return value


CORPUS = {
    "arrays-512": "[" * 512 + "]" * 512, "arrays-513": "[" * 513 + "]" * 513,
    "objects-512": '{"a":' * 512 + "1" + "}" * 512, "objects-513": '{"a":' * 513 + "1" + "}" * 513,
    "mixed-512": '{"a":' + "[" * 511 + "]" * 511 + "}", "mixed-513": '{"a":' + "[" * 512 + "]" * 512 + "}",
    "brackets-in-strings-only": json.dumps({"s": "[" * 5000 + "{" * 5000}),
    "escaped-quote-then-brackets": json.dumps({"s": '\\"' + "[" * 600}),
    "odd-backslashes": json.dumps({"s": "\\" * 3 + '"' + "[" * 600, "t": "\\" * 5}),
    "even-backslashes": json.dumps({"s": "\\" * 4, "t": "[" * 600}),
    "unicode-escape-backslash": '{"s":"\\u005c","t":"' + "[" * 600 + '"}',
    "empty-containers-600": "[" + ",".join(["[]", "{}"] * 600) + "]",
}


# Small windows cross every kind of boundary (inside a string, inside an escape, between
# brackets) many times per body; the answer must be the whole body's.
WINDOWS = [json_depth.WINDOW, 1, 2, 3, 7, 64]


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("name", sorted(CORPUS))
def test_the_counter_agrees_with_a_parse_on_the_review_corpus(name, window):
    raw = CORPUS[name].encode()
    assert json_depth.too_deep(raw, window=window) == (_depth(json.loads(raw)) > 512), name


@pytest.mark.parametrize("window", [json_depth.WINDOW, 3, 97])
def test_the_counter_agrees_with_a_parse_on_three_thousand_seeded_bodies(window):
    rng = random.Random(438)
    wrong = []
    for i in range(3000):
        target = rng.choice([rng.randint(0, 520), rng.randint(505, 520), rng.randint(500, 900)])
        raw = json.dumps(_gen(rng, target), ensure_ascii=rng.random() < 0.5).encode()
        if json_depth.too_deep(raw, window=window) != (_depth(json.loads(raw)) > 512):
            wrong.append(i)
    assert wrong == []


def test_a_run_of_backslashes_longer_than_a_window_is_read_whole():
    body = ('{"s":"' + "\\" * 50 + '\\\"' + "[" * 600 + '","t":' + "[" * 513 + "]" * 513 + "}").encode()
    for window in (1, 4, 1024):
        assert json_depth.too_deep(body, window=window) == (_depth(json.loads(body)) > 512)
