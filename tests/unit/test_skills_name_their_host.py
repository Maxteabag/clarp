"""A skill that names the Host's database file or install directory says which Host (R149).

Two skills told an agent to read the Python Host's own files (its install's db_schema.py, its
state.sqlite, its avatars folder); an agent on a Rust Host that followed them read the other
Host's data or nothing. Every such line now says it is the Python Host's, and this keeps it so:
a line of a skill text that names ~/.local/share/clarp or state.sqlite has "Python Host" on it or
on one of the five lines before it."""
from __future__ import annotations

import pathlib
import re

SKILLS = pathlib.Path(__file__).resolve().parents[2] / "skills"
NAMES_THE_HOSTS_FILES = re.compile(r"\.local/share/clarp|state\.sqlite")


def test_every_skill_line_naming_the_hosts_files_says_which_host():
    unnamed = []
    for path in sorted(SKILLS.rglob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if NAMES_THE_HOSTS_FILES.search(line) and not any(
                    "Python Host" in near for near in lines[max(0, number - 5):number + 1]):
                unnamed.append(f"{path.relative_to(SKILLS)}:{number + 1}: {line.strip()[:80]}")
    assert not unnamed, "\n".join(unnamed)
