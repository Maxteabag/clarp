"""Where an HTTP URL sends its request, parsed once (R133).

An allow-list must compare destinations, not text: a URL can begin with an allowed prefix as
text while its parsed host is another. `destination` parses a URL once into the parts a request
goes to; `allowed` compares those parts with allowed prefixes parsed the same way and returns
the URL rebuilt from them, which is the URL the caller sends. Pure: standard library only."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_SCHEMES = {"http": 80, "https": 443}
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str          # case-folded; an IPv6 literal without its brackets
    port: int          # made explicit: the scheme's default when the URL names none
    path: str          # "/" when empty
    query: str

    def url(self) -> str:
        """The URL rebuilt from the parsed parts (no user information, no fragment); the port
        is named only when it is not the scheme's own, so the request's Host is the plain one."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        netloc = host if self.port == _SCHEMES[self.scheme] else f"{host}:{self.port}"
        return urlunsplit((self.scheme, netloc, self.path, self.query, ""))

    def under(self, prefix: "Destination", *, any_port: bool = False) -> bool:
        """Same scheme, host and port (any port with `any_port`), and the path on a path
        boundary of the prefix's."""
        if (self.scheme, self.host) != (prefix.scheme, prefix.host):
            return False
        if not any_port and self.port != prefix.port:
            return False
        base = prefix.path.rstrip("/")
        return not base or self.path == base or self.path.startswith(base + "/")


def _host(raw: str) -> str | None:
    if ":" in raw:
        try:
            return str(ipaddress.IPv6Address(raw))
        except ValueError:
            return None
    labels = raw.split(".")
    if raw.endswith(".") or not all(_LABEL.match(label) for label in labels):
        return None
    return raw


_ENCODED_SEPARATOR = re.compile(r"%(2e|2f|5c)", re.IGNORECASE)
_DECODED = {"2e": ".", "2f": "/", "5c": "\\"}


def _dot_segment(path: str) -> bool:
    """Whether the path, read as a server might read it, has a segment '.' or '..': the encoded
    dot, slash and backslash decoded once (in either case; never twice, so an encoded '%' stays
    what it is), a backslash read as a slash. A receiving server that resolves dot segments would
    serve a path outside the one judged (R139). Only the test reads it so: what is sent is the
    path as given."""
    read = _ENCODED_SEPARATOR.sub(lambda m: _DECODED[m.group(1).lower()], path).replace("\\", "/")
    return any(segment in (".", "..") for segment in read.split("/"))


def destination(url: str) -> Destination | None:
    """The parsed destination of an http(s) URL, or None: not http(s), no host, user
    information in the authority, a port that is not a number, a host that is not a plain
    name or IP address (an encoded or unexpected character, or brackets around anything but an
    IPv6 address), a backslash anywhere, a path with a dot segment (_dot_segment), or a URL that
    does not parse."""
    if (not isinstance(url, str) or any(ch.isspace() or ord(ch) < 32 for ch in url)
            or "\\" in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES or "@" in parts.netloc or not parts.hostname:
        return None
    if ("[" in parts.netloc or "]" in parts.netloc) and ":" not in parts.hostname:
        return None             # brackets around a name: the grammar's address of a version to come
    host = _host(parts.hostname.lower())
    if host is None or _dot_segment(parts.path):
        return None
    return Destination(scheme, host, port if port is not None else _SCHEMES[scheme],
                       parts.path or "/", parts.query)


def prefix(text: str) -> Destination | None:
    """An allowed prefix, parsed as a URL: refused (None) as a URL is, and when it carries a
    query or a fragment, which a prefix cannot mean."""
    found = destination(str(text or "").strip())
    if found is None or found.query or "#" in str(text):
        return None
    return found


def allowed(url: str, prefixes, *, any_port=()) -> str | None:
    """The URL to send when `url`'s parsed destination is under one of `prefixes`, or of
    `any_port` (prefixes that admit every port of their host), each parsed the same way (one
    that does not parse admits nothing); else None."""
    found = destination(url)
    if found is None:
        return None
    for texts, every_port in ((any_port, True), (prefixes, False)):
        for text in texts:
            parsed = prefix(text)
            if parsed is not None and found.under(parsed, any_port=every_port):
                return found.url()
    return None
