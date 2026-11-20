#!/usr/bin/env python3
"""Fetch one catalog-pinned snapshot into the directory inherited on fd 3."""
import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import os
import re
import signal
import stat
import sys

BLOCK = 256 * 1024
DIRECTORY = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
HUB = "https://huggingface.co"


def parts(name):
    trail = name.split("/")
    if any(part in ("", ".", "..") or "\0" in part for part in trail):
        raise ValueError("invalid snapshot path")
    return trail


def children(directory, prefix=""):
    """Check every existing entry, including partial files, without following a link."""
    # On btrfs a directory stream can retain the view from when it was opened.
    # List from a fresh stream anchored to the same pinned directory.
    stream = os.open(".", DIRECTORY, dir_fd=directory)
    try:
        names = os.listdir(stream)
    finally:
        os.close(stream)
    for name in names:
        path = prefix + name
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            folder = os.open(name, DIRECTORY, dir_fd=directory)
            try:
                yield from children(folder, path + "/")
            finally:
                os.close(folder)
        elif stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
            yield path, entry.st_size
        else:
            raise ValueError("unsafe entry in model staging")


@contextmanager
def staged_file(directory, name):
    trail = parts(name)
    folder = os.dup(directory)
    try:
        for part in trail[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=folder)
            except FileExistsError:
                pass
            child = os.open(part, DIRECTORY, dir_fd=folder)
            os.close(folder)
            folder = child
        fd = os.open(trail[-1], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=folder)
        with os.fdopen(fd, "r+b", buffering=0) as output:
            entry = os.fstat(fd)
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ValueError("unsafe model file")
            yield output
        os.fsync(folder)
    finally:
        os.close(folder)


@contextmanager
def remote_stream(session, url, offset):
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    if hasattr(session, "stream"):  # huggingface_hub 1.x uses httpx.
        with session.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            yield response, response.iter_bytes(chunk_size=BLOCK)
    else:  # The speech environment may still use the requests-based client.
        with session.get(url, headers=headers, stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            yield response, response.iter_content(chunk_size=BLOCK)


def fetch(directory, maximum, manifest, stream):
    entries = {}
    total = 0
    for name, size, checksum, lfs in manifest:
        parts(name)
        if name in entries or not isinstance(size, int) or size < 0:
            raise ValueError("invalid snapshot manifest")
        if not re.fullmatch(r"[0-9a-f]{64}" if lfs else r"[0-9a-f]{40}", checksum or ""):
            raise ValueError("missing snapshot checksum")
        total += size
        if total > maximum:
            raise ValueError("model download exceeds catalog byte limit")
        entries[name] = size, checksum, lfs

    present = dict(children(directory))
    if present.keys() - entries.keys() or sum(present.values()) > maximum:
        raise ValueError("unexpected contents in model staging")
    if any(size > entries[name][0] for name, size in present.items()):
        raise ValueError("staged file exceeds manifest size")
    transferred = 0
    for name, (size, checksum, lfs) in entries.items():
        def digest():
            return hashlib.sha256() if lfs else hashlib.sha1(f"blob {size}\0".encode())
        with staged_file(directory, name) as output:
            hashed = digest()
            offset = os.fstat(output.fileno()).st_size
            if offset > size:
                raise ValueError("staged file exceeds manifest size")
            while chunk := output.read(BLOCK):
                hashed.update(chunk)
            if offset < size:
                with stream(name, offset) as (response, chunks):
                    if response.status_code == 200:
                        output.seek(0)
                        output.truncate()
                        offset = 0
                        hashed = digest()
                    elif response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{size-1}/{size}":
                        raise ValueError("unexpected snapshot byte range")
                    for chunk in chunks:
                        transferred += len(chunk)
                        offset += len(chunk)
                        if offset > size or transferred > maximum:
                            raise ValueError("model download exceeds manifest or catalog byte limit")
                        if output.write(chunk) != len(chunk):
                            raise OSError("short write in model staging")
                        hashed.update(chunk)
            if offset != size:
                raise ValueError("incomplete model download")
            if hashed.hexdigest() != checksum:
                output.truncate(0)
                raise ValueError("model file checksum mismatch")
            os.fsync(output.fileno())
    os.fsync(directory)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--maximum", required=True, type=int)
    parser.add_argument("--parent", required=True, type=int)
    args = parser.parse_args()
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) or os.getppid() != args.parent:
        raise RuntimeError("download owner is gone")
    directory = 3
    if not stat.S_ISDIR(os.fstat(directory).st_mode):
        raise ValueError("expected staging directory")
    from huggingface_hub import HfApi, hf_hub_url
    from huggingface_hub.utils import get_session
    api = HfApi(endpoint=HUB, token=False)
    info = api.model_info(args.repository, revision=args.revision, files_metadata=True)
    if info.sha != args.revision:
        raise ValueError("unexpected model revision")
    manifest = [(e.rfilename, e.size, e.lfs.sha256 if e.lfs else e.blob_id, bool(e.lfs))
                for e in info.siblings]
    def stream(name, start):
        url = hf_hub_url(args.repository, name, revision=args.revision, endpoint=HUB)
        return remote_stream(get_session(), url, start)
    try:
        fetch(directory, args.maximum, manifest, stream)
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Provider exceptions can include signed URLs. The job exposes only its exit status.
        sys.exit(1)
