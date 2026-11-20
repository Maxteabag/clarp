#!/usr/bin/env python3
"""Run the reference handlers and timeline queries against an isolated synthetic database."""
import io
import json
import wave
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/voice-timeline.json"
NOW = 30000000


@scratch.case("voice-timeline-")
def record(home):
    replay = runpy.run_path(str(ROOT/"tests/replay/host.py"))
    replay["isolate"](home)
    replay["loopback_only"]()
    sys.path.insert(0,str(ROOT/"server"))
    from lib import audio_metrics, db, voice_events
    from lib.routes.voice_timeline import VoiceTimelineRoutes

    class Door(VoiceTimelineRoutes):
        def _read_json(self):
            return self.body if isinstance(self.body,dict) else None

        def _json_ok(self, value):
            self.response=[200,value]

        def _json_error(self, status, why):
            self.response=[status,{"error":why}]

        def _query(self):
            return {key:[v] for key,v in self.parameters}

    door=Door()
    batches=[]
    def post(body):
        db.conn().execute("DELETE FROM voice_events")
        db.conn().execute("DELETE FROM sqlite_sequence WHERE name='voice_events'")
        door.body=body
        with patch.object(voice_events,"now_ms",side_effect=[NOW,NOW+1]):
            door._handle_voice_events_post()
        batches.append(dict(body=body,reply=door.response,rows=voice_events.query()))
    for body in [None,[],{},{"events":None},{"events":"none"},{"events":[]},{"events":[None]*501},
                 {"events":[None]*499+[{"event":"last"}]}]:
        post(body)
    for sent in [None,0,NOW-21600001,NOW-21600000,NOW-21599999,NOW,NOW+21599999,NOW+21600000,NOW+21600001,
                 True,False,"nan","inf","  15.9 ",[1],{}]:
        post({"source":" PWA ","sent_at":sent,"events":[{"event":" level ","ts":"4.7","duration_ms":"2.5"}]})
    for source in ["ios","android","server","other","invalid",False,None]:
        post({"source":source,"client_id":" a ","session":" base ","events":[None,{},{"event":" "},{"event":True},
              {"event":"x"*65,"session":"x"*129,"client_ts":22,"level_db":"-19.5","peak_db":False,"mono_ms":-1.2,
               "utterance_id":7,"trace_id":[1],"text":"","detail":["ignored"]}]})
    for number in ["1_234.5", "1__0", "3_", "_8", "1_.5", "1e+2", chr(0x0661)+"."+chr(0x0665)]:
        post({"events":[{"event":"numeric","duration_ms":number,"ts":number,"mono_ms":number}]})
    for n in [3999,4000,4001]:
        post({"events":[{"event":"fixture","text":"x"*n}]})
    for n in [7991,7992,7993]:
        post({"events":[{"event":"fixture","detail":{"x":"k"*n}}]})
    events=[]
    def ev(name,t,uid=None,trace=None,**kw):
        events.append(dict(event=name,ts=t,utterance_id=uid,trace_id=trace,**kw))
    ev("play_end",40,trace="trace")
    ev("send",8,trace="trace",session="rollup-only")
    ev("play_start",24,trace="trace");ev("play_start",25,trace="trace")
    ev("speech_start",4,"a",detail={"silence_ms":42})
    ev("speech_start",5,"a")
    ev("speech_end",9,"a",level_db=-6)
    ev("speech_end",10,"a",duration_ms=7)
    ev("level",10,"a",level_db=-23,peak_db=-5)
    ev("transcript",12,"a","trace",text="",duration_ms=2.5)
    ev("transcript",13,"a",text="synthetic")
    ev("upload_start",7,"a");ev("upload_end",12,"a")
    ev("corrupt",14,"a",detail={"reasons":["fixture",2]})
    ev("corrupt",15,"a",detail={"reasons":[],"reason":"reason"})
    ev("barge_in",32,"a");ev("play_end",39,"a")
    ev("silence",2,"b",duration_ms=0);ev("speech_start",3,"b",detail={"silence_ms":5})
    ev("speech_end",6,"b",level_db=0,peak_db=0)
    ev("upload_start",1,"b");ev("upload_end",5,"b",duration_ms=0)
    ev("transcript",16,"b","trace")
    ev("speech_start",3,"c");ev("speech_end",5,"c")
    ev("transcript",2,"z","independent",session="other")
    ev("unconnected",50,trace="orphan")
    post({"events":events,"sent_at":NOW})
    reads=[]
    for args in [{},{"since":"3","until":"10"},{"since":" -1.5 ","until":"bad"},{"since":"56"},{"until":"0"},
                 {"event":"speech_start","limit":"2"},{"trace_id":"trace","utterance_id":"a"},
                 {"session":" rollup-only "},{"session":"other"},{"session":"absent"},{"limit":"-2"},{"limit":"0"},
                 {"limit":"1.8"},{"limit":"50000"},{"event":"absent"},{"until":"1","since":"9"}]:
        door.parameters=list(args.items())
        door._handle_voice_events_get(); rows=door.response[1]
        door._handle_voice_utterances_get()
        reads.append(dict(params=door.parameters,events=rows,utterances=door.response[1]))
    # Record just the extrema for windows spanning thousands of rows; the test
    # builds the same rows by ID, so the fixture needn't contain thousands of copies.
    conn=db.conn()
    conn.execute("DELETE FROM voice_events")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='voice_events'")
    conn.executemany("INSERT INTO voice_events(ts,received_at,source,event,utterance_id) VALUES(?,?,'ios','fixture',?)",
                     ((n,n,str(n)) for n in range(1,5004)))
    windows=[]
    for bound in [-1,0,1,1999,2000,2001,4999,5000,5001,20000]:
        door.parameters=[("limit",str(bound))]
        door._handle_voice_events_get()
        rr=door.response[1]["events"]
        door._handle_voice_utterances_get()
        uu=door.response[1]["utterances"]
        windows.append(dict(limit=bound,rows=[len(rr),rr[0]["event_id"],rr[-1]["event_id"]],
                            groups=[len(uu),uu[0]["utterance_id"],uu[-1]["utterance_id"]]))
    metrics=[]
    for samples, width, channels,rate in [
        ([128]*120,1,1,1000), ([128]*160,1,1,1000), ([128]*160+[255]*40,1,1,1000),
        ([0,32767,-32768,0]*80,2,2,1000),([1,-1]*1600,4,1,16000),([],2,1,16000),
    ]:
        data=io.BytesIO()
        with wave.open(data,"wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(b"".join(n.to_bytes(width,"little",signed=width!=1) for n in samples))
        measured=audio_metrics.analyze(data.getvalue(),"audio/wav")
        metrics.append(dict(samples=samples,width=width,channels=channels,rate=rate,measured=measured,
                            empty=audio_metrics.corruption_reasons(measured,transcript=""),
                            present=audio_metrics.corruption_reasons(measured,transcript="fixture")))
    flags=[]
    for key,values in {"duration_ms":[149.99,150,150.01],"peak_db":[-40.01,-40,-39.99],
                       "clip_ratio":[0.0099,0.01,0.0101],"silence_ratio":[0.969,0.97,0.971],
                       "dc_offset":[-0.101,-0.1,0.1,0.101],"decode_error":["fixture"],
                       "unavailable":["numpy"]}.items():
        for value in values:
            m={"duration_ms":200,"peak_db":-5,key:value}
            flags.append(dict(metric=m,empty=audio_metrics.corruption_reasons(m,transcript=""),
                              present=audio_metrics.corruption_reasons(m,transcript="fixture")))
    return dict(now=NOW,batches=batches,reads=reads,windows=windows,metrics=metrics,flags=flags)


if __name__ == "__main__":
    if sys.argv[1:]==["--isolated"]:
        print(json.dumps(record(),ensure_ascii=True,separators=(",",":")))
    else:
        OUT.write_bytes(subprocess.check_output([sys.executable,"-IB",__file__,"--isolated"],
            env=scratch.child_env({"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"})))
