import importlib.util,json
from pathlib import Path
from types import SimpleNamespace

def test_snapshot_pins_attempt_and_retains_metadata(monkeypatch,tmp_path):
 path=Path(__file__).resolve().parents[2]/'scripts/github_workflow_artifact.py'
 spec=importlib.util.spec_from_file_location('workflow_attempt_probe',path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 calls=[]
 def run(args,**kwargs):
  calls.append(args);return SimpleNamespace(stdout=json.dumps({'databaseId':42,'attempt':2,'status':'completed','conclusion':'failure','url':'https://github.com/example/app/actions/runs/42/attempts/2','jobs':[]}))
 monkeypatch.setattr(mod.subprocess,'run',run)
 result=mod.snapshot('example/app','42',2)
 assert result['run_attempt']==2
 assert calls[0][calls[0].index('--attempt')+1]=='2'
 assert mod.state(result)=='failed'

 from lib import agents,artifacts
 agents.create_agent(persona='Workflow',voice_id='v',cwd=str(tmp_path),session='workflow-probe')
 created=artifacts.create(session='workflow-probe',type='workflow_run',title='CI',status=mod.state(result),payload=result)
 stored=artifacts.get(created['artifact_id'])
 assert stored['payload']['run_url']=='https://github.com/example/app/actions/runs/42'
 assert stored['payload']['run_attempt']==2 and stored['status']=='failed'
