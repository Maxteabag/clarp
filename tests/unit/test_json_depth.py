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
