#!/usr/bin/env python3
"""Exercise the Python transcription vocabulary compiler on synthetic inputs."""
from dataclasses import asdict
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
OUT = TOOLS.parent / 'crates/clarp-kernel/tests/fixtures/vocabulary-packs-vectors.json'


def reference(root):
    replay = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    replay['isolate'](root)
    replay['loopback_only']()
    sys.path.insert(0, str(ROOT/'server'))
    from lib.vocab_budget import Budget, Term, Pack, compile_packs, estimate_tokens
    from lib.vocab_compile import budget_for, describe_budgets
    from lib.vocab_generators import estimate_rarity

    samples = ['', ' ', 'three words here', 'X-17: OneTwo', 'snake_case', 'two'*20,
               chr(0xdf), chr(0x00e9)*25, '...?!', 'under_score', '0 1234',
               'agent', 'yes', 'no', 'a', 'x','Something','alphabetical','abcdefghi12']
    measured=[dict(text=t, tokens=estimate_tokens(t), rarity=estimate_rarity(t)) for t in samples]
    budgets=[]
    for provider in ('faster-whisper','whisper.cpp','deepgram',' CARTESIA ','elevenlabs','google','assemblyai','other',''):
        for model in ('', 'ink-whisper',' Ink-Whisper ','Other'):
            budgets.append(dict(provider=provider, model=model, result=asdict(budget_for(provider,model))))
    live=[Pack('first',(Term('the','first',0.05),Term('Twin','first',0.8),Term('Near','first',0.45,0.9)),
               priority=2.0,floor=2),
          Pack('later',(Term('TWIN','later',1.0),Term('ordinary','later',0.25),Term('mixed','later',0.05,0.1,True),
                        Term('A long name','later',0.65)),priority=1.0,floor=1),
          Pack('disabled',(Term('hidden','disabled',1.0),),enabled=False)]
    stories=[]
    def record(packs,b):
        stories.append(dict(packs=[asdict(p) for p in packs],budget=asdict(b),audit=compile_packs(packs,b).audit()))
    for unit in ('terms','tokens','words'):
        for capacity in (-1,0,1,2,3,4,10,119,120,121,299,300,301,799,800,801):
            record(live,Budget('synthetic','fixture',unit,capacity,max_term_chars=4,supports_prose=True))
    record([],Budget('synthetic','fixture','terms',20))
    record(live[2:],Budget('synthetic','fixture','terms',20))
    record(live,Budget('s','b','tokens',300,supports_prose=False))
    record([Pack('t',(Term('One','t'),Term('ONE','t'),Term('Two','t')),priority=-1.0)],Budget('s','b','terms',300))
    return dict(measured=measured,budgets=budgets,declared=describe_budgets(),stories=stories)


if __name__ == '__main__':
    if sys.argv[1:]==['--isolated']:
        with tempfile.TemporaryDirectory() as tmp:
            value=reference(Path(tmp))
            table=TOOLS.parent/'crates/clarp-kernel/src/vocabulary-budgets.json'
            table.write_text(json.dumps(value['declared'],ensure_ascii=True,indent=1)+'\n')
            OUT.write_text(json.dumps(value,ensure_ascii=True,separators=(',',':'))+'\n')
    else:
        subprocess.run([sys.executable,'-I','-B',__file__,'--isolated'],check=True,
                       env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
