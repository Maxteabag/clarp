import json
from pathlib import Path
from lib import viz_native,viz_world


def write(path,payloads):
    with path.open('a') as f:
        for p in payloads:f.write(json.dumps(p)+'\n')


def test_native_reader_preserves_command_context_and_completion_incrementally(tmp_path):
    path=tmp_path/'native.jsonl';sid='a'*36
    write(path,[{'type':'session_meta','payload':{'id':sid,'cwd':'/repo'}},
        {'type':'event_msg','payload':{'type':'item_completed','started_at_ms':10,'completed_at_ms':30,'item':{
            'id':'tool-1','type':'CommandExecution','command':['/bin/bash','-lc','cat '+('long/'*30)+'file.py'],
            'cwd':'file:///repo/tests','parsed_cmd':[{'type':'read','path':'unit.py'}],'exit_code':0,'status':'completed'}}}])
    source=viz_native.Source();source.scan(path)
    detail=source.events['tool-1'];assert len(detail['input']['command'])>80
    assert detail['input']['cwd']=='/repo/tests' and detail['input']['paths']==['unit.py']
    assert detail['ts']==10 and detail['finished_at']==30 and detail['outcome']=='succeeded'
    position=source.offset;source.scan(path);assert source.offset==position and len(source.events)==1
    write(path,[{'type':'event_msg','payload':{'type':'item_completed','started_at_ms':40,'completed_at_ms':50,'item':{
        'id':'tool-2','type':'CommandExecution','command':['rm','x'],'exit_code':1,'status':'completed'}}}])
    source.scan(path);assert source.events['tool-2']['outcome']=='failed'


def test_partial_native_line_is_not_lost(tmp_path):
    p=tmp_path/'native.jsonl';p.write_text('{"type":"session_meta",')
    s=viz_native.Source();s.scan(p);assert s.offset==0
    with p.open('a') as f:f.write('"payload":{"id":"session","cwd":"/repo"}}\n')
    s.scan(p);assert s.session=='session'


def test_workspace_context_is_not_fabricated_as_an_inner_file(tmp_path):
    repo=tmp_path/'repo';(repo/'.git').mkdir(parents=True)
    data={'command':"python -c 'print(\"/secret/pretend.py\")'",'cwd':str(repo),'paths':[],'native_targets':True}
    e=viz_world.evidence('Bash',data,'','script:python3','execute')
    assert e['path']==str(repo) and e['scope']=='workspace' and not e['paths']


def test_relative_native_targets_anchor_to_recorded_cwd_and_keep_multiple_files(tmp_path):
    repo=tmp_path/'repo';(repo/'.git').mkdir(parents=True)
    data={'command':'cat a.py b.py','cwd':str(repo),'paths':['a.py','b.py'],'native_targets':True}
    fact=viz_world.evidence('Bash',data,'','file','read')
    ev={'id':1,'ts':1,'agent':'Nadia','agent_id':'a','target':'file','verb':'read','evidence':fact}
    w=viz_world.build([ev]);e=w['events'][0]
    assert e['world_target']=='checkout:'+str(repo)
    assert e['world_targets']==['file:'+str(repo/'a.py'),'file:'+str(repo/'b.py')]


def test_commit_and_push_stay_at_checkout_with_remote_relationship(tmp_path):
    repo=tmp_path/'repo';(repo/'.git').mkdir(parents=True)
    (repo/'.git/config').write_text('[remote "origin"]\nurl=git@github.com:org/repo.git\n')
    out=[]
    for action in ['commit','push']:
        f=viz_world.evidence('Bash',{'command':'git '+action+' origin','cwd':str(repo)},'','repo','vcs')
        out.append({'id':action,'ts':1,'agent_id':'a','agent':'Nadia','target':'repo','verb':'vcs','evidence':f})
    w=viz_world.build(out)
    assert all(e['world_target']=='checkout:'+str(repo) for e in w['events'])
    assert w['events'][0]['remote_target'] is None
    assert w['events'][1]['remote_target']=='github:org/repo'


def test_shell_read_does_not_treat_redirect_as_file_operand(tmp_path):
    f=viz_world.evidence('Bash',{'command':'cat -n a.py > /dev/null','cwd':str(tmp_path)},'','file','read')
    assert f['paths']==[str(tmp_path/'a.py')]


def test_hook_completion_pairs_by_call_id_not_tool_name():
    from lib.viz_normalize import normalize
    def row(ts,phase,cid,status):
        return {'agent_id':'a','runtime_id':1,'ts':ts,'detail':json.dumps({'tool':'Read','input':{},'call_id':cid,'phase':phase,'status':status})}
    result=normalize([row(1,'tool_started','one','running'),row(2,'tool_started','two','running'),row(3,'tool_finished','one','error'),row(4,'tool_finished','two','ok')],{})
    assert [(e['finished_at'],e['outcome']) for e in result]==[(3,'failed'),(4,'succeeded')]


def test_native_source_must_match_runtime_identity_and_lifetime(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'rollout.jsonl';sid='a'*36
    write(path,[{'type':'session_meta','payload':{'id':sid,'cwd':'/repo'}},
        {'type':'event_msg','payload':{'type':'item_completed','started_at_ms':10,'completed_at_ms':20,'item':{'id':'early','type':'CommandExecution','command':'pwd','exit_code':0}}},
        {'type':'event_msg','payload':{'type':'item_completed','started_at_ms':110,'completed_at_ms':120,'item':{'id':'owned','type':'CommandExecution','command':'pwd','exit_code':0}}}])
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE agents(agent_id TEXT,backend TEXT)');con.execute("INSERT INTO agents VALUES('agent','codex')")
    con.execute('CREATE TABLE runtimes(runtime_id INTEGER,agent_id TEXT,backend_session_id TEXT,started_at INTEGER,ended_at INTEGER)')
    con.execute("INSERT INTO runtimes VALUES(1,'agent',?,100,200)",(sid,))
    monkeypatch.setattr(viz_native,'locate',lambda x:path)
    rows,covered=viz_native.tool_rows(con,0,300)
    assert covered=={1} and len(rows)==1 and rows[0]['agent_id']=='agent' and rows[0]['ts']==110
    con.execute("UPDATE runtimes SET backend_session_id=?",('b'*36,))
    rows,covered=viz_native.tool_rows(con,0,300)
    assert not rows and not covered


def test_compound_script_does_not_claim_each_subcommand_succeeded(tmp_path):
    fact=viz_world.evidence('Bash',{'command':'git commit -m x; echo done','cwd':str(tmp_path)},'','repo','vcs')
    assert fact['action']=='execute'


def test_worktree_family_uses_common_git_directory_not_label(tmp_path):
    repo=tmp_path/'project';common=repo/'.git';common.mkdir(parents=True)
    worktree=tmp_path/'feature';worktree.mkdir()
    private=common/'worktrees/feature';private.mkdir(parents=True)
    (private/'commondir').write_text('../..')
    (worktree/'.git').write_text('gitdir: '+str(private))
    a=viz_world.checkout(str(repo));b=viz_world.checkout(str(worktree))
    assert a['project_id']==b['project_id']
    assert b['main_path']==str(repo) and b['is_worktree'] and not a['is_worktree']
    unrelated=tmp_path/'unrelated/project';(unrelated/'.git').mkdir(parents=True)
    assert viz_world.checkout(str(unrelated))['project_id']!=a['project_id']


def test_service_operation_has_actual_unit_and_honest_compound_outcome(tmp_path):
    def build(command):
        fact=viz_world.evidence('Bash',{'command':command,'cwd':str(tmp_path)},'','service','ops')
        return viz_world.build([{'id':1,'ts':1,'finished_at':2,'outcome':'succeeded','agent':'Nadia','agent_id':'a','target':'service','verb':'ops','evidence':fact}])
    world=build('sudo -n systemctl --user restart clarp-fleet-preview')
    service=next(e for e in world['entities'] if e['kind']=='service')
    assert service['unit']=='clarp-fleet-preview.service' and service['scope']=='user'
    assert world['events'][0]['world_target']==service['id']
    assert world['events'][0]['action']=='restart' and world['events'][0]['outcome']=='succeeded'
    assert build('systemctl --user restart clarp-fleet-preview\necho done')['events'][0]['outcome']=='unknown'
    assert not viz_world.service_operations("python -c 'print(\"systemctl restart pretend\")'")
    assert not viz_world.service_operations("cat <<'EOF'\nsystemctl restart pretend\nEOF")
