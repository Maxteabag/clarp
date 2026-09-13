"""Request trust and bounded authentication-failure throttling.

A loopback socket is not proof of a local user: reverse proxies and the relay
also connect on loopback. Forwarding metadata can only reduce local trust.
"""
from __future__ import annotations

from collections import OrderedDict
import ipaddress
import math
import threading
import time
from urllib.parse import urlsplit


def is_loopback(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer.split('%', 1)[0])
        return (address.ipv4_mapped or address).is_loopback if isinstance(address, ipaddress.IPv6Address) else address.is_loopback
    except ValueError:
        return False


def local_request(peer: str, headers) -> bool:
    if not is_loopback(peer):
        return False
    origin = headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            hostname = parsed.hostname or ""
            if parsed.scheme not in {"http", "https"} or (hostname != "localhost" and not is_loopback(hostname)):
                return False
        except ValueError:
            return False
    if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    return not any(
        name.lower().startswith(('x-forwarded-', 'tailscale-', 'cf-'))
        or name.lower() in {'forwarded', 'x-real-ip', 'x-clarp-transport'}
        for name in headers.keys()
    )


def failure_source(peer: str, headers) -> str:
    # Only a proxy on the same machine can provide the client address. Direct
    # remote clients cannot bypass throttling by supplying X-Forwarded-For.
    if is_loopback(peer):
        value = headers.get('X-Forwarded-For', '').split(',')[-1].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    return peer


class FailureLimiter:
    """Fixed-window failure budget; successful auth is never locked out.

    No sleeps in request threads and no persistent user/device lockout. Bound
    the source table so a distributed scan cannot grow memory indefinitely.
    """
    def __init__(self, limit=20, window=60.0, max_sources=4096, clock=time.monotonic):
        self.limit, self.window, self.max_sources = limit, window, max_sources
        self.clock = clock
        self._sources = OrderedDict()
        self._lock = threading.Lock()

    def failure(self, source: str) -> int:
        now = self.clock()
        with self._lock:
            start, count = self._sources.pop(source, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            count += 1
            self._sources[source] = (start, count)
            while len(self._sources) > self.max_sources:
                self._sources.popitem(last=False)
            return max(1, math.ceil(self.window - (now - start))) if count > self.limit else 0
