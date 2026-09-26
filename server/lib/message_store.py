"""SQLite transcript-message read model.

Facade over the message modules; every name callers and tests use stays
importable from here:

* `message_context`  - automation classification, display text, stripping of
                       server-injected prompt context
* `message_writes`   - client-message idempotency, server-authored rows, the
                       transcript import, shared id/revision primitives
* `message_live`     - live assistant row protocol, agy turn begin/commit
* `message_previews` - /log listing, previews, dashboard projection, activity
                       clocks, revision watermarks

Tests monkeypatch `conn`, `IMPORT_*` and `_restore_assistant_state_txn` on this
module; the implementation reads them back through here at call time
(`message_writes._facade`), so those patches still take effect.
"""
from __future__ import annotations

from .db import conn  # noqa: F401
from .message_context import (  # noqa: F401
    TEAM_CONTEXT_OPEN,
    TEAM_CONTEXT_CLOSE,
    FALLBACK_CONTEXT_OPEN,
    FALLBACK_CONTEXT_CLOSE,
    strip_injected_team_context,
    strip_injected_context,
)
from .message_writes import (  # noqa: F401
    client_message_trace,
    relink_client_message,
    has_client_message,
    latest_turn_user_origin,
    _message_activity_sql,
    record_user_message,
    has_interruption_marker,
    record_interruption_marker,
    record_dream_digest,
    IMPORT_COMMIT_EVERY,
    IMPORT_WRITE_BUDGET_SECONDS,
    IMPORT_WRITER_YIELD_SECONDS,
    store_transcript_turns,
)
from .message_live import (  # noqa: F401
    upsert_live_assistant_message,
    delete_live_assistant_message,
    finalize_live_assistant_message,
    capture_assistant_state,
    begin_agy_assistant_turn,
    restore_assistant_state,
    _restore_assistant_state_txn,
    commit_agy_assistant_turn,
    apply_final_assistant_side_effects,
)
from .message_previews import (  # noqa: F401
    list_messages,
    last_message_head,
    last_message_preview,
    dashboard_messages,
    last_message_activity,
    message_tool_details,
    last_real_message_activity,
    last_chat_message_activity,
    latest_revision,
    requires_replace,
)
