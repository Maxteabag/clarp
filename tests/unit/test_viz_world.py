import json
from pathlib import Path
import pytest
from lib import viz_world, viz_library, viz_rule_author


def test_named_file_hierarchy_uses_observed_checkout_and_remote(tmp_path):
    repo=tmp_path/'clarp';(repo/'.git').mkdir(parents=True)
    (repo/'.git/config').write_text('[remote "origin"]\nurl = git@github.com:Example/clarp.git\n')
    path=str(repo/'server/api.py')
    fact=viz_world.evidence('Edit',{'file_path':path},path,'file','write')
    world=viz_world.build([{'id':1,'ts':1,'agent':'Nadia','agent_id':'a','target':'file','verb':'write','evidence':fact}])
    entities={e['id']:e for e in world['entities']}
    file=entities['file:'+path]
    assert file['label']=='api.py' and file['purpose']=='Python source'
    assert entities[file['parent']]['label']=='server'
    assert entities['github:Example/clarp']['parent']=='github:Example'
    assert world['events'][0]['action']=='edit'
    assert world['relations'][0]['to']=='github:Example/clarp'


def test_unrecorded_path_is_not_a_fabricated_file():
    fact=viz_world.evidence('Read',{},'', 'file','read')
    world=viz_world.build([{'id':1,'ts':1,'agent':'Nadia','agent_id':'a','target':'file','verb':'read','evidence':fact}])
    assert not any(e['kind']=='file' for e in world['entities'])
    assert any(e['kind']=='unresolved' for e in world['entities'])


def test_source_modules_are_published_without_an_artistic_vocabulary(tmp_path,monkeypatch):
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    program={'title':'Tidal observatory','entry':'entry.js','files':{
        'entry.js':"const Ocean=require('./systems/ocean.js');module.exports.render=api=>new Ocean().draw(api);",
        'systems/ocean.js':"module.exports=class Ocean{draw({ctx}){for(let i=0;i<120;i++)ctx.bezierCurveTo(i,0,1,2,3,4);return {title:'Tides'};}};"}}
    out=viz_library.apply_program(program,0,'A new spatial system',['action:push'])
    assert out['program']['title']=='Tidal observatory'
    assert (tmp_path/'viz-programs'/out['program']['digest']/'systems/ocean.js').is_file()
    with pytest.raises(ValueError,match='changed'):viz_library.apply_program(program,0,'stale')


def test_bad_source_and_escaping_paths_cannot_replace_a_program(tmp_path,monkeypatch):
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    with pytest.raises(ValueError,match='syntax'):
        viz_library.apply_program({'entry':'x.js','files':{'x.js':'function ('}},0,'bad')
    with pytest.raises(ValueError,match='escapes'):
        viz_library.apply_program({'entry':'../x.js','files':{'../x.js':'ok'}},0,'bad')
    assert not (tmp_path/'library.json').exists()


def test_evolution_extends_the_current_source_and_preserves_world_identity(tmp_path,monkeypatch):
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    current=viz_rule_author.seed_program()
    def model(prompt,name):
        assert name==viz_rule_author.TIER_TWO
        return json.dumps({'change':{'kind':'extension','evidence':'new path details','preserved':'Lantern Works'},
            'edits':[], 'new_files':{'detail.js':'exports.label=x=>x.path;'},'notes':'Add a reusable path detail helper'})
    result=viz_rule_author.evolve_world({'events':[],'coverage_keys':['hierarchy']},'expand',model)
    assert result['applied']==['decision:1']
    updated=viz_library.load()
    assert updated['scene_coverage']==['hierarchy']
    assert updated['program']['title']==current['title']
    assert all(updated['program']['files'][k]==v for k,v in current['files'].items())
    assert 'detail.js' in updated['program']['files']


def test_new_scene_reaches_astra_but_covered_scene_does_not(monkeypatch,tmp_path):
    from lib import viz_learning
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    calls=[]
    monkeypatch.setattr(viz_rule_author,'evolve_world',lambda scene,reason:calls.append(scene) or {'applied':['new'],'rejected':[]})
    scene={'coverage_keys':['entity:new'],'events':[],'entities':[]}
    assert viz_learning._develop_scene({'scene':scene,'example':'new'})['applied']==['new']
    assert calls==[scene]
    known=viz_library.seed();known['scene_coverage']=['entity:new']
    monkeypatch.setattr(viz_library,'load',lambda:known)
    assert not viz_learning._develop_scene({'scene':scene,'example':'same'})['applied']
    assert calls==[scene]


@pytest.mark.parametrize('exit_code,expected',[(0,0),(-15,143)])
def test_host_learning_flags_reach_preview_process(monkeypatch,exit_code,expected):
    import importlib.util
    import subprocess
    spec=importlib.util.spec_from_file_location('viz_host_test',Path(__file__).resolve().parents[2]/'scripts/viz_host.py')
    host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
    commands=[]
    monkeypatch.setattr(host.sys,'argv',['viz_host.py','--session','test','--db','/tmp/db','--library','/tmp/library.json','--learn'])
    monkeypatch.setattr(host.signal,'signal',lambda *args:None)
    monkeypatch.setattr(host.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,'job@g1\n',''))
    class Child:
        def __init__(self,command):commands.append(command)
        def wait(self,timeout):return exit_code
        def poll(self):return exit_code
    monkeypatch.setattr(host.subprocess,'Popen',Child)
    assert host.main()==expected
    assert commands[0][-3:]==['--library','/tmp/library.json','--learn']


def test_invalid_spark_alias_escalates_instead_of_stalling_the_observer(monkeypatch):
    from lib import viz_learning
    library=viz_library.seed();library.update(program={'title':'Existing'},scene_coverage=['entity:old'])
    monkeypatch.setattr(viz_library,'load',lambda:library)
    monkeypatch.setattr(viz_rule_author,'call_tier',lambda *a:json.dumps({'verdict':'variant','of':'invented'}))
    calls=[]
    monkeypatch.setattr(viz_rule_author,'evolve_world',lambda scene,reason:calls.append(scene) or {'applied':['new'],'rejected':[]})
    scene={'coverage_keys':['entity:new'],'entities':[]}
    assert viz_learning._develop_scene({'scene':scene,'example':'new'})['applied']==['new']
    assert calls==[scene]


def test_ordinary_evolution_refuses_whole_world_replacement():
    current={'title':'Lantern Works','entry':'world.js','files':{'world.js':'const boats=1;\nmodule.exports.render=()=>boats;'}}
    for reply in [
        {'change':{'kind':'redesign','evidence':'new filenames','preserved':'data'},'program':{'title':'Moths'}},
        {'change':{'kind':'extension','evidence':'new filenames','preserved':'data'},'program':{'title':'Moths'}},
        {'change':{'kind':'repair','evidence':'cleanup','preserved':'data'},'edits':[{'file':'world.js','before':current['files']['world.js'],'after':'moths();'}]},
        {'change':{'kind':'extension','evidence':'more actions','preserved':'boats'},'new_files':{'world.js':'moths();'}},
    ]:
        with pytest.raises(ValueError):viz_library.evolution_program(reply,current)


def test_focused_repair_leaves_unrelated_source_and_identity_unchanged():
    current={'title':'Lantern Works','entry':'world.js','files':{'world.js':'const speed=1;\nmodule.exports.render=()=>speed;','other.js':'unrelated'}}
    reply={'change':{'kind':'repair','evidence':'motion too fast','preserved':'boats and harbor'},'edits':[{'file':'world.js','before':'const speed=1;','after':'const speed=.5;'}]}
    result=viz_library.evolution_program(reply,current)
    assert result['files']['world.js']=='const speed=.5;\nmodule.exports.render=()=>speed;'
    assert result['files']['other.js']=='unrelated'
    assert result['title']==current['title'] and result['entry']==current['entry']
    assert current['files']['world.js'].startswith('const speed=1;')


def test_already_supported_evidence_can_leave_the_program_unchanged():
    current=viz_rule_author.seed_program()
    result=viz_library.evolution_program({'change':{'kind':'unchanged','evidence':'another existing file type','preserved':'all behavior'}},current)
    assert result==current


def test_unrequested_redesign_cannot_change_the_live_library(tmp_path,monkeypatch):
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    before=viz_library.apply_program(viz_rule_author.seed_program(),0,'baseline')
    def model(*args):
        return json.dumps({'change':{'kind':'redesign','evidence':'new delete event','preserved':'facts'},'program':{'title':'Moths','entry':'world.js','files':{'world.js':'module.exports.render=()=>({});'}}})
    with pytest.raises(ValueError,match='strong explicit demand'):
        viz_rule_author.evolve_world({'events':[],'coverage_keys':['action:delete']},'ordinary novelty',model)
    assert viz_library.load()==before


def test_avatar_upgrade_preserves_unrelated_learned_source():
    from lib.viz_avatar_upgrade import avatar_edits
    source="""const learned='message';
function vessel(c,x,y,name,col,phase,active){return 'boat';}
module.exports.render=({interaction={},reducedMotion=false})=>{
vessel(c,p.x,p.y,ev.agent||id,col,reducedMotion?0:ambient%1,active);
return learned;
};"""
    program={'title':'The Lantern Works','entry':'world.js','files':{'world.js':source,'model.js':'learned classification'}}
    result=viz_library.evolution_program(avatar_edits(program,'exports.drawAgentAvatar=()=>{};'),program)
    assert "const learned='message'" in result['files']['world.js']
    assert result['files']['model.js']=='learned classification'
    assert 'function vessel(' not in result['files']['world.js']
    assert 'avatars[id]' in result['files']['world.js']
    assert result['title']==program['title']


def test_fleet_payload_uses_exact_agent_avatar_routes(tmp_path):
    from lib import db,viz_normalize
    con=db.conn()
    for aid in ['portrait-a','portrait-b']:
        portrait=tmp_path/(aid+'.png');portrait.write_bytes(b'portrait')
        con.execute("INSERT INTO agents(agent_id,persona,voice_id,cwd,session,created_at,avatar_path) VALUES (?, 'Same name','','/tmp',?,1,?)",(aid,aid,str(portrait)))
        con.execute("INSERT INTO state_log(agent_id,ts,kind,detail) VALUES (?,1,'tool',?)",(aid,json.dumps({'tool':'Read','input':{}})))
    payload=viz_normalize.build_fleet_map(0)
    actors={a['id']:a for a in payload['actors']}
    assert actors['portrait-a']['avatar_url'].startswith('/avatars/portrait-a?v=')
    assert actors['portrait-b']['avatar_url'].startswith('/avatars/portrait-b?v=')
    assert actors['portrait-a']['avatar_url']!=actors['portrait-b']['avatar_url']


def test_every_author_prompt_includes_vision_and_accepted_decisions():
    records=viz_rule_author.decision_records()
    assert {'README.md','VISION.md','0002-compatible-evolution.md','0004-agent-avatars.md'} <= records.keys()
    prompt=viz_rule_author.world_prompt({'scene':{}},viz_library.seed())
    for name,content in records.items():
        assert json.dumps(name) in prompt
        assert json.dumps(content) in prompt


def test_installed_author_reads_the_shipped_decision_directory(tmp_path,monkeypatch):
    root=tmp_path/'release';(root/'lib').mkdir(parents=True)
    docs=root/'docs/architecture/fleet-map';docs.mkdir(parents=True)
    (docs/'README.md').write_text('Decision index')
    (docs/'VISION.md').write_text('Owner vision')
    (docs/'0001-example.md').write_text('Accepted requirement')
    monkeypatch.setattr(viz_rule_author,'__file__',str(root/'lib/viz_rule_author.py'))
    assert viz_rule_author.decision_records()=={'README.md':'Decision index','VISION.md':'Owner vision','0001-example.md':'Accepted requirement'}
    (docs/'VISION.md').unlink()
    with pytest.raises(RuntimeError,match='records are missing'):
        viz_rule_author.decision_records()


def test_author_view_switch_preserves_previous_visual_software(tmp_path,monkeypatch):
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    world=viz_rule_author.seed_program()
    old=viz_library.apply_program(world,0,'Existing world')
    flow={**world,'view':'flow','title':'Flow'}
    new=viz_library.apply_program(flow,old['revision'],'Owner-selected authoring view')
    assert new['program']['view']=='flow'
    assert new['view_programs']['world']==old['program']
    assert new['previous_program'] is None
    revised=viz_library.evolution_program({'change':{'kind':'unchanged','evidence':'supported','preserved':'all'}},new['program'])
    final=viz_library.apply_program(revised,new['revision'],'Reused')
    assert final['view_programs']['world']==old['program']
    assert final['previous_program']==new['program']
