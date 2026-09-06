import sqlite3
from lib import viz_work, viz_world


def _db():
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
    con.executescript('''
    CREATE TABLE task_plans(plan_id TEXT PRIMARY KEY,agent_id TEXT,session TEXT,title TEXT,status TEXT,created_at INTEGER,updated_at INTEGER,completed_at INTEGER);
    CREATE TABLE task_items(item_id TEXT PRIMARY KEY,plan_id TEXT,parent_id TEXT,position INTEGER,title TEXT,status TEXT,started_at INTEGER,completed_at INTEGER);
    CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY,agent_id TEXT,session TEXT,type TEXT,title TEXT,summary TEXT DEFAULT '',status TEXT,reference_id TEXT DEFAULT '',payload_json TEXT,created_at INTEGER,completed_at INTEGER,deleted_at INTEGER,updated_at INTEGER DEFAULT 0);
    CREATE TABLE media_assets(asset_id TEXT PRIMARY KEY,mime_type TEXT,width INTEGER,height INTEGER,bytes INTEGER,deleted_at INTEGER);
    CREATE TABLE prompt_admissions(admission_id TEXT PRIMARY KEY,origin TEXT,sender_agent_id TEXT,agent_id TEXT,session TEXT,observed_at INTEGER,original_text TEXT,trace_id TEXT);
    ''')
    con.execute("INSERT INTO task_plans VALUES('aaaaaaaaaaaaaaaa:pistol:deadbeef','a','axel','Compare six pistol approaches','completed',1000,5000,5000)")
    con.execute("INSERT INTO task_items VALUES('aaaaaaaaaaaaaaaa:pistol:deadbeef:verify','aaaaaaaaaaaaaaaa:pistol:deadbeef',NULL,0,'Check mechanics','completed',NULL,4000)")
    con.execute("INSERT INTO task_items VALUES('aaaaaaaaaaaaaaaa:pistol:deadbeef:sub','aaaaaaaaaaaaaaaa:pistol:deadbeef','aaaaaaaaaaaaaaaa:pistol:deadbeef:verify',0,'nested','completed',NULL,4000)")
    con.execute("INSERT INTO media_assets VALUES('asset_thumb','image/png',1440,1250,2000,NULL)")
    con.execute("INSERT INTO media_assets VALUES('asset_video','video/mp4',NULL,NULL,900000,NULL)")
    con.execute("INSERT INTO artifacts (artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at,deleted_at) VALUES('art-video','a','axel','video','Pistol feel lab','','ready','','{\"url\":\"/media/asset_video\",\"thumbnail_url\":\"/media/asset_thumb\",\"mime_type\":\"video/mp4\",\"duration_ms\":19000}',4900,NULL,NULL)")
    con.execute("INSERT INTO artifacts (artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at,deleted_at) VALUES('art-run','b','felix','workflow_run','Docker','','failed','','{\"repository\":\"Maxteabag/clarp\",\"conclusion\":\"failure\",\"branch\":\"feat/x\",\"run_url\":\"https://github.com/Maxteabag/clarp/actions/runs/1\"}',4500,NULL,NULL)")
    con.execute("INSERT INTO artifacts (artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at,deleted_at) VALUES('art-plan','a','axel','plan','Compare','','completed','aaaaaaaaaaaaaaaa:pistol:deadbeef','{}',1000,5000,NULL)")
    con.execute("INSERT INTO artifacts (artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at,deleted_at) VALUES('art-research','c','cagent','research','Host sync','','ready','','{\"content\":\"x\",\"sources\":[{\"title\":\"A\",\"url\":\"https://a\"},{\"title\":\"B\",\"url\":\"https://b\"}]}',4600,NULL,NULL)")
    con.execute("INSERT INTO artifacts (artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at,deleted_at) VALUES('art-missing','a','axel','image','Gone','','ready','','{\"url\":\"/media/asset_nope\"}',4700,NULL,NULL)")
    con.execute("INSERT INTO media_assets VALUES('asset_huge','image/png',20000,20000,3000,NULL)")
    con.execute("INSERT INTO media_assets VALUES('asset_unsized','image/png',NULL,NULL,3000,NULL)")
    con.execute("INSERT INTO media_assets VALUES('asset_heavy','image/png',800,600,9*1024*1024,NULL)")
    for name,asset in [('art-huge','asset_huge'),('art-unsized','asset_unsized'),('art-heavy','asset_heavy')]:
        con.execute("INSERT INTO artifacts (artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at,deleted_at) VALUES(?,'a','axel','image',?,'','ready','',?,4750,NULL,NULL)",(name,name,'{"url":"/media/%s"}'%asset))
    con.execute("INSERT INTO prompt_admissions VALUES('adm-1','agent','n','m','mappy',3000,'Plan aaaaaaaaaaaaaaaa:pistol:deadbeef is yours now, also ffffffffffffffff:other:00000000','t1')")
    con.execute("INSERT INTO prompt_admissions VALUES('adm-2','user','','m','mappy',3100,'hello','t2')")
    return con


def test_work_objects_separate_intent_evidence_basis_and_outcome():
    work=viz_work.build(_db(),0,10000,{'a':'Axel','n':'Nadia','m':'Mappy'})
    plan=work['plans'][0]
    assert plan['title']=='Compare six pistol approaches' and plan['agent']=='Axel' and plan['completed_at']==5000
    assert [i['id'] for i in plan['items']]==['verify'] and plan['item_total']==2
    kinds={a['id']:a for a in work['artifacts']}
    assert 'art-plan' not in kinds
    assert kinds['art-video']['preview']=={'url':'/media/asset_thumb','mime':'image/png','width':1440,'height':1250}
    assert kinds['art-video']['media']['duration_ms']==19000
    assert 'preview' not in kinds['art-missing'] and 'media_url' not in kinds['art-missing']
    for name in ('art-huge','art-unsized','art-heavy'):
        assert 'preview' not in kinds[name], name
        assert kinds[name]['media_url'].startswith('/media/asset_')
    assert kinds['art-video']['media_url']=='/media/asset_video'
    assert kinds['art-run']['remote_target']=='github:Maxteabag/clarp' and kinds['art-run']['run']['conclusion']=='failure'
    assert kinds['art-research']['source_count']==2 and kinds['art-research']['sources'][1]['url']=='https://b'
    assert [m['id'] for m in work['messages']]==['adm-1']
    assert work['messages'][0]['plan_ids']==['aaaaaaaaaaaaaaaa:pistol:deadbeef'] and work['messages'][0]['from']=='Nadia'
    assert work['messages'][0]['link']=='reference'
    assert set(work['contract'])=={'intent','evidence','validation','outcome','handoff','unknown'}


def test_missing_tables_degrade_to_an_unavailable_empty_section():
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
    work=viz_work.build(con,0,10,{})
    assert work['plans']==[] and work['available'] is False


def test_validation_evidence_distinguishes_single_commands_chains_and_scripts():
    chain=viz_world.validation_evidence('npm run test:pistol && npm run build')
    assert chain['kind']=='test' and chain['exact'] is True and chain['scope']=='chain' and chain['commands']==['npm run test:pistol','npm run build']
    single=viz_world.validation_evidence('npm run test:pistol')
    assert single['scope']=='single' and single['commands']==['npm run test:pistol']
    script=viz_world.validation_evidence('sed -i s/a/b/ tests/x.mjs; npm run test:x')
    assert script['kind']=='test' and script['exact'] is False and script['scope']=='script'
    assert viz_world.validation_evidence('false && npm test')['scope']=='chain'
    assert viz_world.validation_evidence('uv run --frozen --group dev python -m pytest')['kind']=='test'
    assert viz_world.validation_evidence('python make_fixture.py tests/x')['kind'] is None
    assert viz_world.validation_evidence("python - <<'PY'\nprint(1)\nPY")['kind'] is None
    fact=viz_world.evidence('Bash',{'command':'npm run test:mechanical','cwd':'/tmp'},'', 'x','run')
    assert fact['action']=='test' and fact['validation']=='test' and fact['validation_exact'] is True and fact['validation_scope']=='single'
    compound=viz_world.evidence('Bash',{'command':'npm run test:a && npm run build','cwd':'/tmp'},'', 'x','run')
    assert compound['action']=='execute' and compound['validation']=='test' and compound['validation_scope']=='chain'
    assert compound['validation_commands']==['npm run test:a','npm run build']


def test_validation_does_not_invent_results_from_pipelines_or_printed_commands():
    assert viz_world.validation_evidence('npm test | cat')['scope']=='script'
    assert viz_world.validation_evidence('echo pytest')['kind'] is None
    assert viz_world.validation_evidence("echo 'something && npm test'")['kind'] is None
    assert viz_world.validation_evidence("npm test -- -k 'one || two'")['scope']=='single'
