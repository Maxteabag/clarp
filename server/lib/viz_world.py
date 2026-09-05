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
        gitdir=marker
        try:
            if marker.is_file(): gitdir=(root/marker.read_text().strip().removeprefix('gitdir: ')).resolve()
            common=gitdir/'commondir'
            if common.exists(): gitdir=(gitdir/common.read_text().strip()).resolve()
            cfg=configparser.ConfigParser(interpolation=None);cfg.read(gitdir/'config')
            remote=cfg.get('remote "origin"','url',fallback='')
            name=gitdir.parent.name if gitdir.name=='.git' else root.name
        except (OSError,ValueError,configparser.Error): name=root.name;remote=''
        return {'id':'checkout:'+str(root),'name':name,'path':str(root),'remote':remote}
    return None


def evidence(tool, inp, path, target, verb):
    inp=inp if isinstance(inp,dict) else {}
    raw=str(inp.get('command') or inp.get('cmd') or tool)
    cwd=inp.get('workdir') or inp.get('cwd') or ''
    paths=[]
    clipped=len(tool)>=80 and tool.startswith(('/usr/bin/','/bin/')) and not inp.get('command')
    if path: paths.append(path)
    if inp.get('path'): paths.append(inp['path'])
    try: words=shlex.split(raw)
    except ValueError: words=raw.replace("'",' ').replace('"',' ').split()
    for i,w in enumerate(words):
        if clipped and i==len(words)-1:continue
        if w in {'cd','-C','--cwd','--prefix'} and i+1<len(words) and words[i+1].startswith('/'):
            cwd=words[i+1]
        if w.startswith('/') and not w.startswith(('/usr/bin/','/bin/')) and not w.endswith(('bash','sh')):
            if '/GIT/' in w or os.path.exists(w): paths.append(w.rstrip(';'))
    if not paths and cwd and os.path.isabs(cwd): paths=[cwd]
    anchored=[]
    for p in paths:
        if not isinstance(p,str): continue
        if not os.path.isabs(p):
            if not cwd or not os.path.isabs(cwd): continue
            p=os.path.normpath(os.path.join(cwd,p))
        if p not in anchored: anchored.append(p)
    location=None;repo=None
    for p in anchored:
        r=checkout(p)
        if r: location=p;repo=r;break
    if location is None and anchored: location=anchored[0]
    action=verb
    from .viz_normalize import first_known_executable
    executable,sub=first_known_executable(raw)
    bases={executable}
    if bases & {'rm','unlink','rmdir'}: action='delete'
    elif tool in {'Write','write'} or bases & {'mkdir','touch'}: action='create'
    elif tool in {'Edit','edit','file_change'}: action='edit'
    elif 'git' in bases and sub=='commit': action='commit'
    elif 'git' in bases and sub=='push': action='push'
    return {'raw':raw[:2000],'path':location,'cwd':cwd or None,'checkout':repo,'action':action,
            'recorded_target':target,'tool':tool}


def build(events):
    host=socket.gethostname()
    entities={}
    relations=[]
    def add(id,**fields):
        if id not in entities: entities[id]={'id':id,**fields,'events':0}
        return entities[id]
    hostid='host:'+host
    add(hostid,label=host,kind='host',parent=None,purpose='The Computer running these agents')
    facts=[]
    for ev in events:
        fact=ev.get('evidence',{});repo=fact.get('checkout');parent=hostid;path=fact.get('path')
        if repo:
            parent=repo['id'];add(parent,label=repo['name'],kind='repository',parent=hostid,path=repo['path'],purpose='Local working checkout')
            remote=repo['remote'];m=re.search(r'github\.com[:/]([^/]+)/([^\s]+)',remote)
            if m:
                owner,name=m.groups();name=name.removesuffix('.git')
                add('github',label='GitHub',kind='platform',parent=None,purpose='Remote code, reviews and automation')
                add('github:'+owner,label=owner,kind='organization',parent='github')
                rid='github:'+owner+'/'+name
                add(rid,label=name,kind='remote-repository',parent='github:'+owner,url='https://github.com/'+owner+'/'+name)
                rel={'from':parent,'to':rid,'kind':'remote'}
                if rel not in relations: relations.append(rel)
        if path and repo and path!=repo['path']:
            rel=os.path.relpath(path,repo['path']);parts=rel.split(os.sep)
            for i,part in enumerate(parts[:-1]):
                did='directory:'+repo['path']+'/'+ '/'.join(parts[:i+1])
                add(did,label=part,kind='directory',parent=parent,path=repo['path']+'/'+ '/'.join(parts[:i+1]));parent=did
            target='file:'+path
            ext=pathlib.Path(path).suffix
            purpose={'py':'Python source','js':'JavaScript source','svelte':'Svelte interface','swift':'Swift source',
                     'md':'Documentation','toml':'Configuration','json':'Structured data','sqlite':'SQLite database',
                     'sh':'Shell automation','png':'Image','svg':'Vector artwork'}.get(ext.lstrip('.'),'Recorded path')
            if any(p in {'test','tests'} for p in parts): purpose='Regression tests'
            purpose=source_purpose(path) or purpose
            add(target,label=parts[-1],kind='directory' if os.path.isdir(path) else 'file',parent=parent,path=path,purpose=purpose,extension=ext)
        elif repo: target=parent
        elif ev['target'].startswith('service:github'):
            add('github',label='GitHub',kind='platform',parent=None,purpose='Remote code, reviews and automation')
            target='github:unlocated';add(target,label='Repository not recorded',kind='unresolved',parent='github',purpose='The tool event identifies GitHub but omits the repository')
        elif path:
            target='path:'+path;add(target,label=os.path.basename(path),kind='file',parent=hostid,path=path,purpose='Explicit recorded path')
        else:
            target='activity:'+ev['agent_id']+':'+ev['target']
            label=fact.get('tool') or ev['verb']
            if label.startswith(('/usr/','/bin/')): label=ev['verb']
            add(target,label=label,kind='unresolved',parent=hostid,purpose='The recording omits the concrete target',sample=fact.get('raw',''))
        entities[target]['events']+=1
        facts.append({**ev,'world_target':target,'action':fact.get('action',ev['verb'])})
    return {'entities':list(entities.values()),'relations':relations,'events':facts,
            'coverage_keys':sorted({'entity:'+e['id'] for e in entities.values()} | {e['kind']+':'+str(e.get('extension','')) for e in entities.values()} | {'action:'+e['action'] for e in facts}),
            'host':host,'evidence_note':'Hierarchy uses explicit recorded paths and local Git metadata. Missing historical targets remain unlocated.'}


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
