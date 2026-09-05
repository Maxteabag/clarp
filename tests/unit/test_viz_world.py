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
