"""Log-once memory for outdated clients: one clientContractOutdated diagnostic
per (platform, build). The set is this module's own state (module-state guard:
THROTTLE); server.py imports the function only."""
from __future__ import annotations

from lib import eventlog

_OUTDATED_CLIENTS_NOTED: set[str] = set()


def _note_outdated_client(client: dict) -> None:
    """One diagnostic per (platform, build) so an old app polling every few
    seconds does not flood the event log."""
    key = f"{client.get('platform')}/{client.get('build')}"
    if key in _OUTDATED_CLIENTS_NOTED:
        return
    _OUTDATED_CLIENTS_NOTED.add(key)
    eventlog.emit(
        "server", "clientContractOutdated", level="warning",
        detail={"platform": client.get("platform"), "build": client.get("build"),
                "client_contract": client.get("contract"), "reason": client.get("reason")})
