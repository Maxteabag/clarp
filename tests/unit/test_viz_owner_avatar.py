import json
from email.message import Message
from lib import viz_owner_avatar as owner

class Response:
    def __init__(self,data,mime='application/json'):
        self.data=data;self.headers=Message();self.headers['Content-Type']=mime
    def read(self,n):return self.data[:n]
    def __enter__(self):return self
    def __exit__(self,*args):pass

def test_owner_avatar_uses_verified_account_and_fixed_avatar_host(tmp_path,monkeypatch):
    monkeypatch.setattr(owner.xdg,'cache_dir',lambda:tmp_path);monkeypatch.setattr(owner,'_retry',{})
    urls=[]
    def open(request,timeout):
        urls.append(request.full_url)
        return Response(json.dumps({'login':'Maxteabag','avatar_url':'https://avatars.githubusercontent.com/u/123?v=4'}).encode()) if len(urls)==1 else Response(b'image','image/jpeg')
    assert owner.portrait('Maxteabag',open)==(b'image','image/jpeg')
    assert owner.portrait('Maxteabag',open)==(b'image','image/jpeg')
    assert len(urls)==2 and urls[0]=='https://api.github.com/users/maxteabag'

def test_invalid_identity_or_avatar_origin_is_not_fetched(tmp_path,monkeypatch):
    monkeypatch.setattr(owner.xdg,'cache_dir',lambda:tmp_path);monkeypatch.setattr(owner,'_retry',{})
    urls=[]
    def open(request,timeout):
        urls.append(request.full_url);return Response(json.dumps({'login':'test','avatar_url':'http://127.0.0.1/private'}).encode())
    assert owner.portrait('../secret',open) is None
    assert owner.portrait('test',open) is None
    assert len(urls)==1
