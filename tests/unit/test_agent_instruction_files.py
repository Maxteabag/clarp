"""Every backend must read the same repository instructions.

codex, grok and opencode look for AGENTS.md; claude looks for CLAUDE.md; gemini
looks for GEMINI.md; agy accepts either AGENTS.md or GEMINI.md. The repo keeps
one real file under three names via symlinks. A tool that rewrites one of the
symlinks as a regular file forks the rules for whichever backends read it, and
the fork is invisible until an agent follows stale instructions -- so assert the
shape here instead.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
CANONICAL = "AGENTS.md"
ALIASES = ("CLAUDE.md", "GEMINI.md")


def test_canonical_instruction_file_is_a_real_file():
    path = REPO / CANONICAL
    assert path.is_file() and not path.is_symlink()
    assert path.read_text().strip(), "instructions must not be empty"


def test_aliases_are_symlinks_to_the_canonical_file():
    for name in ALIASES:
        path = REPO / name
        assert path.is_symlink(), (
            f"{name} must stay a symlink to {CANONICAL}; restore it with"
            f" `ln -sf {CANONICAL} {name}`")
        # Relative link, so it survives clones, worktrees and containers.
        assert path.readlink() == pathlib.Path(CANONICAL)
        assert path.resolve() == (REPO / CANONICAL).resolve()
