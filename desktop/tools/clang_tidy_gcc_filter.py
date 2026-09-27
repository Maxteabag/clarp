#!/usr/bin/env python3
"""Run clang-tidy after dropping GCC-only arguments it cannot parse."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: clang_tidy_gcc_filter.py CLANG_TIDY [ARGS...]")
    clang_tidy = sys.argv[1]
    args = [arg for arg in sys.argv[2:] if arg != "-mno-direct-extern-access"]
    os.execvp(clang_tidy, [clang_tidy, *args])


if __name__ == "__main__":
    main()
