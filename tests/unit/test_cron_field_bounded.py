"""A cron field is read in time that does not depend on the numbers written in it.

`0-999999999999` used to be counted value by value before the values outside the
field were dropped: about 24 ns a value, hours for a twelve-digit end, in pure
Python on the request's thread, which holds the interpreter (R24 of issue 360).
"""
from __future__ import annotations

import itertools
import threading

import pytest

from lib import scheduler


def _as_it_was(field_str: str, min_val: int, max_val: int) -> set[int]:
    """The parser before the change, kept here as the reference for small numbers."""
    result: set[int] = set()
    for part in field_str.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            subparts = part.split("/", 1)
            step = int(subparts[1])
            if step <= 0:
                raise ValueError(f"Step must be > 0 in {part}")
            if subparts[0] == "*":
                start_val, end_val = min_val, max_val
            elif "-" in subparts[0]:
                r_start, r_end = subparts[0].split("-", 1)
                start_val, end_val = int(r_start), int(r_end)
            else:
                start_val = int(subparts[0])
                end_val = max_val
            for v in range(start_val, end_val + 1, step):
                if min_val <= v <= max_val:
                    result.add(v)
        elif "-" in part:
            r_start, r_end = part.split("-", 1)
            s_val, e_val = int(r_start), int(r_end)
            for v in range(s_val, e_val + 1):
                if min_val <= v <= max_val:
                    result.add(v)
        elif part == "*":
            result.update(range(min_val, max_val + 1))
        else:
            val = int(part)
            if min_val <= val <= max_val:
                result.add(val)
            else:
                raise ValueError(f"Value {val} out of range ({min_val}-{max_val})")
    if not result:
        raise ValueError(f"Empty or invalid field expression: {field_str}")
    return result


def _answer(parse, field, low, high):
    try:
        return sorted(parse(field, low, high))
    except ValueError as error:
        return f"ValueError: {error}"


FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
NUMBERS = (0, 1, 2, 5, 6, 7, 12, 23, 24, 31, 32, 59, 60, 75)
STEPS = (1, 2, 3, 5, 7, 15, 60, 100)


def test_every_small_expression_answers_as_before():
    expressions = ["*", "", " ", "3,5", "1-5,20-25", "*/5,7", "7,*/20"]
    expressions += [str(n) for n in NUMBERS]
    expressions += [f"{a}-{b}" for a, b in itertools.product(NUMBERS, repeat=2)]
    expressions += [f"*/{s}" for s in STEPS] + ["*/0", "5/0", "1-5/0"]
    expressions += [f"{a}/{s}" for a in NUMBERS for s in STEPS]
    expressions += [f"{a}-{b}/{s}" for a, b in itertools.product(NUMBERS, repeat=2) for s in STEPS]
    differ = [(field, low, high) for field in expressions for low, high in FIELDS
              if _answer(scheduler._parse_cron_field, field, low, high) != _answer(_as_it_was, field, low, high)]
    assert differ == []
    assert len(expressions) > 1900


@pytest.mark.parametrize("field, low, high, expected", [
    ("0-999999999999", 0, 59, list(range(60))),
    ("5-999999999999", 0, 59, list(range(5, 60))),
    ("0-999999999999/7", 0, 59, list(range(0, 60, 7))),
    ("3-999999999999/10", 0, 59, [3, 13, 23, 33, 43, 53]),
    ("0-999999999999/999999999999", 0, 23, [0]),
    ("999999999990-999999999999", 0, 59, "ValueError: Empty or invalid field expression: 999999999990-999999999999"),
    ("0-999999999999", 1, 12, list(range(1, 13))),
    ("0-999999999999/5", 1, 31, [5, 10, 15, 20, 25, 30]),
])
def test_a_range_far_beyond_its_field_is_read_at_once(field, low, high, expected):
    out = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("answer", _answer(scheduler._parse_cron_field, field, low, high)),
        daemon=True)
    worker.start()
    worker.join(5)
    assert "answer" in out, f"{field!r} was still being counted after five seconds"
    assert out["answer"] == expected


def test_a_whole_expression_with_such_a_range_is_parsed_at_once():
    out = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("answer", scheduler.parse_cron("0-999999999999 * * * *")), daemon=True)
    worker.start()
    worker.join(5)
    assert "answer" in out
    assert out["answer"][0] == set(range(60))
