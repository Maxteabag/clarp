from __future__ import annotations


def claude_model_pin(value: str | None) -> str:
    """The --model value to dispatch for a stored pin, "" for the CLI default.

    "default" was once listed as an alias; the CLI never understood it."""
    model_id = (value or "").strip()
    return "" if model_id.lower() == "default" else model_id
