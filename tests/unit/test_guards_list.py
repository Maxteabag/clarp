"""Every structural test file runs in 'make test-guards' (G2), so the list cannot fall behind again.

A structural file is one that checks the code's structure rather than its behaviour. It says so
with ``pytestmark = pytest.mark.structural``. A file named like a guard (``*guard*``) must carry
the marker too, so a new guard cannot slip past by forgetting it. This test fails when a marked
file is missing from GUARDS in the Makefile, when GUARDS names a file that is not marked or does
not exist, and when a guard-named file is unmarked.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.structural  # in GUARDS (Makefile): this file

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Named like a guard but checking behaviour, not structure: each with its reason.
NOT_STRUCTURAL = {
    "tests/readmodel/test_readmodel_checkpoint_guard.py":
        "a behaviour golden: checkpoints written by older code still read right (checkpoint epochs)",
}
_MARKED = re.compile(r"^pytestmark\s*=.*\bpytest\.mark\.structural\b", re.M)


def guards() -> set[str]:
    text = (ROOT / "Makefile").read_text()
    block = re.search(r"^GUARDS\s*=(.*?)(?:\n(?![ \t]))", text, re.S | re.M)
    assert block, "no GUARDS in the Makefile"
    return set(re.findall(r"tests/\S+\.py", block.group(1)))


def marked() -> set[str]:
    return {str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("test_*.py")
            if _MARKED.search(p.read_text(errors="replace"))}


def test_every_structural_file_is_in_guards():
    missing = sorted(marked() - guards())
    assert not missing, "structural files not in GUARDS (Makefile):\n" + "\n".join(missing)


def test_guards_names_only_existing_structural_files():
    wrong = sorted(f for f in guards() if not (ROOT / f).is_file() or f not in marked())
    assert not wrong, "GUARDS names files that do not exist or are not marked structural:\n" + "\n".join(wrong)


def test_a_file_named_like_a_guard_is_marked():
    unmarked = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("test_*guard*.py")
                      if not _MARKED.search(p.read_text(errors="replace"))
                      and str(p.relative_to(ROOT)) not in NOT_STRUCTURAL)
    assert not unmarked, "guard-named files without pytest.mark.structural:\n" + "\n".join(unmarked)


def test_the_scanner_sees_a_marked_file_and_the_guards_block(tmp_path):
    assert _MARKED.search("import pytest\npytestmark = pytest.mark.structural\n")
    assert _MARKED.search("pytestmark = [pytest.mark.x, pytest.mark.structural]\n")
    assert not _MARKED.search("# pytestmark = pytest.mark.structural\n")
    assert "tests/unit/test_guards_list.py" in guards()
    assert all((ROOT / f).is_file() for f in NOT_STRUCTURAL)          # no stale exemption
