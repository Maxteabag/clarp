"""Evidence for a self-authored world. Facts are not a prescribed visual grammar."""
from __future__ import annotations
import configparser
import functools
import os
import pathlib
import re
import shlex
import socket


@functools.lru_cache(maxsize=2048)
def checkout(path):
    p=pathlib.Path(path)
    if not p.is_absolute(): return None
    for root in [p,*p.parents]:
        marker=root/'.git'
        if not marker.exists(): continue
        gitdir=marker;family=None;main_path=None
        try:
            if marker.is_file(): gitdir=(root/marker.read_text().strip().removeprefix('gitdir: ')).resolve()
            common=gitdir/'commondir'
            if common.exists(): gitdir=(gitdir/common.read_text().strip()).resolve()
            family='git:'+str(gitdir.resolve());main_path=str(gitdir.parent) if gitdir.name=='.git' else None
            cfg=configparser.ConfigParser(interpolation=None);cfg.read(gitdir/'config')
            remote=cfg.get('remote "origin"','url',fallback='')
            name=gitdir.parent.name if gitdir.name=='.git' else root.name
        except (OSError,ValueError,configparser.Error): name=root.name;remote=''
        return {'id':'checkout:'+str(root),'name':name,'path':str(root),'remote':remote,'project_id':family,'main_path':main_path,'is_worktree':marker.is_file()}
    return None


def service_operations(raw):
    """Only direct shell commands, never strings inside an embedded program."""
    if '<<' in raw:return []
    result=[]
    for line in raw.splitlines():
        try:words=shlex.split(line,comments=True)
        except ValueError:continue
        if words[:1]==['sudo']:
            words=words[1:]
            while words and words[0] in {'-n','--non-interactive'}:words=words[1:]
        if not words or os.path.basename(words[0])!='systemctl':continue
        args=words[1:];scope='user' if '--user' in args else 'system'
        while args and args[0] in {'--user','--system','--no-pager','--quiet','-q'}:args=args[1:]
        if not args or args[0] not in {'restart','start','stop','reload','status','is-active'}:continue
        operation=args[0]
        for unit in args[1:]:
            if unit in {'&&',';','|','>','2>'}:break
            if not re.fullmatch(r'[A-Za-z0-9_@.:-]+',unit) or unit.startswith('-'):continue
            result.append({'unit':unit if '.' in unit else unit+'.service','scope':scope,'operation':operation})
    return result[:12]


def evidence(tool, inp, path, target, verb):
    inp=inp if isinstance(inp,dict) else {}
    raw=str(inp.get('command') or inp.get('cmd') or tool)
    cwd=str(inp.get('workdir') or inp.get('cwd') or '')
    paths=list(inp.get('paths') or [])
    if path:paths.insert(0,path)
    if inp.get('path'):paths.append(inp['path'])
    try:words=shlex.split(raw,comments=True)
    except ValueError:words=[]
    # Follow only explicit shell location switches. Never read a path out of
    # Python/JavaScript source, quoted output, environment values or redirects.
    if words and words[0]=='cd' and len(words)>2 and words[2] in {'&&',';'}:
        cwd=os.path.normpath(os.path.join(cwd,words[1])) if cwd else words[1]
        words=words[3:]
    exe=os.path.basename(words[0]) if words else ''
    args=words[1:]
    if exe=='git' and '-C' in args:
        pos=args.index('-C')
        if pos+1<len(args):
            cwd=os.path.normpath(os.path.join(cwd,args[pos+1]));args=args[:pos]+args[pos+2:]
    sub=next((w for w in args if not w.startswith('-')), '')
    action=verb
    if exe in {'rm','unlink','rmdir'}:action='delete'
    elif tool in {'Write','write'} or exe in {'mkdir','touch'}:action='create'
    elif tool in {'Edit','edit','file_change'}:action='edit'
    elif exe=='git' and sub=='commit':action='commit'
    elif exe=='git' and sub=='push':action='push'
    elif exe in {'pytest','ctest','vitest'} or (exe in {'npm','pnpm','dotnet','cargo'} and sub=='test'):action='test'
    # Simple actual shell operands supplement native parsed targets. Complex
    # scripts retain their execution cwd as a workspace, not a guessed file.
    simple=not any(x in raw for x in ['\n','<<','|',';','&&'])
    if not simple and tool=='Bash':action='execute'
    if not paths and simple and exe in {'cat','head','tail','bat','rm','unlink','touch','mkdir','ls'}:
        skip=False
        for arg in args:
            if skip:skip=False;continue
            if arg in {'-n','-c','--lines','--bytes'} and exe in {'head','tail'}:skip=True;continue
            if arg.startswith('-'):continue
            if arg in {'>','>>','2>','/dev/null'}:break
            paths.append(arg)
    anchored=[]
    for value in paths:
        if not isinstance(value,str) or not value or any(c in value for c in '*?$'):continue
        if not os.path.isabs(value):
            if not os.path.isabs(cwd):continue
            value=os.path.normpath(os.path.join(cwd,value))
        if value not in anchored:anchored.append(value)
    location=anchored[0] if anchored else (cwd if os.path.isabs(cwd) else None)
    repo=checkout(location) if location else None
    return {'raw':raw[:2000],'path':location,'paths':anchored[:40],'cwd':cwd or None,'checkout':repo,'action':action,
            'services':service_operations(raw),'recorded_target':target,'tool':tool,'scope':'target' if anchored else 'workspace' if location else 'unknown'}


def build(events):
    host=socket.gethostname();hostid='host:'+host
    entities={};relations=[];facts=[]
    def add(id,**fields):
        if id not in entities:entities[id]={'id':id,**fields,'events':0}
        return entities[id]
    add(hostid,label=host,kind='host',parent=None,purpose='The Computer running these agents')
    def repo_entity(repo):
        rid=repo['id'];add(rid,label=repo['name'],kind='repository',parent=hostid,path=repo['path'],purpose='Local working checkout',project_id=repo.get('project_id'),main_path=repo.get('main_path'),is_worktree=repo.get('is_worktree',False))
        m=re.search(r'github\.com[:/]([^/]+)/([^\s]+)',repo['remote'])
        remote=None
        if m:
            owner,name=m.groups();name=name.removesuffix('.git')
            add('github',label='GitHub',kind='platform',parent=None,purpose='Remote repositories')
            add('github:'+owner,label=owner,kind='organization',parent='github')
            remote='github:'+owner+'/'+name
            add(remote,label=name,kind='remote-repository',parent='github:'+owner,url='https://github.com/'+owner+'/'+name)
            rel={'from':rid,'to':remote,'kind':'remote','label':'origin'}
            if rel not in relations:relations.append(rel)
        return rid,remote
    def path_entity(path,repo,is_directory=False):
        parent=hostid
        if repo:
            parent,_=repo_entity(repo)
            if path==repo['path']:return parent
            parts=os.path.relpath(path,repo['path']).split(os.sep);base=repo['path']
        else:
            parts=pathlib.Path(path).parts[1:];base=''
        for i,part in enumerate(parts):
            full=base+'/'+ '/'.join(parts[:i+1]);directory=i<len(parts)-1 or is_directory
            eid=('directory:' if directory else 'file:')+full
            purpose='Directory' if directory else source_purpose(full) or {
                '.py':'Python source','.js':'JavaScript source','.md':'Documentation','.json':'Structured data',
                '.swift':'Swift source','.svelte':'Svelte interface','.sqlite':'SQLite database','.sh':'Shell automation',
                '.toml':'Configuration','.png':'Image','.svg':'Vector artwork'}.get(pathlib.Path(full).suffix,'File')
            if not directory and any(p in {'test','tests'} for p in parts):purpose='Regression tests'
            add(eid,label=part,kind='directory' if directory else 'file',parent=parent,path=full,purpose=purpose,extension=pathlib.Path(full).suffix)
            parent=eid
        return parent
    for ev in events:
        fact=ev.get('evidence',{});repo=fact.get('checkout');path=fact.get('path');action=fact.get('action',ev['verb'])
        targets=[];remote=None;workspace=None
        if repo:
            workspace,remote=repo_entity(repo)
            if action=='push':
                try:push_words=shlex.split(fact.get('raw',''))
                except ValueError:push_words=[]
                if 'push' in push_words:
                    remaining=[w for w in push_words[push_words.index('push')+1:] if not w.startswith('-')]
                    if remaining and remaining[0]!='origin':remote=None
        paths=fact.get('paths') or []
        services=fact.get('services') or []
        if services:
            for service in services:
                sid='service:'+host+':'+service['scope']+':'+service['unit']
                add(sid,label=service['unit'].removesuffix('.service'),kind='service',parent=hostid,
                    unit=service['unit'],scope=service['scope'],purpose='Observed systemd service')
                targets.append(sid)
            target=targets[-1];action=services[-1]['operation']
        elif action in {'commit','push','vcs','build','test'} and repo:
            target=workspace
        elif path:
            for value in paths:
                targets.append(path_entity(value,checkout(value),os.path.isdir(value)))
            if len(paths)>1:
                common=os.path.commonpath(paths)
                target=path_entity(common,checkout(common),True)
            else:target=path_entity(path,repo,fact.get('scope')=='workspace' or os.path.isdir(path))
        elif ev['target'].startswith('service:github'):
            add('github',label='GitHub',kind='platform',parent=None,purpose='Remote repositories')
            target='github:unknown';add(target,label='Repository unknown',kind='unresolved',parent='github',purpose='No repository recorded')
        else:
            target='unknown:'+ev['agent_id'];add(target,label=ev['agent'],kind='unresolved',parent=None,purpose='Current location unknown')
        entities[target]['events']+=1
        operation_exact=len(fact.get('raw','').splitlines())==1 and not any(token in fact.get('raw','') for token in ['&&',';','|'])
        facts.append({**ev,**({'outcome':'unknown'} if services and not operation_exact else {}),'world_target':target,'world_targets':targets or [target],'workspace_target':workspace,
                      'remote_target':remote if action=='push' else None,'remote_basis':'configured origin' if remote and action=='push' else None,
                      'action':action,'operations':services,'location_scope':'target' if services else fact.get('scope','unknown')})
    return {'entities':list(entities.values()),'relations':relations,'events':facts,
            'coverage_keys':sorted({'entity:'+e['id'] for e in entities.values()}|{e['kind']+':'+str(e.get('extension','')) for e in entities.values()}|{'action:'+e['action'] for e in facts}),
            'host':host,'evidence_note':'Exact native runtime records supply cwd, tool targets and lifecycle. Workspace evidence never implies a specific file.'}


@functools.lru_cache(maxsize=1024)
def source_purpose(path):
    p=pathlib.Path(path)
    if p.suffix not in {'.py','.js','.svelte','.md','.swift'}:return None
    try:
        with p.open() as f:head=f.read(2048)
    except (OSError,UnicodeError):return None
    if p.suffix=='.py':m=re.match(r'\s*"""([^\n"]+)',head)
    elif p.suffix=='.md':m=re.search(r'^# ([^\n]+)',head,re.M)
    else:m=re.search(r'^\s*// ([^\n]+)',head,re.M)
    return m.group(1)[:120] if m else None
