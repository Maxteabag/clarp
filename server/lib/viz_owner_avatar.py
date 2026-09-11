"""Cache public GitHub owner portraits outside rendering, using a fixed origin."""
from __future__ import annotations
import json
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from . import xdg

_lock=threading.Lock()
_retry={}


def portrait(login: str, opener=urllib.request.urlopen) -> tuple[bytes,str] | None:
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})',login):return None
    name=login.lower();root=xdg.cache_dir()/'viz-owner-avatars';path=root/(name+'.image');meta=root/(name+'.json')
    with _lock:
        try:
            info=json.loads(meta.read_text())
            if time.time()-info['at']<86400 and path.is_file():return path.read_bytes(),info['type']
        except (OSError,ValueError,KeyError):pass
        if _retry.get(name,0)>time.time():return None
        _retry[name]=time.time()+300
        try:
            request=urllib.request.Request('https://api.github.com/users/'+name,headers={'User-Agent':'Clarp-fleet-map','Accept':'application/vnd.github+json'})
            with opener(request,timeout=8) as response:profile=json.loads(response.read(65536))
            if str(profile.get('login','')).lower()!=name:return None
            url=str(profile.get('avatar_url',''));parsed=urlsplit(url)
            if parsed.scheme!='https' or parsed.hostname!='avatars.githubusercontent.com' or parsed.username:return None
            with opener(urllib.request.Request(url,headers={'User-Agent':'Clarp-fleet-map'}),timeout=8) as response:
                mime=response.headers.get_content_type();image=response.read(2_000_001)
            if mime not in {'image/png','image/jpeg','image/webp'} or len(image)>2_000_000:return None
            root.mkdir(parents=True,exist_ok=True);path.write_bytes(image);meta.write_text(json.dumps({'at':time.time(),'type':mime,'login':profile['login']}))
            return image,mime
        except (OSError,ValueError,KeyError):return None
