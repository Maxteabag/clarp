import json,tempfile,pathlib,concurrent.futures,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'server/lib'))
from audio_fact_ledger import AudioFactLedger
with tempfile.TemporaryDirectory() as tmp:
 ledger=AudioFactLedger(pathlib.Path(tmp)/'facts.sqlite')
 base=dict(host='h1',conversation='c1',message='m1',clip='clip1',revision=2,stage='authored',occurrence='once',fact='spoken',owner='worker',generation=1)
 rows=[]
 def proposal(**kw):
  e=base|kw;ledger.seed(e,owner="worker",generation=1,revision=2,reader="worker");e['producerEvent']=ledger.produce(e);return e
 def test(name,e,expected,delta=0,**kw):
  before=ledger.count();caller=kw.pop('caller','worker');got=ledger.apply(e,name,caller=caller,**kw);after=ledger.count();rows.append(dict(case=name,actual=got,expected=expected,effectDelta=after-before,expectedEffectDelta=delta,passed=got==expected and after-before==delta))
 e=proposal();test('initial',e,'accepted',1);test('new-transport-envelope-same-effect',e,'replay')
 resumed=proposal();rows.append(dict(case='producer-restart-stable-event',passed=resumed['producerEvent']==e['producerEvent']))
 test('conflict',e|{'fact':'written'},'conflict')
 for k,v in [('host','h2'),('conversation','c2'),('clip','clip2')]:test('collision-isolated-'+k,proposal(**{k:v}),'accepted',1)
 for field,value in [('owner','new-worker'),('generation',2),('revision',3),('cancelled',1)]:
  p=proposal(message=field);ledger.authority(p,**{field:value});test('trusted-state-changed-'+field,p,'cancelled' if field=='cancelled' else 'stale_authority')
 p=proposal(message='spoof');ledger.authority(p,cancelled=1);test('payload-cannot-clear-cancellation',p|{'cancelled':False},'cancelled')
 test('wrong-caller-receipt',e,'denied',caller='other');ledger.authority(e,reader='revoked');test('revoked-receipt-denied',e,'denied');ledger.authority(e,reader='worker',cancelled=1);test('authorized-cancelled-receipt-replay',e,'replay')
 test('regenerated-producer-id-rejected',e|{'producerEvent':'invented'},'unknown_producer_event')
 for attempt in ['playback:1','playback:2']:
  p=proposal(message='play',stage='delivered',occurrence=attempt);test(attempt,p,'accepted',1);test(attempt+'-retry',p,'replay')
 test('single-stage-cannot-invent-occurrence',proposal(message='invalid',occurrence='twice'),'invalid_occurrence')
 test('unknown-fact',proposal(message='unknown',fact=None),'needs_judgment')
 p=proposal(message='before');test('rollback',p,'rollback_before_commit',fault='before');test('retry-rollback',p,'accepted',1)
 p=proposal(message='after');test('lost-ack',p,'lost_ack_after_commit',1,fault='after');test('retry-lost-ack',p,'replay')
 p=proposal(message='race');before=ledger.count()
 with concurrent.futures.ThreadPoolExecutor(2) as pool:got=sorted(pool.map(lambda n:ledger.apply(p,n,caller="worker"),['race1','race2']))
 rows.append(dict(case='two-writers',actual=got,effectDelta=ledger.count()-before,passed=got==['accepted','replay'] and ledger.count()-before==1))
 print(json.dumps({'scope':'Candidate audio_fact_ledger API, disposable SQLite; no live ingestion/provider','checks':rows,'passed':sum(r['passed'] for r in rows),'total':len(rows)},indent=2))
 assert all(r['passed'] for r in rows)
