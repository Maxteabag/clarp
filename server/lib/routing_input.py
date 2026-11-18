"""How a one-shot routing prompt reaches its CLI without standing on its command line.

``routing_cmd`` (lib.backend) gives every routing-capable backend's argv with the prompt as its
last element (grok and muse: the last two, a flag and the prompt). A prompt on argv can be read by
any process of the machine while the CLI runs, and one over the kernel's limit for a single
argument (MAX_ARG_STRLEN, 128 KiB) cannot start at all. :func:`prepared` takes that argv off the
command line: on stdin for the CLIs that read it there, in a file of a private directory for the
ones that take a prompt file. A CLI with neither keeps the prompt on argv behind a size check that
fails before exec, with an error the fallback chain (lib.model_fallbacks) takes as the provider's.

This lives outside the backend adapters on purpose: they are sources of the transcript importer's
version (lib.transcript_incremental), and a routing fix must not make every import a full one.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import pathlib
import shutil
import tempfile
from typing import Iterator

# The kernel's limit for one argument, its terminating NUL included (PAGE_SIZE * 32).
ARGUMENT_LIMIT = 128 * 1024

# Backend id -> how the prompt goes: "stdin" (the argv's last element is the prompt), or a flag
# that names a prompt file (the argv's last two elements are a flag and the prompt).
_STDIN = {"claude": (), "codex": ("-",)}
_PROMPT_FILE = {"grok": ("-p", "--prompt-file"), "muse": ("--", "--prompt-file")}


class PromptTooLarge(RuntimeError):
    """The prompt cannot be handed to this CLI: it only takes it as an argument, and it is over
    the kernel's limit for one."""


@dataclasses.dataclass(frozen=True)
class Prepared:
    argv: list[str]
    input: str | None     # for stdin; None when nothing is written there


def check_argument(backend: str, value: str) -> None:
    """Refuse, before exec, an argument the kernel would refuse (E2BIG)."""
    if len(value.encode("utf-8", "surrogatepass")) + 1 > ARGUMENT_LIMIT:
        raise PromptTooLarge(f"prompt too large for {backend}: the CLI takes it only as an argument")


@contextlib.contextmanager
def prepared(backend: str, argv: list[str], prompt: str) -> Iterator[Prepared]:
    """``argv`` from ``routing_cmd`` for ``prompt``, ready to run with the prompt off it.

    A prompt file lives in a private (0700) directory of its own, removed when the block ends,
    whether the call answered, failed or timed out."""
    argv = list(argv)
    if backend in _STDIN and argv and argv[-1] == prompt:
        yield Prepared(argv[:-1] + list(_STDIN[backend]), prompt)
        return
    flag, file_flag = _PROMPT_FILE.get(backend, ("", ""))
    if flag and argv[-2:] == [flag, prompt]:
        root = pathlib.Path(tempfile.mkdtemp(prefix="clarp-routing-"))
        try:
            path = root / "prompt.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(prompt)
            yield Prepared(argv[:-2] + [file_flag, str(path)], None)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        return
    # No other way in (agy's --print=, opencode's message), or an argv this module does not know
    # the shape of: it stays as it is, and only an argument the kernel would refuse is stopped.
    for value in argv:
        check_argument(backend, value)
    yield Prepared(argv, None)
