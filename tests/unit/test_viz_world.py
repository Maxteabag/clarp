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


def test_evolution_applies_executable_source_without_a_rule_or_icon(tmp_path,monkeypatch):
    monkeypatch.setattr(viz_library,'path',lambda:tmp_path/'library.json')
    def model(prompt,name):
        assert name==viz_rule_author.TIER_TWO
        return json.dumps({'program':{'title':'New system','entry':'scene.js','files':{
            'scene.js':'module.exports.render=({ctx})=>{ctx.fillRect(0,0,100,100);return {title:"New system"};};'}},'notes':'A new system'})
    result=viz_rule_author.evolve_world({'events':[],'coverage_keys':['hierarchy']},'reinvent',model)
    assert result['applied']==['decision:1']
    assert viz_library.load()['scene_coverage']==['hierarchy']


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


def test_host_learning_flags_reach_preview_process(monkeypatch):
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
        def wait(self,timeout):return 0
        def poll(self):return 0
    monkeypatch.setattr(host.subprocess,'Popen',Child)
    assert host.main()==0
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
