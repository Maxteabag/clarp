import pytest
from lib import agents,artifacts,attention_index,db

def seed(tmp_path):
 agents.create_agent(persona='Mike',voice_id='V',cwd=str(tmp_path),session='mike')
def make(t='document',status='ready',payload=None):
 return artifacts.create(session='mike',type=t,title='Result',status=status,payload=payload or {'content':'text'})
def test_eligibility_before_pagination_and_old_results(tmp_path):
 seed(tmp_path);old=make();
 # Newer unrelated source work must never displace an eligible artifact.
 for i in range(250):make(status='draft')
 result=attention_index.page(limit=1)
 assert result['total']==1 and result['artifacts'][0]['artifact_id']==old['artifact_id']
 assert result['artifacts'][0]['attention_bucket']=='review'
def test_paging_and_stale_archive_restart(tmp_path):
 seed(tmp_path);a=make();make();first=attention_index.page(limit=1);second=attention_index.page(limit=1,cursor=first['next_cursor'])
 assert len({first['artifacts'][0]['artifact_id'],second['artifacts'][0]['artifact_id']})==2
 assert second['next_cursor'] is None
 db.conn().execute('UPDATE artifacts SET archived_at=1 WHERE artifact_id=?',(a['artifact_id'],));db.conn().commit()
 with pytest.raises(attention_index.StaleCursor):attention_index.page(cursor=first['next_cursor'])
def test_source_outcomes_do_not_infer_resolution(tmp_path):
 seed(tmp_path);make(status='failed');make(status='active');make(status='cancelled');make(status='completed')
 result=attention_index.page();assert [r['attention_bucket'] for r in result['artifacts']]==['blocking','review','working']
def test_invalid_cursor_and_limits(tmp_path):
 seed(tmp_path);make()
 with pytest.raises(ValueError):attention_index.page(cursor='invalid')
 assert len(attention_index.page(limit=999)['artifacts'])==1

def test_workflow_attempt_dedupe_and_failure_preservation(tmp_path):
 seed(tmp_path)
 payload={'provider':'github','repository':'example/app','run_id':'1','run_url':'https://github.com/example/app/actions/runs/1','workflow_name':'Build'}
 one=make('workflow_run','failed',{**payload,'conclusion':'failure'})
 two=make('workflow_run','failed',{**payload,'conclusion':'failure'})
 # Source supports preserving attempt metadata in payload even before public promotion.
 attempt=make('workflow_run','completed',{**payload,'run_attempt':2,'conclusion':'success'})
 result=attention_index.page()
 assert result['total']==2
 assert [r['attention_bucket'] for r in result['artifacts']]==['blocking','review']
 assert artifacts.get(one['artifact_id']) and artifacts.get(two['artifact_id'])

def test_countdown_integration_preserves_unsettled_policy(tmp_path):
 seed(tmp_path)
 row=make('countdown','ready',{'target_at':'2000-01-01T00:00:00Z','time_zone':'UTC','purpose':'informational'})
 result=attention_index.page()
 assert result['artifacts'][0]['attention_bucket']=='review'
 assert artifacts.get(row['artifact_id'])['status']=='ready'
 db.conn().execute('UPDATE artifacts SET payload_json=? WHERE artifact_id=?',('{"target_at":"invalid"}',row['artifact_id']));db.conn().commit()
 assert attention_index.page()['artifacts'][0]['attention_bucket']=='blocking'
