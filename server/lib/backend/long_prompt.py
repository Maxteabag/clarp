"""Prompts longer than one argv string may be (BACKENDS-08).

Linux caps a single argv string at MAX_ARG_STRLEN (128 KiB), and Popen fails
with E2BIG above it. A composed prompt larger than ``ARGV_PROMPT_MAX`` goes to
the CLI through stdin or a prompt file instead; a backend whose CLI takes
neither refuses the user's text above its ``prompt_limit_bytes()`` before
the send is admitted."""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Callable

# Room is left for the rest of the command line.
ARGV_PROMPT_MAX = 64 * 1024
# The largest user text a backend with only argv takes: with agy's restore
# block (60,000 bytes) and the instruction preamble (about 4 KiB) on top, the
# prompt stays under the kernel's 128 KiB.
ARGV_TEXT_LIMIT = 60 * 1024


def fits_argv(prompt: str) -> bool:
    return len(prompt.encode()) <= ARGV_PROMPT_MAX


class PromptFile:
    """One turn's prompt in a private file (its own 0700 directory)."""

    def __init__(self, prompt: str) -> None:
        self.dir = tempfile.mkdtemp(prefix="clarp-prompt-")
        self.path = os.path.join(self.dir, "prompt.txt")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(prompt)
        except BaseException:
            # No caller owns it yet: a partial prompt must not stay behind.
            self.remove()
            raise

    def open(self):
        return open(self.path, encoding="utf-8")

    def remove(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def removing(target: Callable[..., Any], prompt_file: PromptFile | None) -> Callable[..., Any]:
    """A drain target that removes the prompt file once the turn's output ends."""
    if prompt_file is None:
        return target

    def run(**kwargs: Any) -> Any:
        try:
            return target(**kwargs)
        finally:
            prompt_file.remove()
    return run
