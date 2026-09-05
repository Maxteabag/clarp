#!/usr/bin/env python3
"""Export a bounded trace timeline without prompts, credentials or raw payloads."""
import argparse
import json
from pathlib import Path
import sqlite3


def rows(path, query, trace):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, (trace,))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-db', type=Path, required=True)
    parser.add_argument('--telemetry-db', type=Path, required=True)
    parser.add_argument('--trace', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'trace_id': args.trace, 'redaction': 'metadata-only; no prompt text or raw details'}
    report['turns'] = rows(args.state_db,
        'SELECT turn_id, agent_id, trace_id, started_at, ended_at FROM turns WHERE trace_id=? ORDER BY started_at LIMIT 500', args.trace)
    report['messages'] = rows(args.state_db,
        'SELECT message_id, agent_id, backend_session_id, role, revision, updated_at FROM messages WHERE trace_id=? ORDER BY updated_at LIMIT 500', args.trace)
    report['events'] = rows(args.telemetry_db,
        'SELECT ts, source, event, level, request_id, client_id, session, agent_id, clip_id, sse_event_id, status, duration_ms '
        'FROM diagnostic_events WHERE trace_id=? ORDER BY ts LIMIT 2000', args.trace)
    # Exclusive creation keeps an earlier evidence bundle intact.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(report, output, indent=2)
        output.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
