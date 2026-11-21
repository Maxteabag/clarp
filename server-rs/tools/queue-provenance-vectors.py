#!/usr/bin/env python3
"""Record R182 queue provenance from the Python Host, without starting a Host."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-host/tests/fixtures/queue-provenance.json'


@scratch.case('clarp-queue-provenance-')
def generate(_place):
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agents, events, turn_queue

    names = {'mike': 'Mike', 'unnamed': ''}
    inputs = [('user', ''), ('agent', 'mike'), ('agent', 'gone'), ('agent', 'unnamed'),
              ('oracle', ''), ('automation', ''), ('janitor', ''), ('heartbeat', ''),
              ('future-origin', ''), ('', ''), (None, None)]
    rows = []
    with patch.object(agents, 'get_by_agent_id', side_effect=lambda key: (
            {'persona': names[key]} if key in names else None)):
        for origin, sender in inputs:
            source = {'origin': origin, 'sender_agent_id': sender}
            rows.append({**source, 'name': names.get(sender) or None,
                         'provenance': turn_queue.item_provenance(source)})
    state = dict(session='s', agent_id='a', queue_depth=1, queue_paused=True,
                 queue_started=False, queue_revision=7)
    sample = rows[1]['provenance']
    wires = {
        'mailbox': events.queue_updated(**state),
        'edited': events.queue_updated(**state, client_msg_id='m2', queue_change='edited',
                                       text='changed', **sample),
        'deleted': events.queue_updated(**state, client_msg_id='m1', queue_change='deleted',
                                        **rows[0]['provenance']),
        'retired': events.queue_updated(**state, client_msg_id='goal-wake', **rows[5]['provenance']),
    }
    OUT.write_text(json.dumps({'rows': rows, 'events': wires}, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    generate()
