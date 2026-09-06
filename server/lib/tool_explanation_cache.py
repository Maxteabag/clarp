"""Ready explanations only. Fixed 24h TTL; never persist tool input or source."""
import time
from .db import conn

TTL_MS = 24 * 60 * 60 * 1000


def now_ms():
    return int(time.time() * 1000)


def get(key):
    row = conn().execute(
        "SELECT explanation, expires_at FROM tool_explanation_cache WHERE cache_key=? AND expires_at>?",
        (key, now_ms())).fetchone()
    return (row[0], row[1]) if row else None


def put(key, text):
    created = now_ms()
    expires = created + TTL_MS
    conn().execute(
        "INSERT INTO tool_explanation_cache(cache_key,explanation,created_at,expires_at) VALUES(?,?,?,?) "
        "ON CONFLICT(cache_key) DO UPDATE SET explanation=excluded.explanation,created_at=excluded.created_at,expires_at=excluded.expires_at",
        (key, text, created, expires))
    return expires


def prune():
    conn().execute("DELETE FROM tool_explanation_cache WHERE expires_at<=?", (now_ms(),))
