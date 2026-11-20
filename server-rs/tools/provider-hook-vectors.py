#!/usr/bin/env python3
"""Record both parts of Claude's background Bash hook using a synthetic agent."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/provider-hook.json'


def record(directory):
    spec = importlib.util.spec_from_file_location('isolated_host', ROOT / 'tests/replay/host.py')
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.isolate(directory)
    isolated.loopback_only()
    sys.path.insert(0,str(ROOT/'server'))
    from lib.activity import tool_input_from_hook_payload
    from lib.backend.claude_background_provenance import background_task_context, tool_input_with_origin

    cases = []
    contexts = []
    base = {'tool_name':'Bash','session_id':'native','tool_use_id':'call',
            'tool_input':{'run_in_background':True,'command':'fixture','description':'Keep me','extra':[0]}}
    def case(data,agent='agent',has_turn=True):
        name = data.get('tool_name') or (data.get('tool') or {}).get('name') or 'tool'
        inputs = tool_input_from_hook_payload(data)
        native = (data.get('session_id') or '').strip()
        modified = context = None
        if name == 'Bash':
            modified = tool_input_with_origin({'agent_id':agent,'backend':'claude'},native,data.get('tool_use_id'),inputs)
            context = background_task_context(inputs) if has_turn else None
        if context is not None and context not in contexts:
            contexts.append(context)
        cases.append([data,agent,has_turn,modified,contexts.index(context) if context is not None else None])
    for name in ['Bash','bash','Monitor','Agent',None,'']:
        for flag in [True,False,None,1,'true',[]]:
            for turn in [False,True]:
                case({**base,'tool_name':name,'tool_input':{**base['tool_input'],'run_in_background':flag}},has_turn=turn)
    prefix = tool_input_with_origin({'agent_id':'agent','backend':'claude'},'native','call',base['tool_input'])['command'].split('\n')[0]+'\n'
    for command in ['',None,False,0,[],{},'quote it: '+chr(39), 'one\ntwo',prefix+'unchanged',prefix.strip()+'suffix', ' '+prefix+'fixture']:
        case({**base,'tool_input':{**base['tool_input'],'command':command}})
    for key in ['agent','tool_use_id','session_id']:
        for v in ['', 'a', 'v'*159, 'v'*160,'v'*161,'-9_a','a.b','a/b',chr(0xe9),' v ']:
            case({**base,**({key:v} if key!='agent' else {})},v if key=='agent' else 'agent')
    for inputs in [None,[],{'run_in_background':True},base['tool_input']]:
        case({**base,'tool_input':inputs,'tool_name':None,'tool':{'name':'Bash','input':base['tool_input']}})
    return {'contexts':contexts,'cases':cases}


if __name__ == '__main__':
    if '--record' in sys.argv:
        with scratch.directory() as root:
            print(json.dumps(record(Path(root))))
    else:
        data=json.loads(subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=scratch.child_env({'PATH':os.defpath,'TZ':'UTC','LANG':'C.UTF-8'})))
        OUT.write_text(json.dumps(data,ensure_ascii=True,indent=1)+'\n',encoding='ascii')
        print(len(data['cases']),'hook cases')
