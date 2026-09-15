"""Cumulative Live voice usage; router/worker billing is deliberately separate."""
import math


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class LiveUsage:
    def __init__(self, mode="api"):
        if mode not in ("api", "subscription"):
            raise ValueError("Unsupported Live billing mode")
        self.mode = mode
        self.seconds = None
        self.context_ratio = None
        self.expires_at = None
        self.finalized = False
        self.usage_final = False
        self.reason = None

    def observe(self, event):
        kind = event.get("type")
        if self.finalized or kind not in ("session.started", "session.usage.updated", "session.closed"):
            return False
        session = event.get("session")
        if isinstance(session, dict) and _number(session.get("expires_at")):
            self.expires_at = session["expires_at"]
        usage = event.get("usage")
        seconds = usage.get("seconds") if isinstance(usage, dict) else None
        if _number(seconds):
            self.seconds = seconds  # Replacement snapshot, never addition.
        context = event.get("context_window")
        ratio = context.get("usage_ratio") if isinstance(context, dict) else None
        if _number(ratio) and ratio <= 1:
            self.context_ratio = ratio
        if kind == "session.closed":
            self.finalized = True
            self.usage_final = _number(seconds)
            self.reason = str(event.get("reason") or "unknown")[:120]
        return True

    def snapshot(self):
        return {"seconds": self.seconds, "context_ratio": self.context_ratio,
                "expires_at": self.expires_at, "finalized": self.finalized,
                "usage_final": self.usage_final, "reason": self.reason,
                "voice_estimate_usd": round(self.seconds * .05 / 60, 6) if self.seconds is not None and self.mode == "api" else None,
                "billing": "openai_api" if self.mode == "api" else "chatgpt_subscription", "scope": "voice_only"}
