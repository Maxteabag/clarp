"""927 post-switch ERROR counts only. Raw timestamped records remain immutable."""
from pathlib import Path
import datetime,hashlib,json,math,re
CATEGORIES=('http_5xx','runtime_writer_giveups')
REPORT_ONLY='db_locked_records'
CATCHUP={'http_5xx':15,'runtime_writer_giveups':10}
FLOORS={'http_5xx':5,'runtime_writer_giveups':2}
class Unknown(RuntimeError):pass

def finite(n):return type(n) in (int,float) and math.isfinite(n)
def usec(n):
 if not finite(n):raise Unknown('clock_schema')
 return round(n*1000000)
def stamp(text):
 d=datetime.datetime.fromisoformat(text)
 if d.tzinfo is None:raise Unknown('clock_timezone')
 return d.timestamp()
def bounded(path,limit=1048576):
 p=Path(path)
 if p.is_symlink():raise Unknown('metadata_symlink')
 with p.open('rb') as f:raw=f.read(limit+1)
 if len(raw)>limit:raise Unknown('metadata_bound')
 return raw

def boot_window(request,healthy,health_rows,context,unit,now,mono,tag,host,runtime,boot):
 """Exact owned830 start, positive same-native-identity healthy, same clock domain."""
 if (not isinstance(request,dict) or type(request.get('authority')) is not int or request.get('authority')!=830
     or type(request.get('attempt')) is not int or request.get('attempt')!=1
     or request.get('argv')!=['systemctl','--user','restart','clarp-runtime.service']
     or request.get('tag')!=tag or not isinstance(context,dict) or context.get('tag')!=tag
     or request.get('supervisor_pid')!=context.get('supervisor_pid')
     or request.get('supervisor_start')!=context.get('supervisor_start')
     or any(type(request.get(k)) is not int or request[k]<=0 for k in ('supervisor_pid','supervisor_start'))
     or not isinstance(boot,str) or not boot
     or request.get('boot')!=context.get('boot') or boot!=context.get('boot')):raise Unknown('foreign_start')
 start=request.get('epoch');start_mono=request.get('monotonic')
 if (not all(finite(v) for v in (start,start_mono,now,mono,context.get('launch_epoch'),context.get('launch_mono')))
     or not context['launch_epoch']<=start<=now or not context['launch_mono']<=start_mono<=mono
     or abs((now-start)-(mono-start_mono))>5):raise Unknown('start_clock')
 if healthy is None:return {'state':'PENDING','start_epoch':start,'healthy_epoch':None,'eligible_counts':'NULL_UNTIL_POSITIVE_HEALTHY'}
 if not isinstance(healthy,dict) or not isinstance(health_rows,list) or len(health_rows)>512:raise Unknown('healthy_schema')
 q=healthy.get('probe') if isinstance(healthy,dict) else None
 if (not isinstance(q,dict) or not isinstance(unit,dict) or any(not isinstance(r,dict) for r in health_rows)
     or healthy.get('host_on_installed') is not True or healthy.get('runtime_on_installed') is not True
     or q.get('health_ready') is not True or q.get('runtime_available') is not True
     or q.get('release_id')!=host or q.get('runtime_release_id')!=runtime
     or unit.get('ActiveState')!='active' or not re.fullmatch('[0-9a-f]{32}',str(unit.get('InvocationID','')))
     or not str(unit.get('MainPID','')).isdigit() or int(unit['MainPID'])<=0):raise Unknown('healthy_foreign')
 positives=[]
 for row in health_rows:
  if (row.get('host')==host and row.get('runtime')==runtime and row.get('available') is True
      and row.get('ready') is True and row.get('unit_active')=='active'
      and row.get('invocation')==unit['InvocationID'] and str(row.get('runtime_pid'))==unit['MainPID']):
   positives.append(stamp(row['t']))
 if not positives:raise Unknown('healthy_native_join')
 healthy_epoch=max(stamp(q['t']),min(positives))
 if not start<=healthy_epoch<=now:raise Unknown('healthy_clock')
 return {'state':'PROVEN','start_epoch':start,'healthy_epoch':healthy_epoch,
         'start_us':usec(start),'end_us':usec(healthy_epoch+60),
         'healthy_plus60_epoch':healthy_epoch+60,'invocation':unit['InvocationID'],
         'runtime_pid':unit['MainPID'],'boot':boot,'tag':tag,'host':host,'runtime':runtime}

def load_boot(logs,tag,context,unit,row,host,runtime,runtime_started):
 """Missing/foreign/unknown never means an excluded record or a mature phase."""
 if not runtime_started:return {'state':'NO_ACTIVATION_YET','healthy_epoch':None}
 refs={};base=Path(logs)
 try:
  def read(s):
   p=base/(tag+'.runtime830'+s);raw=bounded(p);refs[str(p)]=hashlib.sha256(raw).hexdigest();return raw
  request=json.loads(read('.restart.requested.json'))
  hp=base/(tag+'.runtime830.healthy.json')
  healthy=json.loads(read('.healthy.json')) if hp.exists() else None
  rows=[json.loads(x) for x in read('.health.jsonl').splitlines() if x.strip()] if healthy is not None else []
  result=boot_window(request,healthy,rows,context,unit,row['epoch'],row['mono'],tag,host,runtime,row['boot'])
  result['evidence']=refs;return result
 except (OSError,ValueError,KeyError,TypeError,Unknown) as e:return {'state':'UNKNOWN','code':str(e) if isinstance(e,Unknown) else type(e).__name__,'evidence':refs}

def journal_records(run,since,until,category,units,pattern):
 a,b=usec(since),usec(until);args=['journalctl','--user']
 for unit in units:args+=['-u',unit]
 args+=['--since','@'+str(since),'--until','@'+str(until),'--grep',pattern,'--output=json','--no-pager','-n','1000']
 r=run(args,35)
 if r.returncode==1 and not r.stdout.strip() and not r.stderr.strip():return []
 lines=r.stdout.splitlines()
 if r.returncode or len(lines)>=1000:raise Unknown('journal_coverage')
 values=[];seen=set()
 for line in lines:
  row=json.loads(line);value=row['__REALTIME_TIMESTAMP']
  if type(value) is not int and not (isinstance(value,str) and value.isdigit()):raise Unknown('journal_timestamp')
  t=int(value);identity=row.get('__CURSOR')
  if not isinstance(identity,str) or not identity or identity in seen:raise Unknown('journal_identity')
  seen.add(identity)
  if t<a or t>b:raise Unknown('journal_interval')
  if a<=t<b:values.append({'at_us':t,'id':hashlib.sha256(identity.encode()).hexdigest(),'code':category})
 return values

def capture(telemetry,run,since,until):
 """Independent bounded producer reads; a DB report fault cannot suppress HTTP/giveups."""
 if usec(until)<usec(since):raise Unknown('interval_clock')
 result={};faults={}
 for category,units,pattern in [('db_locked_records',['clarp.service','clarp-runtime.service'],'database is locked'),('runtime_writer_giveups',['clarp-runtime.service'],'sqliteWriteWait.*outcome=gave_up')]:
  try:result[category]=journal_records(run,since,until,category,units,pattern)
  except Exception as e:result[category]=None;faults[category]=str(e) if isinstance(e,Unknown) else type(e).__name__
 try:
  rows=telemetry.execute('SELECT event_id,ts FROM diagnostic_events WHERE ts>=? AND ts<? AND status>=500 ORDER BY ts,event_id LIMIT 1001',(int(since*1000),int(until*1000))).fetchall()
  if len(rows)>1000:raise Unknown('http_coverage')
  values=[];seen=set()
  for identity,t in rows:
   if identity is None or type(t) is not int or identity in seen:raise Unknown('http_identity')
   seen.add(identity);values.append({'at_us':t*1000,'id':hashlib.sha256(str(identity).encode()).hexdigest(),'code':'http_5xx'})
  result['http_5xx']=values
 except Exception as e:result['http_5xx']=None;faults['http_5xx']=str(e) if isinstance(e,Unknown) else type(e).__name__
 return {'since_epoch':since,'until_epoch':until,'raw_records':result,'capture_unknown':faults,'producer':'samejournalgrep+HTTPstatus>=500;timestamps+hashedidentityONLY'}

def partition(intervals,boot,window_since,window_until):
 raw={};excluded={};eligible={};faults={}
 for category in (REPORT_ONLY,)+CATEGORIES:
  total=inside=outside=0;seen=set()
  try:
   for interval in intervals:
    a,b=interval['since_epoch'],interval['until_epoch']
    if not finite(a) or not finite(b) or b<a or b>window_until:raise Unknown('interval_schema')
    events=interval['raw_records'].get(category)
    if not isinstance(events,list):raise Unknown('raw_records')
    legacy=interval.get('legacy_counts',{}).get(category)
    if type(legacy) is not int or legacy<0 or len(events)!=legacy:raise Unknown('raw_producer_count_mismatch')
    for event in events:
     if (not isinstance(event,dict) or type(event.get('at_us')) is not int or event.get('code')!=category
         or not isinstance(event.get('id'),str) or not event['id']):raise Unknown('event_schema')
     lo,hi=(int(a*1000)*1000,int(b*1000)*1000) if category=='http_5xx' else(usec(a),usec(b))
     if not lo<=event['at_us']<hi:raise Unknown('event_outside_producer_interval')
     if not usec(window_since)<=event['at_us']<usec(window_until):continue
     if event['id'] in seen:raise Unknown('event_duplicate')
     seen.add(event['id']);total+=1
     if category in CATEGORIES and boot['state']=='PROVEN' and boot['start_us']<=event['at_us']<=boot['end_us']:inside+=1
     else:outside+=1
   raw[category]=total
   if category in CATEGORIES:
    if boot['state'] not in ('PROVEN','NO_ACTIVATION_YET'):eligible[category]=excluded[category]=None
    else:
     eligible[category]=outside;excluded[category]=inside
     assert total==inside+outside
  except (KeyError,TypeError,AttributeError,ValueError,Unknown) as e:
   raw[category]=None;faults[category]=str(e)
   if category in CATEGORIES:eligible[category]=excluded[category]=None
 return {'raw':raw,'eligible':eligible,'excluded':excluded,'capture_unknown':faults,'boot_state':boot['state'],'state':'PARTITION_PARTIAL_UNKNOWN' if faults else 'PARTITION_PROVEN' if boot['state'] in ('PROVEN','NO_ACTIVATION_YET') else boot['state'],'window_since':window_since,'window_until':window_until}


def apply(row,decision,intervals,boot,baseline,now):
 """Replace ONLY count decisions; DB always report-only; other decisions byte-for-byte carry."""
 result={'triggers':[k for k in decision['triggers'] if k not in CATEGORIES+(REPORT_ONLY,)],
         'unknown':[k for k in decision['unknown'] if k not in CATEGORIES]}
 try:
  switch=row.get('actual_Host_switch_epoch')
  if not finite(switch) or not finite(now) or now<switch:raise Unknown('Host_switch_clock')
  parts=partition(intervals,boot,max(switch,now-1800),now)
 except (KeyError,TypeError,ValueError,Unknown) as e:parts={'state':'UNKNOWN','code':str(e),'eligible':dict.fromkeys(CATEGORIES,None),'excluded':dict.fromkeys(CATEGORIES,None)}
 healthy=boot.get('healthy_epoch');mature=finite(healthy) and finite(now) and now>=healthy+2700
 phase='MATURE_AFTER_HEALTHY45' if mature else 'CATCHUP_HEALTHY45' if finite(healthy) else 'HEALTHY_CLOCK_PENDING'
 if not finite(healthy):result['unknown'].append('runtime_healthy_clock_pending')
 for k in CATEGORIES:
  count=parts['eligible'][k];before=baseline.get('baseline_error_counts',{}).get(k)
  if type(count) is not int or count<0:
   result['unknown'].append(k);continue
  if mature:
   if type(before) is not int or before<0:result['unknown'].append(k)
   elif count>FLOORS[k] and count>3*before:result['triggers'].append(k)
  elif count>CATCHUP[k]:result['triggers'].append(k)
 row['error_policy927']={'phase':phase,'boot_window':boot,'partition':parts,'db_locked':'REPORT_ONLY_NEVER_ALONE_ROLLBACK','raw_legacy_post_counts':dict(row.get('post_error_counts',{})),'baseline_fixed':True}
 return result
