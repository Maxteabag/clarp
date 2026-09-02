"""Optional same-LAN Bonjour advertisement for the Clarp HTTP service."""
from __future__ import annotations

import socket
from typing import Iterable

from .log import log, log_exception


class BonjourAdvertiser:
    def __init__(self, *, name: str, server_id: str, port: int,
                 auth_required: bool):
        self.name = name
        self.server_id = server_id
        self.port = int(port)
        self.auth_required = bool(auth_required)
        self._zeroconf = None
        self._info = None

    @staticmethod
    def _addresses() -> Iterable[bytes]:
        found: set[str] = set()
        try:
            candidates = socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            candidates = []
        for candidate in candidates:
            address = candidate[4][0]
            if address.startswith("127.") or address in found:
                continue
            found.add(address)
            yield socket.inet_aton(address)

    def start(self) -> bool:
        addresses = list(self._addresses())
        if not addresses:
            log("bonjourSkip", "no non-loopback IPv4 address")
            return False
        try:
            from zeroconf import ServiceInfo, Zeroconf
            label = "".join(
                character if character.isalnum() or character in " -_" else "-"
                for character in self.name).strip() or "Clarp"
            self._info = ServiceInfo(
                "_clarp._tcp.local.", f"{label}._clarp._tcp.local.",
                addresses=addresses, port=self.port,
                properties={
                    b"server_id": self.server_id.encode(),
                    b"auth": b"required" if self.auth_required else b"none",
                    b"path": b"/",
                },
                server=f"{socket.gethostname()}.local.",
            )
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(
                self._info, allow_name_change=True)
            log("bonjourStart", f"name={label} port={self.port}")
            return True
        except Exception as exc:  # noqa: BLE001 - optional capability
            log_exception("bonjourFail", exc)
            self.stop()
            return False

    def stop(self) -> None:
        try:
            if self._zeroconf is not None and self._info is not None:
                self._zeroconf.unregister_service(self._info)
        except Exception as exc:  # noqa: BLE001
            log_exception("bonjourStopFail", exc)
        finally:
            if self._zeroconf is not None:
                self._zeroconf.close()
            self._zeroconf = None
            self._info = None
