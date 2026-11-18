#!/usr/bin/env python3
"""Exercise the Python transcription vocabulary compiler on synthetic inputs."""
from dataclasses import asdict
import json
from pathlib import Path
import runpy
import shutil
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
    # Each component of the rank can decide an otherwise tied contest.
    compete=[Pack('low',(Term('Rare','low',1.0,0.0), Term('Recent','low',0.5,1)),priority=1),
             Pack('high',(Term('Learned','high',0.5,0,True),),priority=2)]
    for n in (1,2,3):
        record(compete,Budget('s','test','terms',n))
    for cap in (None,0,1,3,4,5,49):
        record([Pack('exact',(Term('AA BC','exact',1.0),Term('AA BD','exact',0.9),Term('abc','exact',1.0)),
                     floor=1)],Budget('s','names','tokens',25,max_term_chars=cap))
    for boundary in (0.25,0.45,0.65):
        pack=Pack('edge',tuple(Term('word'+str(i),'edge',boundary+delta) for i,delta in enumerate((-0.001,0,0.001))))
        for n in (119,120,299,300,799,800):record([pack],Budget('s','test','terms',n))
    from lib import judgment_sites, judgments
    from unittest.mock import patch
    verdicts=[]
    for transcript in ('Okay.', 'Open the folder.'):
        for probability in (None,0.0,0.5999,0.6,0.6001,1.0):
            answer=None if probability is None else judgments.Answers({'junk':{'noul':probability}},{})
            with patch.object(judgments,'judge',return_value=answer),patch.object(judgments,'record_outcome'):
                discard=judgment_sites.is_junk(transcript)
            verdicts.append(dict(transcript=transcript,probability=probability,discard=discard))
    question=judgment_sites._JUNK_QUESTION
    from lib import vocab_store, vocab_compile, workspace_vocab, heard_audio
    from lib.routes.vocab import VocabRoutes
    from unittest.mock import patch
    from contextlib import ExitStack
    import sqlite3
    db=sqlite3.connect(':memory:')
    db.executescript('PRAGMA foreign_keys=ON; CREATE TABLE agents(agent_id TEXT PRIMARY KEY);')
    schema=(ROOT/'server/lib/db_schema.py').read_text().split('CREATE TABLE vocab_packs (',1)[1].split('"""',1)[0]
    db.executescript('CREATE TABLE vocab_packs ('+schema)
    snapshots=[]
    class Handler(VocabRoutes):
        def _read_json(self): return self.body
        def _query_first(self): return self.body
        def _json_ok(self,value): self.response=[200,value]
        def _json_error(self,status,why): self.response=[status,{'error':why}]
    client=Handler()
    def step(route,method,operation,data):
        client.body=data
        getattr(client,'_handle_vocab_'+operation)()
        snapshots.append(dict(route=route,method=method,data=data,response=client.response))
    flags={}
    with ExitStack() as stack:
        for name,impl in dict(conn=lambda:db,now_ms=lambda:1000,_new_id=lambda pre:pre+'-example',
                              profile_harvests_workspace=lambda pf:flags.get(pf,False),
                              set_profile_harvests_workspace=lambda pf,flag:flags.update({pf:flag})).items():
            stack.enter_context(patch.object(vocab_store,name,impl))
        def get(route,op,**data):step(route,'GET',op,data)
        def post(route,op,**data):step(route,'POST',op,data)
        get('packs','packs_get')
        post('packs','packs_post',name='')
        post('packs','packs_post',name='bad',kind='unknown')
        post('packs','packs_post',name='Gloss',terms=['RareName',dict(text='Alpha',rarity=0.8, often_heard_as='alter')])
        post('packs','packs_post',name='Gloss')
        pk='pk-example';pf='pf-example'
        get('packs','packs_get');get('terms','terms_get',pack_id=pk)
        post('terms','terms_post',pack_id=pk,text='rarename')
        post('terms','terms_post',pack_id=pk,text='NewName',say_as=' nu ')
        post('terms/update','terms_update',term_id=999,text='unknown')
        post('terms/update','terms_update',term_id=4,rarity=2,often_heard_as='new')
        get('terms','terms_get',pack_id=pk)
        post('terms/delete','terms_delete',term_id=4)
        post('packs/update','packs_update',pack_id=pk,enabled=False,name='Renamed',priority=2,floor=1)
        get('packs','packs_get')
        post('profiles','profiles_post',name='Default',pack_ids=[pk])
        get('profiles','profiles_get');get('profile','profile_get',profile_id=pf)
        post('profiles/workspace','profile_workspace',profile_id=pf,enabled=True)
        post('profiles/packs/remove','profile_packs_remove',profile_id=pf,pack_id=pk)
        post('profiles/packs','profile_packs_post',profile_id=pf,pack_id=pk,position=3)
        post('assign','assign_post',profile_id=pf,team_id='example-team')
        get('assignments','assignments_get');get('profile','profile_get',profile_id=pf)
        post('unassign','unassign_post',team_id='example-team')
        post('packs/delete','packs_delete',pack_id=pk);get('profile','profile_get',profile_id=pf)
        get('terms','terms_get')
        post('packs/update','packs_update')
        post('terms','terms_post',pack_id='missing',text='X')
        post('assign','assign_post',profile_id=pf)
        post('assign','assign_post',profile_id=pf,agent_id='a',team_id='b')
        post('assign','assign_post',team_id='b')
        post('unassign','unassign_post')
        post('profiles/workspace','profile_workspace',profile_id=pf,enabled='yes')
        post('profiles/workspace','profile_workspace',profile_id='missing',enabled=True)
        post('profiles/packs','profile_packs_post',profile_id=pf)
        post('profiles/packs','profile_packs_post',profile_id=pf,pack_id='gone')
        post('terms/delete','terms_delete')
        post('terms/update','terms_update',term_id='bad')
        post('profiles','profiles_post',name='Default')
        post('profiles/delete','profiles_delete',profile_id=pf);get('profile','profile_get',profile_id=pf)
    db.close()
    sources=[]
    for names,recent,corrections in [([],[],[]),(['a',' A ','beta',''],['Open AlphaBeta or echo_thing','AlphaBeta delta'],[('wrong','target'),('alias','Target')]),
            (['Z9'],[' '.join('Term'+str(n) for n in range(28))],[])]:
        source=vocab_compile.Sources(agent_names=tuple(names),recent_transcripts=tuple(recent),corrections=tuple(corrections),
            project_name=' Alpha ',identifiers=('Xray','Alpha'),branches=('worktree-123/active-branch','123','one_two_three_four_five_six_seven'),
            commit_subjects=('fix CamelCase in service2',))
        sources.append(dict(input=asdict(source),packs=[asdict(p) for p in vocab_compile.dynamic_packs(source)]))
    paths=['src/ClipStreamBroker.py','src/ClipStream.rs','lib/WidgetThing.py','build/JunkName.so','libmp3lame.a','MyName.cc','x2.txt','test_only.hh','utils.ts']
    workspaces=[dict(paths=paths,ids=workspace_vocab.identifiers_from_paths(paths))]
    traces=[dict(trace=t,valid=heard_audio.valid_trace(t)) for t in ('','a'*7,'a'*8,'a'*64,'a'*65,'ABCDEF12','abcdef01','gg123456','not/a/name')]
    # The reference sweeps sidecars. Record which clip ids survive, independent of
    # the Rust Host's on-disk representation of each clip.
    import os
    audio_sweeps=[]
    for times in ([0,1,2], [13*86400,14*86400,14*86400+1],
                  list(range(499)),list(range(500)),list(range(501))):
        clips=root/'clips'
        clips.mkdir()
        now=2_000_000_000
        for i,age in enumerate(times):
            ident=f'{i:08x}'
            side=clips/(ident+'.json')
            side.write_text('{}')
            (clips/(ident+'.wav')).write_bytes(b'fixture')
            os.utime(side,(now-age,now-age))
        heard_audio.sweep(clips,now=now)
        audio_sweeps.append(dict(ages=times,survived=sorted(p.stem for p in clips.glob('*.json'))))
        shutil.rmtree(clips)
    return dict(audio_sweeps=audio_sweeps,admin=snapshots,sources=sources,workspaces=workspaces,traces=traces,question=question,verdicts=verdicts,measured=measured,budgets=budgets,declared=describe_budgets(),stories=stories)


if __name__ == '__main__':
    if sys.argv[1:]==['--isolated']:
        with tempfile.TemporaryDirectory() as tmp:
            value=reference(Path(tmp))
            table=TOOLS.parent/'crates/clarp-kernel/src/vocabulary-budgets.json'
            (table.parent / 'vocabulary-judge.json').write_text(json.dumps(value['question'],indent=1,ensure_ascii=True)+'\n')
            table.write_text(json.dumps(value['declared'],ensure_ascii=True,indent=1)+'\n')
            OUT.write_text(json.dumps(value,ensure_ascii=True,separators=(',',':'))+'\n')
    else:
        subprocess.run([sys.executable,'-I','-B',__file__,'--isolated'],check=True,
                       env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
