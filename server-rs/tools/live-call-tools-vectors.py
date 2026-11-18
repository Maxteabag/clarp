#!/usr/bin/env python3
"""Capture the live call director's receipts and the notes the voice hears."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/live-call-tools-vectors.json'


def record(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agent_live_tools as tools

    base = dict(sesjon='test', intent='one', intent_status='ok', sent=True)
    outcomes = []
    def receive(status, answer):
        outcomes.append(dict(status=status, answer=answer, receipt=tools.regi_outcome(answer, status)))
    for code in (None, 199, 200, 201, 399, 400, 401, 408, 409, 410, 412, 413, 414, 500):
        for answer in (None, [], '', {}, dict(result='refused', reason='why'), dict(base, result='forwarded')):
            receive(code, answer)
    for result in ('forwarded','queued','already_present','not_forwarded','unknown'):
        for sent in (True,False,None,0,'yes'):
            receive(200,dict(base,result=result,sent=sent))
    for field in base:
        row = dict(base, result='forwarded')
        del row[field]
        receive(200,row)
    for reason in ('cap_reached','base_task_failed','send_failed','other',None):
        for status in ('ukjent','ok',None):
            receive(200,dict(base,result='not_forwarded',reason=reason,intent_status=status))
    receive(200,dict(base,result='forwarded',base_task='started_now',unknown='omit',
                     additions_used=2, additions_max=3, max=160, len=9, max_bytes=4096))
    notes = []
    def hear(receipts):
        notes.append(dict(receipts=receipts, notes=tools.host_note(receipts)))
    hear([])
    for name in ('delegate_to_agent','schedule_callback','cancel_callback','get_agent_status',
                 'read_agent_transcript','ask_my_agent','read_result','other',''):
        for extra in ({},dict(minutes=5,at='12:34:56 UTC',replaced={'minutes':1},regi_status='venter'),
                      dict(regi_status='ukjent',reason='why'),dict(status='asked')):
            hear([dict({'tool':name,'agent':'Theo','status':'forwarded'},**extra)])
    for data in ({}, {'messages':[]},{'messages':[{'role':'assistant','text':'old'},{'role':'user','text':'new'}]},
                 {'messages':[{'role':'assistant','text':'old'},{'role':'assistant','text':'last'}]},
                 {'state':'working','current_step':'checking','last_said':'answer'},
                 {'error':'away','current_step':'checking'},{'messages':[{'role':'assistant'}]}):
        hear([dict(tool='read_agent_transcript',agent='A',untrusted_data=data)])
    for size in (7,8,9,23,24,25,39,40,41,59,60,61,79,80,81,119,120,121):
        for unit in ('A',chr(0xe9),chr(0x1f4f1)):
            text = unit * size
            hear([dict(agent=text,status=text,minutes=2,at=text,regi_status=text,reason=text,
                       untrusted_data={'last_said':text})]*3)
    for size in (449,450,451,900):
        hear([dict(tool='X'*(size-len(tools._NOTE_HEAD)-len(': ok')),status='ok')])
    return {'outcomes':outcomes,'notes':notes}


if __name__ == '__main__':
    if '--record' in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        raw = subprocess.check_output([sys.executable,'-I',__file__,'--record'],
                                     env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        rows=json.loads(raw)
        written = ',\n'.join(' "'+name+'":[\n'+',\n'.join(json.dumps(r, ensure_ascii=True)
                            for r in values)+'\n]' for name,values in rows.items())
        OUT.write_text('{\n'+written+'\n}\n',encoding='ascii')
