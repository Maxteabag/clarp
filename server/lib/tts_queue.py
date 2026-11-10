"""Compatibility exports for canonical durable TTS queue records."""
from .tts_queue_records import (
    CANCELLED_PREFIX,
    DONE,
    FAILED,
    QUEUED,
    SYNTHESIZING,
    cancel,
    claim_next,
    enqueue,
    fail_stale_in_flight,
    in_flight_count,
    mark_done,
    mark_failed,
    pending_count,
    prune_old,
    recent,
    reset_in_flight,
)
