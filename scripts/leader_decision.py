#!/usr/bin/env python3
"""CLI wrapper for leader decision-memory helpers."""
from __future__ import annotations

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

from lib.leader_memory import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
