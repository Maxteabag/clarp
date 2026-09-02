"""Latest client GPS fix per session.

The iOS app posts a fix (one-shot CoreLocation, When-In-Use) when the user taps
Share location — either spontaneously or in response to an agent's request. The
`request-location` skill reads the latest fix to power the maps / Uber skills.
"""
from __future__ import annotations

import math

from .db import conn, now_ms


def set_location(session: str, lat: float, lng: float,
                 accuracy: float | None = None, ts: int | None = None) -> dict:
    """Upsert the latest fix for a session. Returns the stored row."""
    session = (session or "").strip()
    if not session:
        raise ValueError("session required")
    lat = float(lat)
    lng = float(lng)
    accuracy = None if accuracy is None else float(accuracy)
    if not math.isfinite(lat) or not -90 <= lat <= 90:
        raise ValueError("latitude must be between -90 and 90")
    if not math.isfinite(lng) or not -180 <= lng <= 180:
        raise ValueError("longitude must be between -180 and 180")
    if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
        raise ValueError("accuracy must be non-negative")
    ts = int(ts or now_ms())
    conn().execute(
        """INSERT INTO client_locations (session, lat, lng, accuracy, ts)
                VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(session) DO UPDATE SET
                lat = excluded.lat, lng = excluded.lng,
                accuracy = excluded.accuracy, ts = excluded.ts""",
        (session, lat, lng, accuracy, ts),
    )
    return {"session": session, "lat": lat, "lng": lng,
            "accuracy": accuracy, "ts": ts}


def get_location(session: str) -> dict | None:
    """Latest fix for a session, or None if the user never shared one."""
    session = (session or "").strip()
    if not session:
        return None
    row = conn().execute(
        "SELECT lat, lng, accuracy, ts FROM client_locations WHERE session = ?",
        (session,),
    ).fetchone()
    if not row:
        return None
    return {"lat": row["lat"], "lng": row["lng"],
            "accuracy": row["accuracy"], "ts": int(row["ts"])}


def latest_location() -> dict | None:
    """Newest shared location fix across sessions."""
    row = conn().execute(
        """SELECT session, lat, lng, accuracy, ts
             FROM client_locations
            ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return {
        "session": row["session"],
        "lat": row["lat"],
        "lng": row["lng"],
        "accuracy": row["accuracy"],
        "ts": int(row["ts"]),
    }
