#!/usr/bin/env python3
"""Record the full and compact /log tool fields from the Python Host (PR 691)."""
import importlib.util
import json
from pathlib import Path
import sys

import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/compact-log-vectors.json'
FIELDS = ('id', 'tools', 'display_cells', 'tool_details_available', 'activity_count')


@scratch.case('clarp-compact-log-')
def generate(place):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.isolate(place)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agents, backends, conversation

    claude = backends.by_id('claude')
    claude.find_transcript = lambda *_args, **_kwargs: None
    agent = agents.create_agent(persona='Compact', voice_id='v', cwd=str(place),
                                session='compact', backend='claude')
    agents.start_runtime(agent, 'compact')
    agents.bind_backend_session(agent, 'compact-conversation')

    def tool(key, status):
        return {'id': key, 'name': 'Bash', 'summary': key, 'status': status, 'result': 'full'}

    def cell(key, status, lines):
        return {'id': key, 'kind': 'command', 'title': 'Bash', 'status': status,
                'lines': [{'text': str(n)} for n in range(lines)]}

    inputs = [
        ('interleaved-cells', [],
         [cell('bad-before', 'error', 0), cell('representative', 'ok', 2),
          cell('bad-after', 'error', 4), cell('folded-empty', 'ok', 0),
          cell('folded-detail', 'ok', 3)]),
        ('all-cells-failed', [], [cell('bad-1', 'error', 0), cell('bad-2', 'error', 2)]),
        ('interleaved-tools',
         [tool('ok-tool', 'ok'), tool('bad-tool-1', 'error'), tool('later-tool', 'ok'),
          tool('bad-tool-2', 'error')], []),
        ('first-tool-failed',
         [tool('bad-first', 'error'), tool('ok-after', 'ok'), tool('bad-last', 'error')], []),
    ]
    turns = [
        {'id': row_id, 'role': 'assistant', 'timestamp': f'2026-01-01T00:00:0{index}Z',
         'text': row_id, 'tools': tools, 'display_cells': cells}
        for index, (row_id, tools, cells) in enumerate(inputs)
    ]
    agents.store_transcript_turns(agent_id=agent, backend_session_id='compact-conversation',
                                  source_file=str(place / 'conversation.jsonl'), turns=turns)

    def read(with_details):
        page = conversation.load_conversation(session='compact', include_tool_details=with_details)
        rows = page['turns']
        if [row['id'] for row in rows] != [row_id for row_id, _, _ in inputs]:
            raise RuntimeError('the compact-log examples did not all reach the conversation')
        return [{key: row[key] for key in FIELDS if key in row} for row in rows]

    OUT.write_text(json.dumps({'full': read(True), 'compact': read(False)}, indent=2) + '\n')


if __name__ == '__main__':
    generate()
