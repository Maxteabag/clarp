"""New path of lib.db_schema (S1, docs/architecture/s1-layering.md 3.3).

The code stays in lib/db_schema.py until the final S1 move, so a main edit to
it merges into the refactor branch without conflict. This module is that same
module object: patches on either name reach the same code.
"""
import sys

from .. import db_schema as _db_schema

sys.modules[__name__] = _db_schema
