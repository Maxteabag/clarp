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
    return dict(deepgram=deepgram,google_input=google,eleven=eleven)


if __name__ == '__main__':
    if sys.argv[1:]==['--isolated']:
        with tempfile.TemporaryDirectory() as tmp:
            OUT.write_text(json.dumps(reference(Path(tmp)),ensure_ascii=True,indent=1)+'\n')
    else:
        subprocess.run([sys.executable,'-I','-B',__file__,'--isolated'],check=True,
                       env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
