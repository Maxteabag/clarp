#!/usr/bin/env python3
"""Reference requests and provider responses for the engines that speak."""
import base64
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
OUT = TOOLS.parent / 'crates/clarp-kernel/tests/fixtures/speech-engines-vectors.json'


def reference(root):
    harness = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    harness['isolate'](root)
    harness['loopback_only']()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import deepgram_tts, eleven_ws, google_tts

    deepgram = []
    for voice in ('flux-example', 'aura-2-example-en', 'Flux-other', 'with space/+'):
        requests = []
        def opened(req, timeout):
            requests.append(dict(url=req.full_url, method=req.get_method(), headers=dict(req.header_items()),
                                 body=json.loads(req.data), timeout=timeout))
            return io.BytesIO(b'fixture audio')
        text = 'Say <break time="1s"/> this.'
        with patch.object(deepgram_tts.urllib.request, 'urlopen', opened):
            chunks = []
            size = deepgram_tts.synthesize(text=text,voice_id=voice,out_path=None,api_key='unit',
                                           on_chunk=lambda i,b: chunks.append([i,b.decode()]))
        deepgram.append(dict(voice=voice,text=text,request=requests[0],size=size,chunks=chunks))

    google = []
    for text in ['', 'one  two', 'A & B < C', '[happy] Hello', 'one<speed x="2">two</speed>',
                 'a<break/>b', 'a<break time="0s"/>b', '<BREAK TIME="0.001s"/>',
                 'x<break time="3000ms"/>y','<break time="3001ms"/>', '<break time="0.0025s"/>',
                 '<break time="0.0035s"/>', '<break time="1.234s"/>', '<break time="1234567s"/>',
                 '<break time="2.5ms"/>', '<break time="3.5ms"/>', '<break time="2999ms"/>','<break time="1ms"/><break time="3s"/>',
                 '<break time="0.0001s"/>','<break time="1"/>','</break time="2s">',
                 '<speed>fast</speed><volume>loud</volume>', 'x\tY\nZ<break time="1ms"/>',
                 "x<Break  TIME='1 s'/> y<emotion mode='sad'>z</emotion>",
                 '<emotion>Hi</emotion><break time="2s"/> & <untrusted>','<break time="1ms"/>\x01']:
        google.append(dict(text=text,spoken=google_tts.synthesis_input(text)))

    eleven = []
    class Socket:
        def send(self, msg):
            sent.append(json.loads(msg))
        def recv(self):
            return next(incoming)
        def close(self):
            closed.append(True)
    for events in [[{'audio':'YQ=='},{'audio':'Yg==','isFinal':True}],
                   [{'audio':'','isFinal':True}], [{'message':'busy','code':'busy'}],
                   [{'error':'rejected'}], [{'audio':'YQ=='},{'message':'closed'}],
                   [{'audio':'a','isFinal':True}], [''], ['bad-json']]:
        sent, chunks, closed, urls = [], [], [], []
        incoming = iter(e if isinstance(e,str) else json.dumps(e) for e in events)
        def connect(url,timeout):
            urls.append(url)
            return Socket()
        with patch.object(eleven_ws,'_open_ws',connect), patch.object(eleven_ws,'_emit',lambda *a,**k: None):
            try:
                size = eleven_ws.synthesize_streaming(text='A line.',voice_id='example',api_key='unit',
                    model='model',speed=1.1,out_path=None,on_chunk=lambda i,b: chunks.append([i,base64.b64encode(b).decode()]))
                error = None
            except eleven_ws.ElevenWSError as exc:
                size,error=None,str(exc)
        assert closed == [True]
        eleven.append(dict(events=events,url=urls[0],sent=sent,chunks=chunks,size=size,error=error))
    from lib import cartesia_stt_ws as ink, tts_service, tts_mode
    from lib.tts_provider import AudioSink
    from types import SimpleNamespace
    ink_queries=[]
    for words in ([], [' one ', '', 'two'], ['x']*99, ['x']*100,['x']*101,
                  ['x'*1199,'y'], ['x'*1200,'z'], ['x'*1201,'after'], ['a/b','two words',chr(0xdf)]):
        ink_queries.append(dict(words=words,url=ink.connection_url(model='ink-2',sample_rate=16000,keyterms=words,language='nb')))
    ink_streams=[]
    for events in ([],[{'type':'done'}],[{'type':'transcript','text':'partial'}],
                   [{'type':'transcript','text':' first','is_final':True},'bad json',[],{'type':'transcript','text':' second ','is_final':True},{'type':'done'}],
                   [{'type':'error','message':'message','title':'title'}],[{'type':'error','title':'title'}],[{'type':'error'}]):
        class Stream:
            def send_binary(self,data): sent.append(len(data))
            def send(self,op): sent.append(op)
            def recv(self): return next(incoming,'')
            def close(self): closed.append(True)
        for n in (0,3199,3200,3201):
            sent,closed=[],[]
            incoming=iter(e if isinstance(e,str) else json.dumps(e) for e in events)
            try:
                result,error=ink.transcribe_pcm(Stream(),pcm=bytes(n)),None
            except ink.CartesiaSTTStreamError as exc:
                result,error=None,str(exc)
            assert closed==[True]
            ink_streams.append(dict(events=events,bytes=n,sent=sent,result=result,error=error))
    ladders=[]
    for fallback in ('none','deepgram','gemini'):
        for primary in ('success','failed','partial'):
            for second in ('success','failed'):
                calls,chunks,events=[],[],[]
                class Engine:
                    available=True
                    voice='test'
                    def __init__(self,name,mode): self.id,self.mode=name,mode
                    def synthesize(self,text,sink):
                        calls.append([self.id,text])
                        if self.mode!='failed': sink.on_chunk(0,b'x')
                        if self.mode!='success': raise RuntimeError(self.id+' refused')
                        return 1
                engines={'deepgram':Engine('deepgram',primary),'gemini':Engine('gemini',second)}
                registry=SimpleNamespace(cartesia_voice=None,get=engines.__getitem__)
                try:
                    size=tts_service.synth('[happy] sound',tts_mode.Route('deepgram',None,fallback),
                        AudioSink(root/'unused',on_chunk=lambda i,b:chunks.append(b.decode())),
                        'turn',cfg=SimpleNamespace(),raw_voice=None,agent={},emit=lambda source,event,**kw:events.append(event),
                        context=lambda:None,registry=registry)
                    error=None
                except RuntimeError as exc:
                    size,error=None,str(exc)
                ladders.append(dict(primary=primary,second=second,fallback=fallback,calls=calls,chunks=chunks,size=size,error=error))
    google_http=[]
    for status,payload in [(200,{'audioContent':'YWJj'}),(200,{}),(200,[]),(200,{'audioContent':17}),
                            (200,{'audioContent':'!!!!'}),(200,{'audioContent':''}),
                            (401,{}),(403,{}),(429,{}),(500,{}),(201,{})]:
        requests, chunks=[],[]
        def transport(url,body,headers,deadline,maximum):
            requests.append(dict(url=url,body=json.loads(body),headers=headers,maximum=maximum))
            return status,json.dumps(payload).encode()
        with patch.object(google_tts,'refuse_if_collision',lambda:None):
            try:
                size=google_tts.synthesize(text='a<break time="1s"/>b',voice='nb-NO-Chirp3-HD-Kore',out_path=root/'audio',
                    creds=google_tts.Credentials('google_tts','fixture','test-project',''),
                    token=lambda *_a,**_kw:'unit',transport=transport,on_chunk=lambda i,b:chunks.append(b.decode()))
                error=None
            except google_tts.GoogleTTSError as exc:
                size,error=None,exc.code
        google_http.append(dict(status=status,payload=payload,request=requests[0],chunks=chunks,size=size,error=error))

    from lib import cartesia_stt
    from email import message_from_bytes
    ink_batch=[]
    for ctype,duration in [('audio/mp4',1),('audio/webm','1.25'),('',None),('audio/wav','bad')]:
        requests=[]
        def receive(req,timeout):
            fields=[]
            message=message_from_bytes(('Content-Type: '+req.get_header('Content-type')+'\r\n\r\n').encode()+req.data)
            for part in message.get_payload():
                fields.append(dict(name=part.get_param('name',header='content-disposition'),filename=part.get_filename(),
                                   body=part.get_payload(decode=True).decode()))
            requests.append(dict(url=req.full_url,version=req.get_header('Cartesia-version'),auth=req.get_header('Authorization'),fields=fields))
            return io.BytesIO(json.dumps({'text':'heard','duration':duration}).encode())
        with patch.object(cartesia_stt.urllib.request,'urlopen',receive):
            heard=cartesia_stt.transcribe(audio_bytes=b'clip',content_type=ctype,api_key='unit',language='nb',keyterms=['never sent'])
        ink_batch.append(dict(ctype=ctype,duration=duration,heard=heard,request=requests[0]))
    from lib import custom_tts_adapters as adapters
    import shutil
    guards=[]
    store=root/'adapters'
    for raw,executable in [
            ('{"id":"other"}',None),('invalid',None),('[]',None),
            ('{"id":" Google "}',None),('{"id":"google","executable":""}',None),
            ('{"id":"google","executable":"run"}',None),('{"id":"google","executable":"run"}','fixture program'),
            ('{"id":"google","executable":"inner/run"}','nested program')]:
        if store.exists(): shutil.rmtree(store)
        package=store/'sample'
        package.mkdir(parents=True)
        (package/'manifest.json').write_text(raw)
        if executable is not None:
            target=package/json.loads(raw)['executable']
            target.parent.mkdir(exist_ok=True)
            target.write_text(executable)
        inventory=adapters.collision_inventory(store)
        packages=[r['fingerprint'] for r in inventory['collisions']]
        for resolution in ({},{'choice':'builtin','packages':['different']},{'choice':'builtin','packages':packages}):
            with patch.object(adapters,'collision_inventory',lambda:inventory):
                code=google_tts.collision_code(SimpleNamespace(tts_google_resolution=resolution))
            guards.append(dict(raw=raw,executable=executable,resolution=resolution,code=code))
    return dict(deepgram=deepgram,google_input=google,eleven=eleven,ink_queries=ink_queries,ink_streams=ink_streams,ladders=ladders,google_http=google_http,ink_batch=ink_batch,guards=guards)


if __name__ == '__main__':
    if sys.argv[1:]==['--isolated']:
        with tempfile.TemporaryDirectory() as tmp:
            OUT.write_text(json.dumps(reference(Path(tmp)),ensure_ascii=True,indent=1)+'\n')
    else:
        subprocess.run([sys.executable,'-I','-B',__file__,'--isolated'],check=True,
                       env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
