"""Cache headers for the web app's own files.

The built bundle keeps its fixed name (`static/app/bundle.js`): the
checked-in `static/index.html` and the Tauri shell (`frontendDist`) load it by
that literal path. The content hash goes in the URL instead. `/` serves
index.html with every local `/static/...` and `/styles.css` reference
rewritten to `...?v=<hash of the file>`, so:

- a request whose `v` matches the file's current hash can be cached for a
  year (`immutable`): new content always arrives under a new URL;
- index.html and any unversioned request are `no-cache` with a strong ETag,
  so revalidation costs a 304 instead of the whole body.

Everything else the Host serves (API JSON, media) keeps `no-store`.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import threading

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

_REF = re.compile(r'''((?:src|href)=["'])(/static/[^"'?#]+|/styles\.css)(["'])''')
_lock = threading.Lock()
_hashes: dict[str, tuple[int, int, str]] = {}


def file_version(path: pathlib.Path) -> str:
    """Content hash of a file, recomputed only when its size or mtime changes."""
    st = path.stat()
    key = str(path)
    with _lock:
        cached = _hashes.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    with _lock:
        _hashes[key] = (st.st_mtime_ns, st.st_size, digest)
    return digest


def versioned_index(static_dir: pathlib.Path) -> bytes:
    """index.html with its local asset references pinned to their content."""
    html = (static_dir / "index.html").read_text()

    def pin(match: re.Match) -> str:
        url = match.group(2)
        target = static_dir / url[len("/static/"):] if url.startswith("/static/") \
            else static_dir / url.lstrip("/")
        try:
            version = file_version(target)
        except OSError:
            return match.group(0)
        return f"{match.group(1)}{url}?v={version}{match.group(3)}"

    return _REF.sub(pin, html).encode()


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:16] + '"'


def etag_matches(if_none_match: str, etag: str) -> bool:
    if not if_none_match:
        return False
    tags = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
    return "*" in tags or etag in tags


def asset_cache_headers(path: pathlib.Path, requested_version: str) -> dict[str, str]:
    """Cache-Control and ETag for one of the app's static files."""
    version = file_version(path)
    if requested_version and requested_version == version:
        return {"Cache-Control": IMMUTABLE, "ETag": f'"{version}"'}
    return {"Cache-Control": REVALIDATE, "ETag": f'"{version}"'}
