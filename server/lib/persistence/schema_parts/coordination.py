"""Schema of lib.coordination: which turns answered another agent (docs/protocol.md, audience)."""
from __future__ import annotations

# One row per trace that an agent's message to another agent started: the
# rows of that turn (the prompt, live rows, the imported transcript rows, the
# final reply) are coordination between agents. Keyed by the trace, never by
# a message row: the transcript import rewrites a row's origin on every pass
# and moves rows between positional ids, but it never rewrites a trace this
# table holds. `audience` only ever loosens, from 'agents' to 'mixed' (a
# user's message steered into the turn); a 'mixed' turn is shown as any other.
SCHEMA = '''CREATE TABLE IF NOT EXISTS coordination_traces (
 agent_id TEXT NOT NULL,
 trace_id TEXT NOT NULL,
 sender_agent_id TEXT NOT NULL DEFAULT '',
 audience TEXT NOT NULL DEFAULT 'agents' CHECK (audience IN ('agents', 'mixed')),
 created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL,
 PRIMARY KEY (agent_id, trace_id)
) WITHOUT ROWID;'''
