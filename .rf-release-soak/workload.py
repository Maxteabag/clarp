#!/usr/bin/env python3
"""Runs INSIDE one owned synthetic canary only. Never reads real transcripts."""
import argparse,json,os,time,urllib.request,urllib.error
from pathlib import Path
BASE='http://127.0.0.1:7682'
def binding():return {'nonce':os.environ['HEAVY_SOAK_NONCE'],'remote_job_id':os.environ['HEAVY_SOAK_REMOTE_JOB_ID'],'boot_id':os.environ['HEAVY_SOAK_BOOT_ID']}
def write(path,data):
 tmp=path.with_suffix('.pending');tmp.write_text(json.dumps(data,sort_keys=True)+'\n');os.replace(tmp,path)
def request(path,data=None):
 body=None if data is None else json.dumps(data).encode()
 req=urllib.request.Request(BASE+path,data=body,headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=15) as response:
  result=json.load(response)
  if response.status not in (200,201,202):raise RuntimeError('unexpected HTTP code '+str(response.status))
 return result

def verify(state):
 counts={}
 for sent in state['sent']:
  turns=request('/log?session='+sent['session']).get('turns') or []
  u=sum(t.get('role')=='user' and t.get('text')==sent['text'] for t in turns)
  a=sum(t.get('role')=='assistant' and t.get('text')=='QA reply: '+sent['text'] for t in turns)
  counts[sent['mid']]={'users':u,'replies':a}
 return counts

def batch(state,index):
 sent=[]
 for key,suffix in [('a','1'),('b','1'),('b','2')]:
  session=state['agents'][key];mid='soak-'+str(index)+'-'+key+suffix;text='synthetic '+mid
  if key=='b' and suffix=='2':
   until=time.monotonic()+15
   while request('/status?session='+session).get('busy') is not True:
    if time.monotonic()>=until:raise RuntimeError('queued-turn busy precondition unproven')
    time.sleep(.05)
  result=request('/send',{'session':session,'text':text,'client_msg_id':mid,'queue_if_busy':True,'synthesize_audio':False})
  if key=='b' and suffix=='2' and result.get('queued') is not True:raise RuntimeError('synthetic followup was not queued')
  sent.append({'session':session,'mid':mid,'text':text})
 state['sent'].extend(sent)
 until=time.monotonic()+180
 while True:
  counts=verify({'sent':sent})
  if all(v=={'users':1,'replies':1} for v in counts.values()):return counts
  if time.monotonic()>=until:raise RuntimeError('synthetic reply equality deadline exceeded')
  time.sleep(.25)

def main():
 parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['run','verify','compat-send']);parser.add_argument('--state',type=Path,required=True);parser.add_argument('--seconds',type=int,default=21600);parser.add_argument('--rehearsal',action='store_true');args=parser.parse_args()
 if args.mode!='run':
  state=json.loads(args.state.read_text())
  if args.mode=='compat-send':batch(state,'compat-'+str(time.time_ns()));write(args.state,state)
  counts=verify(state);ok=all(v=={'users':1,'replies':1} for v in counts.values());print(json.dumps({'ok':ok,'messages':len(counts),'bad':sum(v!={'users':1,'replies':1} for v in counts.values())}));return 0 if ok else 1
 if args.seconds!=21600 and not(args.rehearsal and 1<=args.seconds<=60):raise RuntimeError('full soak is exactly21600s; short mode requires labelled rehearsal')
 state={'agents':{},'sent':[],'binding':binding()}
 for key in ('a','b','c'):state['agents'][key]=request('/agents',{'name':'Synthetic soak '+key,'backend':'codex','cwd':'/home/canary'})['session']
 write(args.state,state);start=time.monotonic();deadline=start+args.seconds;next_batch=start;next_request=start;polls=0;batches=0;started=args.state.with_suffix('.started.json');result=args.state.with_suffix('.result.json')
 write(started,{'pid':os.getpid(),'started_epoch':time.time(),'started_monotonic':start,'seconds_required':args.seconds,'synthetic_data_only':True,'rehearsal':args.rehearsal,'binding':binding()})
 try:
  while time.monotonic()<deadline:
   now=time.monotonic()
   if now>=next_batch and not Path('/home/canary/soak-pause-sends').exists():
    batch(state,batches);batches+=1;write(args.state,state);next_batch=start+batches*900
   if now>=next_request:
    keys=('a','b','c');key=keys[polls%3];request('/log?session='+state['agents'][key]);polls+=1;next_request=time.monotonic()+1/3
   time.sleep(min(.05,max(0,deadline-time.monotonic())))
  counts=verify(state);elapsed=time.monotonic()-start
  if elapsed<args.seconds or not all(v=={'users':1,'replies':1} for v in counts.values()):raise RuntimeError('full interval/equality not proven')
  write(result,{'result':'PASS','started_monotonic':start,'finished_monotonic':time.monotonic(),'elapsed_seconds':elapsed,'required_seconds':args.seconds,'batches':batches,'polls':polls,'messages':len(counts),'synthetic_data_only':True,'rehearsal':args.rehearsal,'binding':binding()});return 0
 except Exception as error:
  write(result,{'result':'FAIL','elapsed_seconds':time.monotonic()-start,'error_type':type(error).__name__,'reason':str(error),'batches':batches,'polls':polls,'rehearsal':args.rehearsal,'binding':binding()});raise
if __name__=='__main__':raise SystemExit(main())
