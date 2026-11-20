#!/usr/bin/env python3
"""Record the computer's capture controls and public health on a synthetic Host."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/diagnostics.json"


def record(root):
    spec = importlib.util.spec_from_file_location("isolated", ROOT / "tests/replay/host.py")
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.isolate(root)
    isolated.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import db, diagnostics_settings as d, eventlog, health, tts_queue
    from lib.routes.settings import SettingsRoutes
    db.conn()
    updates = []
    for enabled in [True, False, 1, None, "true"]:
        for categories in [None, {}, "client", [], list(reversed(d.CATEGORIES)), [" CLIENT ", "client"], ["Z", "a", "Z"], [None, True, 2]]:
            data = dict(enabled=enabled, categories=categories)
            try:
                reply = d.update(data).public()
            except ValueError as exc:
                reply = str(exc)
            updates.append([data, reply])
    gates = []
    for cats in [[], *[[k] for k in d.CATEGORIES], list(d.CATEGORIES)]:
        for enabled in [False, True]:
            d.update(dict(enabled=enabled,categories=cats))
            samples = [(s,e,p,d.category_for(source=s,event=e,path=p),d.allows_event(source=s,event=e,path=p)) for s,e,p in [
                ('server','httpRequest','/fixture'),('client','ios.device-resources',''),('client','ios.user-slow-report',''),
                ('client','ios.conversation-open',''),('client','ios.tap',''),('server','sqliteWriteWait',''),
                ('tts','spoken',''),('transcript','row',''),('sse','connection',''),('resources','sample',''),
                ('feedback','tick',''),('interaction','tap',''),('agent','turn','')]]
            gates.append([enabled,cats,d.accepts_client_uploads(),samples])
    private = [[{k:v},eventlog._sanitize_detail({k:v})] for k in sorted(eventlog._PRIVATE_DETAIL_KEYS)
               for v in ['fixture',chr(233),None,22,True,['fixture']]]
    from lib.routes.ops import OpsRoutes
    mixed = {" Unlisted Field ": "NotAllowedError: fixture", "rows": [None, False, 1, {" PROMPT ": ["hidden", {"one":1}], "msg": "dur=18ms size=42B"}]}
    for value in [mixed, {"nested": [mixed, {" ToolOutput ": "literal string"}]}, {" Body ": {"z":"first","a":"second"}}, {"empty":[]}, {"x":None}]:
        private.append([value,eventlog._sanitize_detail(value)])
    uploads=[]
    d.update({"enabled": True,"categories":["client"]})
    for name in [None, "", "micFail", chr(233)*63, chr(233)*64, chr(233)*65]:
        for detail in [None, "", "getUserMedia rejected: NotAllowedError", 17, False, ["ignored"], {}, mixed]:
            item={"event":name,"detail":detail}
            received=[]
            h=SimpleNamespace(_read_json=lambda:item,_json_ok=lambda v:None)
            def emitted(source,event,**fields):
                obj={"kind":"client_upload","name":event}
                if fields["detail"] is not None:
                    obj["detail"]=eventlog._sanitize_detail(fields["detail"])
                received.append(obj)
            with patch.dict("os.environ",{"CLAUDE_PWA_CLIENT_LOG":str(root / "fixture.log")}),patch.object(eventlog,"emit",emitted):
                OpsRoutes._handle_clog(h)
            answer,=received
            uploads.append([item,answer])
    checks=[]
    for stt in [True,False]:
        for delivery in ['hls','stream']:
            for ff in [True,False]:
                health.reset_for_tests()
                health.mark_error('stt','fixture_error',now=3.5)
                health.mark_success('stt',now=5)
                results=[]
                h=SimpleNamespace(ctx=SimpleNamespace(stt=SimpleNamespace(ready=SimpleNamespace(is_set=lambda:stt)),deployed_version=lambda:'test-version'),
                    server=SimpleNamespace(boot_config=SimpleNamespace(delivery=delivery)),_send=lambda code,body,kind:results.append(json.loads(body)))
                with patch('shutil.which',return_value='fixture' if ff else None),patch.object(tts_queue,'pending_count',return_value=7),patch.object(tts_queue,'in_flight_count',return_value=2):
                    SettingsRoutes._handle_diagnostics_health(h)
                checks.append([stt,delivery,ff,results[0]])
    return dict(updates=updates,gates=gates,uploads=uploads,private=private,health=checks,
                decode=[[raw,d._decode(raw).public()] for raw in ['', 'not json','[]','{"enabled":1}', '{"enabled":true,"categories":"client"}','{"enabled":true,"categories":["unknown"," VOICE "]}']])


if __name__ == '__main__':
    if '--record' in sys.argv:
        with tool_scratch.directory() as scratch:
            print(json.dumps(record(Path(scratch))))
    else:
        data=json.loads(subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'UTC'})))
        OUT.write_text(json.dumps(data,ensure_ascii=True,sort_keys=False,separators=(',',':'))+'\n',encoding='ascii')
        print({k:len(v) for k,v in data.items()})
