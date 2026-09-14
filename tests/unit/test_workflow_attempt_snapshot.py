import importlib.util,json
from pathlib import Path
from types import SimpleNamespace

def test_snapshot_pins_attempt_and_retains_metadata(monkeypatch):
 path=Path(__file__).resolve().parents[2]/'scripts/github_workflow_artifact.py'
 spec=importlib.util.spec_from_file_location('workflow_attempt_probe',path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 calls=[]
 def run(args,**kwargs):
  calls.append(args);return SimpleNamespace(stdout=json.dumps({'databaseId':42,'attempt':2,'status':'completed','conclusion':'failure','url':'https://github.com/example/app/actions/runs/42','jobs':[]}))
 monkeypatch.setattr(mod.subprocess,'run',run)
 result=mod.snapshot('example/app','42',2)
 assert result['run_attempt']==2
 assert calls[0][calls[0].index('--attempt')+1]=='2'
 assert mod.state(result)=='failed'
