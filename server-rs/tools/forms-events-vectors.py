#!/usr/bin/env python3
"""Form telemetry from the Python Host, using an isolated SQLite database."""
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/forms-events.json'


def reference(root):
    replay = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    replay['isolate'](root)
    replay['loopback_only']()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import artifacts, html_form_events as events

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY);' + events.SCHEMA)
    form = {'type': 'html_form', 'payload': {'version': 'v1'}}
    inputs = []

    def case(kind, data):
        name = str(len(inputs))
        db.execute('INSERT INTO artifacts VALUES (?)', (name,))
        db.commit()
        try:
            if kind == 'append':
                output = events.append(name, data)
            elif kind == 'configure':
                output = events.configure(name, data)
            else:
                output = events.read(name, **data)
        except ValueError as exc:
            output = {'error': str(exc)}
        digests = dict(db.execute('SELECT event_id, payload_hash FROM html_form_events WHERE artifact_id=?',(name,)))
        inputs.append(dict(kind=kind, artifact=name, data=data, output=output, digests=digests))

    with patch.object(events.db,'conn',return_value=db), patch.object(events.db,'now_ms',return_value=1000), patch.object(artifacts,'get',return_value=form):
        row = dict(event_id='a'*16,client_seq=1,client_at=2,event={'text':'hello'})
        def batch(rows): case('append',dict(version='v1',events=rows))
        for data in [None, {}, {'events':None}, {'events':[]}, {'events':[row]*33}]:
            case('append', data)
        for rows in [[None], [row], [row]*32]:
            batch(rows)
        for id_ in [None,'a'*15,'a'*16,'-'*80,'a'*81,'a'*15+'_','a'*15+chr(233)]:
            batch([{**row,'event_id':id_}])
        for key in ['client_seq','client_at']:
            for value in [None,False,1.0,-1,0,2**53-1,2**53]:
                batch([{**row,key:value}])
        for event in [None, [], {}, {'x':chr(233)*8188},{'x':chr(233)*8189}, {'x':'x'*16376}, {'x':'x'*16377}]:
            batch([{**row,'event':event}])
        case('append',dict(version='other',events=[row]))
        batch([row,{**row,'client_at':99}])
        batch([row,{**row,'event':{'different':True}}])
        for data in [None,{}, {'draft_key':None},{'draft_key':''},{'draft_key':'a_B-0'},{'draft_key':'a'*80},{'draft_key':'a'*81},{'draft_key':'a b'},{'draft_key':True}]:
            case('configure',data)
        for data in [{},{'after':-1},{'after':1},{'after':2**63},{'limit':0},{'limit':1},{'limit':100},{'limit':101},{'through':2**63-1},{'through':2**63},{'through':-1}]:
            case('read',data)
    return inputs


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as root:
        OUT.write_text(json.dumps(reference(Path(root)),ensure_ascii=True,separators=(',',':'))+'\n')
