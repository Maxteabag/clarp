#!/usr/bin/env python3
"""Record the reference interpreter's decimal int(text) answers and Unicode digit blocks."""
import json
import sys
from pathlib import Path
from unicodedata import decimal

KERNEL = Path(__file__).resolve().parents[1] / "crates/clarp-kernel"
LOW = -(1 << 127)
HIGH = (1 << 127) - 1


def answer(text):
    try:
        value = int(text)
    except ValueError:
        return None
    # Python integers are unbounded after parsing. The kernel port returns an i128.
    return str(min(HIGH, max(LOW, value)))


def main():
    assert sys.get_int_max_str_digits() == 4300
    zeroes = [cp for cp in range(sys.maxunicode + 1) if decimal(chr(cp), None) == 0]
    cases = []
    def record(prefix="", unit="", count=0, suffix=""):
        cases.append(dict(prefix=prefix, unit=unit, count=count, suffix=suffix,
                          expected=answer(prefix + unit * count + suffix)))

    for zero in zeroes:
        for offset in (-1, 0, 1, 8, 9, 10):
            record(chr(zero + offset))
        record("-" + chr(zero + 2) + "_3" + chr(zero + 4))

    for text in ("", "+", "-", "2", "-0", "+007", "++7", "-+7", "  -12_345  ",
                 "5__4", "_1", "1_", "+_9", "0x10", "3.0", "1e3", "3 4",
                 chr(0x2212) + "9", chr(0xb2), chr(0x2167), chr(0x96d) + "_3"):
        record(text)
    for cp in (*range(0x21), 0x85, 0xa0, 0x1680, 0x2003, 0x200b, 0x2028, 0x202f, 0x3000):
        record(chr(cp) + "+27" + chr(cp))
    for value in (LOW-1, LOW, LOW+1, HIGH-1, HIGH, HIGH+1):
        record(str(value))
    for n in (38, 39, 40, 4299, 4300, 4301):
        for sign in ("", "+", "-"):
            for digit in ("0", "9", chr(0x969)):
                record(sign, digit, n)
        record("+", "0_", n-1, "1")
        record("-", chr(0xff11)+"_", n-1, chr(0x662))
    for name, value in (("src/python/decimal-zeroes.json", zeroes),
                        ("tests/fixtures/python-int-vectors.json", cases)):
        (KERNEL/name).write_text(json.dumps(value, ensure_ascii=True, separators=(",", ":"))+"\n",
                                 encoding="ascii")


if __name__ == "__main__":
    main()
