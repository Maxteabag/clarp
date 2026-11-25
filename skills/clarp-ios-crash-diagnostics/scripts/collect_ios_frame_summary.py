#!/usr/bin/env python3
"""Read and summarize real iOS frame samples without exporting messages or credentials."""
import argparse,datetime,json,math,re,sqlite3,statistics,time
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--database',type=Path,default=Path.home()/'.local/share/clarp-host/data/client-events.sqlite')
p.add_argument('--minutes',type=float,default=30);p.add_argument('--build');p.add_argument('--output',type=Path)
a=p.parse_args()
if a.minutes<=0 or not a.database.is_file():raise SystemExit('Existing client-events database and positive minutes required')
samples=[];seen=set()
with sqlite3.connect('file:'+str(a.database.resolve())+'?mode=ro',uri=True,timeout=3) as db:
 for received,detail,build in db.execute("SELECT received_at,detail,client_build FROM client_events WHERE received_at>=? AND event='ios.performance.frame-sample' ORDER BY received_at",((time.time()-a.minutes*60)*1000,)):
  try:payload=json.loads(detail or '""')
  except (TypeError,ValueError):continue
  text=payload if isinstance(payload,str) else payload.get('msg','') if isinstance(payload,dict) else ''
  values=dict(re.findall(r'(\w+)=([^ ]+)',text))
  values.setdefault('app_build',build)
  if a.build and values.get('app_build')!=a.build:continue
  key=(values.get('sampled_at_ms',str(received)),values.get('app_build'),values.get('screen'))
  if key in seen:continue
  seen.add(key)
  row={k:values[k] for k in ['app_build','app_version','screen','state','thermal'] if k in values}
  row['received_at_ms']=received
  for k in ['sampled_at_ms','fps','expected_fps','hitches','dropped','worst_ms','budget_ms','window_ms','rows','artifacts','agents','attention']:
   try:value=float(values[k])
   except (KeyError,ValueError):continue
   if math.isfinite(value):row[k]=value
  if 'fps' in row:samples.append(row)
groups={}
for row in samples:groups.setdefault(row.get('app_build','unknown')+'/'+row.get('screen','unknown'),[]).append(row)
result={'collected_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'minutes':a.minutes,'sample_count':len(samples),'groups':{},'samples':samples,'limits':'FPS statistics describe sampled windows, not a device-wide average or causal attribution. Compare matching builds, screens and actions; server receipt time can lag sampling.'}
for key,rows in groups.items():
 fps=[r['fps'] for r in rows];result['groups'][key]={'samples':len(rows),'min_fps':min(fps),'median_fps':statistics.median(fps),'max_fps':max(fps),'worst_frame_ms':max(r.get('worst_ms',0) for r in rows)}
text=json.dumps(result,indent=2)+'\n'
if a.output:
 a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text)
 print(json.dumps({'samples':len(samples),'groups':result['groups'],'output':str(a.output)}))
else:print(text)
