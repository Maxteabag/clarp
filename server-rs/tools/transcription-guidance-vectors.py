#!/usr/bin/env python3
"""Record guidance updates, their two previews and roster inputs in a disposable Python Host."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/transcription-guidance-vectors.json'


def record():
    spec = importlib.util.spec_from_file_location('host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    with tool_scratch.directory() as tmp:
        host.isolate(Path(tmp))
        host.loopback_only()
        sys.path.insert(0, str(ROOT / 'server'))
        from lib import db, settings_store, vocab
        from lib.vocab_service import VocabService
        service = VocabService(lambda: None)
        cases = []
        def case(names, switch, glossary, change):
            conn = db.conn()
            conn.execute('DELETE FROM turns')
            conn.execute('DELETE FROM agents')
            inputs = []
            for i, row in enumerate(names):
                name, created, role, deleted, *extra = row
                identity, turns, parent, archived = extra or (f'a{i}', [], None, False)
                state = 'completed' if identity == 'r' else None
                inputs.append(dict(name=name, created=created, role=role, deleted=deleted,
                                   id=identity, turns=turns, parent=parent, archived=archived, helper_state=state))
                conn.execute('INSERT INTO agents(agent_id,persona,session,created_at,voice_id,cwd,is_janitor,deleted_at,role,parent_agent_id,archived_at,helper_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (identity, name, f's{i}', created, '', '/test', int(role=='janitor'),
                              1 if deleted else None, role, parent, 1 if archived else None, state))
                for started in turns:
                    conn.execute('INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES(?,?,?,?)',
                                 (identity, 'user', 'test', started))
            conn.execute('DELETE FROM settings WHERE key IN (?,?)',(vocab._DELEGATION_NAMES_KEY,vocab._GLOSSARY_KEY))
            if switch is not None:
                settings_store.set_text(vocab._DELEGATION_NAMES_KEY,switch)
            settings_store.set_text(vocab._GLOSSARY_KEY,glossary)
            active = service.active_agent_names()
            before = vocab.settings_payload(active)
            try:
                updated = vocab.update_guidance(change)
                error = None
            except ValueError as e:
                updated,error = None,str(e)
            cases.append(dict(agents=inputs,switch=switch,glossary=glossary,change=change,before=before,
                              updated=updated,error=error,after=vocab.settings_payload(active)))
        agents=[(' second ',3,'helper',False),('Stra'+chr(223)+'e',1,'agent',False),('STRASSE',4,'agent',False),
                ('gone',0,'agent',True),('cleaner',2,'janitor',False),(' ; , ',5,'agent',False)]
        for switch in (None,'','TRUE','  Yes ','on','1','0','unknown','false'):
            case(agents,switch,'before',{})
        for change in [{'unknown':1}]+[{'delegation_agent_names_enabled':v,'technical_glossary':'else'} for v in (True,False,1,None,[],{},'false')]+[
                {'technical_glossary':v} for v in ('',None,False,True,0,8,1.5,['word',2],{'k':True},
                    '  ;Fjord, # note\r\n\t;;;#ignored\nplover;\t'+chr(0x2028)+'blue',
                    'x'*1999+' '+chr(0xe9),'x'*2001,'x'*2000,chr(0xe9)*2001,'w '*500,
                    'a'*576+';, tail',chr(0x4e00)*300,'!'*223, ' ;word'+chr(0xa0))]:
            case(agents,'true','before',change)
        for length in [0,1,218,219,220,585,586,587,588,599,600,601]:
            for char in ('x','!',chr(0x4e00)):
                case([('old',0,'agent',False),(char*length,1,'helper',False)],None,char*length,{})
        mixed = [
            ('Role helper', 1, 'helper', False, 'r', [2000], None, False),
            ('Parent helper', 2, 'agent', False, 'p', [2500], 'owner', False),
            ('Never used', 1, 'agent', False, 'n', [], None, False),
            ('Tie Z', 5, 'agent', False, 'z', [30], None, False),
            ('Tie A', 5, 'agent', False, 'a', [30], None, False),
            ('Older creation', 3, 'agent', False, 'o', [30], '', False),
            ('Most recent', 8, 'agent', False, 'm', [1, 90, 4], None, False),
            ('MOST RECENT', 0, 'agent', False, 'c', [20], None, False),
            ('Archived', 0, 'agent', False, 'h', [5000], None, True),
            ('Janitor', 0, 'janitor', False, 'j', [6000], None, False),
        ]
        case(mixed, None, '', {})
        case(list(reversed(mixed)), None, '', {})
        for words in [('x'*587,'two','three'), ('One','x'*590,'Three'), ('One','Two','!'*221,'Four'),
                      ('x'*586,), ('x'*587,), ('!'*219,), ('!'*220,)]:
            case([(word, i, 'agent', False) for i,word in enumerate(words)], None, '', {})
        return cases


if __name__=='__main__':
    if sys.argv[1:]==['--record']:
        print(json.dumps(record(),ensure_ascii=True,separators=(',',':')))
    else:
        OUT.write_bytes(subprocess.check_output([sys.executable,'-IB',__file__,'--record'],
                                                env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})))
