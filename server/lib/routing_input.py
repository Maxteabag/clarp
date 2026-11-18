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
import time
from typing import Iterator

# The kernel's limit for one argument, its terminating NUL included (PAGE_SIZE * 32).
ARGUMENT_LIMIT = 128 * 1024

# Backend id -> how the prompt goes: "stdin" (the argv's last element is the prompt), or a flag
# that names a prompt file (the argv's last two elements are a flag and the prompt).
_STDIN = {"claude": (), "codex": ("-",)}
_PROMPT_FILE = {"grok": ("-p", "--prompt-file"), "muse": ("--", "--prompt-file")}


class PromptNotEncodable(RuntimeError):
    """The prompt holds a lone half of a UTF-16 pair outside the range surrogateescape maps back
    to a byte: no CLI can be handed it, on argv or otherwise (as on main)."""


class PromptTooLarge(RuntimeError):
    """The prompt cannot be handed to this CLI: it only takes it as an argument, and it is over
    the kernel's limit for one."""


@dataclasses.dataclass(frozen=True)
class Prepared:
    argv: list[str]
    input: bytes | None   # for stdin, encoded as argv is; None when nothing is written there


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
    # Bytes as the command line would have carried them: UTF-8, with surrogateescape for the
    # bytes a text read from the system kept as surrogates. Anything else no CLI can be handed:
    # one provider-style failure, its words naming the backend only.
    try:
        raw = prompt.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raise PromptNotEncodable(f"prompt cannot be encoded for {backend}") from None
    if backend in _STDIN and argv and argv[-1] == prompt:
        yield Prepared(argv[:-1] + list(_STDIN[backend]), raw)
        return
    flag, file_flag = _PROMPT_FILE.get(backend, ("", ""))
    if flag and argv[-2:] == [flag, prompt]:
        root = pathlib.Path(tempfile.mkdtemp(prefix="clarp-routing-"))
        try:
            path = root / "prompt.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            yield Prepared(argv[:-2] + [file_flag, str(path)], None)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        return
    # No other way in (agy's --print=, opencode's message), or an argv this module does not know
    # the shape of: it stays as it is, and only an argument the kernel would refuse is stopped.
    for value in argv:
        check_argument(backend, value)
    yield Prepared(argv, None)


def sweep_stale(*, older_than: float = 3600.0, now: float | None = None) -> int:
    """Remove prompt-file directories a Host death left behind: ours, named by the prefix and
    older than any routing call lives (the longest waits 150 s). Two Hosts of one user may share
    the temporary directory, so a younger one, maybe in use by the other, is left alone.
    Returns how many were removed."""
    root = pathlib.Path(tempfile.gettempdir())
    now = time.time() if now is None else now
    removed = 0
    for path in root.glob("clarp-routing-*"):
        try:
            info = path.lstat()
            if (path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid()
                    and now - info.st_mtime > older_than):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
