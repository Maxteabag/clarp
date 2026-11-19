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


def reply_cases():
    from lib import dispatch_adapters, html_forms
    from unittest.mock import Mock, patch
    rows=[]
    for flag in [None,False,True]:
        submission=dict(session='speaker',prompt='the answers',submission_id='submission-example')
        if flag is not None: submission['synthesize_audio']=flag
        for fails in [False,True]:
            service=Mock()
            if fails: service.submit.side_effect=ValueError('scripted failure')
            adapter=dispatch_adapters.DispatchAdapters(None,lambda _:service,new_trace_id=lambda:'test-trace')
            with patch.object(html_forms,'mark_delivered') as mark, patch.object(dispatch_adapters,'log_exception'):
                adapter.deliver_html_form(submission)
                command=service.submit.call_args.args[0]
                rows.append(dict(input=submission,failed=fails,delivered=mark.called,
                    session=command.requested_session,text=command.text,client_msg_id=command.client_msg_id,
                    synthesize_audio=command.synthesize_audio,queue_if_busy=command.queue_if_busy,origin=command.origin))
    return rows


"""Reference cases for the wired countdown projection and its accepted payload."""
from datetime import datetime
from unittest.mock import patch


def countdown_cases():
    from lib import artifacts, countdown_attention, attention_index
    target='2026-11-19T12:00:00.000+02:00'
    at=int(datetime.fromisoformat(target).timestamp()*1000)
    base=dict(artifact_id='deadline',server_id='fixture',updated_at=7,status='ready',target_at=target,time_zone='Europe/Oslo',type='countdown')
    projections=[]
    with patch('lib.settings_store.get_text',return_value='fixture'):
        for status in ['draft','cancelled','expired','active','failed','ready','completed','paused','other']:
            for purpose in [None,'informational','actionable']:
                row=dict(base,status=status,purpose=purpose)
                for delta in [-1,0,1,59999,60000,60001]:
                    projections.append(dict(row=row,now=at+delta,projection=countdown_attention.project(row,now_ms=at+delta),
                                            bucket=attention_index._bucket(row,row,at+delta)))
        for target_at in [None,False,'','not-a-date','2026-02-30T12:00:00Z','2026-11-19T12:00:00','1970-01-01T00:00:00.000999Z']:
            row=dict(base,target_at=target_at)
            projections.append(dict(row=row,now=0,projection=countdown_attention.project(row,now_ms=0),
                                    bucket=attention_index._bucket(row,row,0)))
    # Each sampling clock has an unambiguous ordinary spelling, even when
    # the input under test has an hour an interpreter cannot read.
    for stamp, clock in [
        ('1969-12-31T23:59:59.999999Z', '1969-12-31T23:59:59.999999Z'),
        ('2024-02-29T23:00:00.123456789123-05:30', '2024-03-01T04:30:00.123456Z'),
        ('2026-01-01T00:00:00.001+23:59', '2025-12-31T00:01:00.001Z'),
        ('2024-02-29T24:00:00.0000001-05:30', '2024-03-01T05:30:00Z'),
        ('9999-12-30T24:00:00Z', '9999-12-31T00:00:00Z'),
        ('9999-12-31T23:59:59Z', '9999-12-31T23:59:59Z'),
    ]:
        moment=int(datetime.fromisoformat(clock).timestamp()*1000)
        row=dict(base,target_at=stamp)
        with patch('lib.settings_store.get_text',return_value='fixture'):
            for delta in [-1,0,59999,60000]:
                now = moment + delta
                projections.append(dict(row=row, now=now, projection=countdown_attention.project(row,now_ms=now),
                                        bucket=attention_index._bucket(row,row,now)))
    cases=[]
    for key,values in [('time_zone',[None,False,'',' ','UTC','Europe/Oslo','America/New_York','does/notexist','europe/oslo','../etc','UTC+00']),
                       ('target_at',[None,False,'','now','2026-01-01','2026-01-01 01:02:03Z','2026-01-01T01:02:03','2026-01-01T01:02:03z','2026-01-01T01:02:03Z','2026-01-01T01:02:03-00:00','2026-01-01T01:02:03+23:59','2026-01-01T01:02:03+24:00','2026-01-01T01:02:03.123456789123Z','2024-02-29T00:00:00Z','2026-02-29T00:00:00Z','2026-01-01T24:00:00Z','2026-01-01T24:00:01Z','2026-01-01T24:01:00Z','2026-01-01T24:00:00.000001Z','2026-01-01T24:00:00.0000001Z','9999-12-30T24:00:00Z','9999-12-31T00:00:00Z','9999-12-31T23:59:59Z','9999-12-31T24:00:00Z','2026-01-01T25:00:00Z','2026-01-01T23:59:60Z','0000-01-01T00:00:00Z','2026-01-01T00:00:00+00:60','2026-01-01T00:00:00+23:60'])]:
        for value in values:
            payload=dict(target_at=target,time_zone='UTC')
            payload[key]=value
            cases.append(payload)
    missing = object()
    for instant in [missing, None, False, 17, [], {}, '', 'bad', target]:
        for zone in [missing, None, False, 17, [], {}, '', 'unknown/zone', 'UTC']:
            payload = {k: v for k, v in [('target_at', instant), ('time_zone', zone)] if v is not missing}
            cases.append(payload)

    def publish(payload):
        try:
            artifacts.create(session='fixture', artifact_id='deadline', type='countdown',
                             title='Deadline', payload=payload)
        except ValueError as exc:
            return str(exc)
        return None

    validation = []
    with patch.object(artifacts, '_agent', return_value={'session':'fixture'}), \
         patch.object(artifacts, '_insert_artifact', return_value={}):
        for payload in cases:
            error = publish(payload)
            errors = [] if error is None else [error]
            # _payload visits a set intersection. With two non-string fields
            # either field can be first, depending on the interpreter hash seed.
            if all(k in payload and not isinstance(payload[k], str) for k in ('target_at','time_zone')):
                errors = sorted({publish({**payload,'time_zone':'UTC'}),
                                 publish({**payload,'target_at':target})})
                assert error in errors
            validation.append(dict(payload=payload, errors=errors))
    return dict(projections=projections,validation=validation)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as root:
        writer = 'server-rs/tools/forms-events-vectors.py with Python ' + sys.version.split()[0]
        for name, fields in [
            (OUT.name, {'cases': reference(Path(root))}),
            ('form-reply-dispatch.json', {'cases': reply_cases()}),
            ('countdown-attention.json', countdown_cases()),
        ]:
            fixture = {'written_by': writer, **fields}
            (OUT.parent / name).write_text(json.dumps(fixture, ensure_ascii=True, separators=(',', ':')) + '\n')
