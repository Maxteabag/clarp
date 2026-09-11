"""Read native tool evidence without modifying the shared runtime or its schema.

The runtime's recorded backend session and lifetime bind events to an agent.
Codex item_completed records retain the command, cwd, parsed targets and exact
start/end times lost by older state_log writers. Read incrementally, never load
whole transcripts or tool output into the map's long-lived cache.
"""
from __future__ import annotations
import collections
import datetime
import json
import pathlib
import threading
from urllib.parse import unquote,urlsplit
from .codex_transcript import find_latest_jsonl

_lock=threading.Lock()
_cache={}


def local_path(value):
    value=str(value or '')
    if value.startswith('file:'):
        parsed=urlsplit(value)
        if parsed.netloc not in {'','localhost'}:return ''
        value=unquote(parsed.path)
    return value


def timestamp(value):
    try:return int(datetime.datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()*1000)
    except (ValueError,TypeError):return 0


_paths={}
def locate(sid):
    import re
    if not re.fullmatch(r'[0-9a-fA-F-]{36}',str(sid)):return None
    if sid in _paths and _paths[sid].is_file():return _paths[sid]
    result=find_latest_jsonl(sid)
    if result:_paths[sid]=result
    if len(_paths)>128:_paths.pop(next(iter(_paths)))
    return result


class Source:
    def __init__(self):
        self.offset=0;self.inode=None;self.cwd='';self.session='';self.last_ts=0
        self.events=collections.OrderedDict();self.pending={};self.supported=False;self.incomplete=False

    def scan(self,path):
        stat=path.stat()
        if self.inode!=stat.st_ino or stat.st_size<self.offset:self.__init__();self.inode=stat.st_ino
        with path.open('rb') as f:
            f.seek(self.offset)
            while f.tell()<stat.st_size:
                start=f.tell();line=f.readline(8*1024*1024)
                if not line.endswith(b'\n'):
                    if len(line)>=8*1024*1024:
                        self.incomplete=True
                        while line and not line.endswith(b'\n'):line=f.readline(1024*1024)
                        self.offset=f.tell();continue
                    break
                self.offset=f.tell()
                try:d=json.loads(line)
                except (ValueError,TypeError):continue
                ts=timestamp(d.get('timestamp'));self.last_ts=max(self.last_ts,ts)
                p=d.get('payload') or {}
                if not isinstance(p,dict):continue
                typ=d.get('type')
                if typ=='session_meta':self.session=str(p.get('id',''));self.cwd=local_path(p.get('cwd'))
                if typ=='turn_context':self.cwd=local_path(p.get('cwd')) or self.cwd
                if typ=='event_msg' and p.get('type') in {'item_completed','item_started','item_updated'}:
                    item=p.get('item',{})
                    if not isinstance(item,dict) or item.get('type') in {'Reasoning','AgentMessage','UserMessage','Plan','ContextCompaction'}:continue
                    self.supported=True
                    key=str(item.get('id') or '')
                    if not key:continue
                    begin=p.get('started_at_ms') or ts
                    end=p.get('completed_at_ms') if p['type']=='item_completed' else None
                    info=self.detail(item,begin,end)
                    self.events[key]=info
                if typ=='response_item':
                    kind=p.get('type');call=p.get('call_id')
                    if kind in {'function_call','custom_tool_call'} and call:
                        self.pending[call]={'ts':ts,'cwd':self.cwd,'tool':str(p.get('name') or 'tool')}
                    elif kind in {'function_call_output','custom_tool_call_output'}:self.pending.pop(call,None)
                if typ=='event_msg' and p.get('type') in {'task_complete','turn_aborted','turn_failed'}:
                    self.pending.clear()
                while len(self.events)>5000:self.events.popitem(last=False)
                while len(self.pending)>200:self.pending.pop(next(iter(self.pending)))

    def detail(self,item,begin,end):
        kind=str(item.get('type','tool'));cmd=item.get('command') or ''
        if isinstance(cmd,list):cmd=cmd[-1] if len(cmd)>=3 and cmd[1] in {'-lc','-c'} else ' '.join(cmd)
        cwd=local_path(item.get('cwd')) or self.cwd
        paths=[];action=None
        for parsed in item.get('parsed_cmd',[]) or []:
            if not isinstance(parsed,dict):continue
            if parsed.get('path'):paths.append(local_path(parsed['path']))
            if parsed.get('type') in {'read','search','list_files'}:action={'list_files':'search'}.get(parsed['type'],parsed['type'])
        if len(item.get('parsed_cmd',[]) or [])!=1:action=None
        changes=item.get('changes') or {}
        if kind=='FileChange':
            if isinstance(changes,dict):paths.extend(changes)
            elif isinstance(changes,list):paths.extend(c['path'] for c in changes if isinstance(c,dict) and c.get('path'))
            ops=[x.get('type') for x in changes.values() if isinstance(x,dict)] if isinstance(changes,dict) else []
            action='delete' if ops and all(x=='delete' for x in ops) else 'create' if ops and all(x=='add' for x in ops) else 'edit'
        if kind=='ImageView' and item.get('path'):paths=[local_path(item['path'])];action='media'
        code=item.get('exit_code');status=str(item.get('status') or '').lower()
        outcome=('failed' if (isinstance(code,int) and code!=0) or status in {'failed','declined','error','cancelled'}
                 else 'succeeded' if end is not None and (code==0 or status in {'completed','succeeded','success'}) else 'unknown')
        tool={'CommandExecution':'Bash','FileChange':'file_change','ImageView':'image_view'}.get(kind,kind)
        inp={'command':str(cmd)[:65536],'cwd':cwd,'paths':paths,'native_targets':True}
        return {'ts':begin,'finished_at':end,'outcome':outcome,'tool':tool,'input':inp,'action':action,
                'phase':'tool_started','cwd':cwd,'native':True,'location_basis':'native tool record'}


def tool_rows(con,since,until):
    """Return rich rows plus runtime IDs safely covered by native item records."""
    now=until if until<(1<<61) else 1<<62
    runtimes=con.execute('SELECT r.runtime_id,r.agent_id,r.backend_session_id,r.started_at,r.ended_at '
        'FROM runtimes r JOIN agents a USING(agent_id) WHERE a.backend=? AND r.started_at<=? '
        'AND (r.ended_at IS NULL OR r.ended_at>=?) AND r.backend_session_id IS NOT NULL',('codex',now,since)).fetchall()
    rows=[];covered=set()
    with _lock:
        for r in runtimes:
            sid=r['backend_session_id']
            path=locate(sid)
            if not path:continue
            source=_cache.setdefault(str(path),Source())
            try:source.scan(path)
            except OSError:continue
            if source.session!=sid or not source.supported or source.incomplete:continue
            covered.add(r['runtime_id'])
            begin=max(since,r['started_at']);end=min(now,r['ended_at'] or now)
            for key,detail in source.events.items():
                if not begin<=detail['ts']<=end:continue
                rows.append({'state_id':'native:'+sid+':'+key,'agent_id':r['agent_id'],'runtime_id':r['runtime_id'],
                             'ts':detail['ts'],'detail':json.dumps(detail)})
            for key,p in source.pending.items():
                if r['ended_at'] is not None or not begin<=p['ts']<=end:continue
                # A wrapper call may contain conditional JavaScript. Its cwd
                # is known, but do not pretend every mentioned path was touched.
                detail={'tool':'Bash','input':{'command':'','cwd':p['cwd'],'native_targets':True,'paths':[]},
                        'native':True,'action':'execute','phase':'tool_started','finished_at':None,'outcome':'unknown',
                        'location_basis':'native invocation cwd','pending_tool':p['tool']}
                rows.append({'state_id':'native:'+sid+':'+key,'agent_id':r['agent_id'],'runtime_id':r['runtime_id'],
                             'ts':p['ts'],'detail':json.dumps(detail)})
        # Bound source caches too, even when agents rotate through conversations.
        used={str(locate(r['backend_session_id'])) for r in runtimes}
        for key in list(_cache):
            if key not in used:del _cache[key]
    return rows,covered
