#!/usr/bin/env python3
"""Record real Claude parent transcript projections using only synthetic files."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-backend/tests/fixtures/helper-cells.json"

def record(directory):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.isolate(directory)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib.claude_transcript import parse_turns
    from lib.transcript_incremental import _claude_children
    path = directory / "project" / "parent.jsonl"
    path.parent.mkdir()
    children = path.parent / path.stem / "subagents"
    children.mkdir(parents=True)
    stories = []
    def call(name="Agent", tool_id="alpha", **args):
        return {"type":"assistant","message":{"content":[{"type":"tool_use","name":name,"id":tool_id,"input":args}]}}
    def receipt(content, meta=None, **fields):
        return {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"alpha","content":content,**fields}]},"toolUseResult":meta}
    def case(title, records, files=None):
        for entry in children.iterdir():
            entry.unlink()
        for name, text in (files or {}).items():
            (children / name).write_text(text)
        path.write_text("".join(json.dumps(r)+"\n" for r in records))
        expected=parse_turns(path)
        keys=sorted({str(v) for t in expected for cell in t.get("display_cells",[]) for v in [cell["id"],cell["agent_id"]] if v})
        projected=_claude_children(str(path), set(keys))
        item={"title":title,"records":records,"files":files or {},"expected":expected,"children":projected}
        stories.append(json.loads(json.dumps(item).replace(str(path.parent),"/fixture")))
    for name in ["Task","Agent","agent","task","Bash"]:
        case(name,[call(name,description="  First\n step  ",subagent_type=" Explore ",prompt="Look\there",run_in_background=True)])
    for n in [0,1,119,120,121,239,240,241,299,300,301]:
        val="λ"*n
        case(f"width {n}",[call(description=val,prompt=val),receipt(val)])
    for value in [False,None,1,"true",True]:
        case(f"background {value!r}",[call(run_in_background=value)])
    for status in ["async_launched","failed","killed","stopped","error","completed","",None]:
        for async_ in [None,True,1]:
            case(f"receipt {status} {async_!r}",[call(),receipt([{"text":" alpha "},{"content":"β"}],{"status":status,"isAsync":async_,"agentId":"kid"})])
    for error in ["is_error","isError"]:
        case(error,[call(),receipt("bad",{"isAsync":True,"agentId":"child"},**{error:True})])
    for count in [0,1,2,-1,True,1.5,"1",None]:
        for ms in [999,1000,59999,60000,61000,-1,True,1.5,"1000",None]:
            case(f"usage {count!r}, {ms!r}",[call(),receipt("done",{"totalToolUseCount":count,"totalDurationMs":ms})])
    for status in ["COMPLETED","failed","killed","stopped","running",""]:
        for channel in ["queue-operation","user"]:
            for closed in [True,False]:
                note=f"<task-notification><status>{status}</status><tool-use-id> alpha </tool-use-id><task-id>child</task-id><summary> fallback </summary>"+("</task-notification>" if closed else "")
                payload={"type":channel,"content":note} if channel=="queue-operation" else {"type":"user","message":{"content":note}}
                case(f"note {status} {channel} {closed}",[call(),receipt("launch",{"status":"async_launched"}),payload])
    for files in [{},{"agent-kid.jsonl":"private fixture text"},{"agent-kid.meta.json":'{"toolUseId":"alpha"}'},
                  {"agent-kid.jsonl":"","agent-kid.meta.json":"broken"},
                  {"agent-kid.jsonl":"","agent-kid.meta.json":'{"toolUseId":"alpha"}'},
                  {"agent-kid.jsonl":"","agent-alias.jsonl":"","agent-alias.meta.json":'{"toolUseId":"alpha"}'}]:
        case("discovery",[call(),receipt("",{"agentId":"kid"})],files)
    case("same id replaces its earlier association",[call(),call("Task"),receipt("last")])
    case("interleaved siblings",[call(),call(tool_id="beta"),receipt("one"),call(tool_id="gamma")])
    for value in (None,False,True,17,1.5,[],["hello"],{},{"v":"q"},"  Ada\u2003Lee  "):
        case("typed inputs",[call(prompt=value,description=value,subagent_type=value)])
    for field in ("isAsync","agentId"):
        for value in (None,0,False,True,"","known",[],{}):
            case("typed receipt",[call(),receipt("...",{field:value})])
    for content in [None,12,{},[],["plain",False,{}], [{"text":0,"content":"fallback"},{"text":17}], " ".join("a" for _ in range(300))]:
        case("result content",[call(),receipt(content)])
    for value in ("",None,0,False,17,True,1.5):
        case("missing call id",[call(tool_id=value),receipt("unmatched")])
    def note(id,status,body=""):
        return f"<task-notification><tool-use-id>{id}</tool-use-id><task-id>notice-id</task-id><status>{status}</status>{body}</task-notification>"
    for status in ("stopped","killed","failed","completed"):
        for next_status in ("stopped","failed","completed"):
            first=note("alpha",status,"<result> first </result><summary>ignored</summary>")
            last=note("alpha",next_status,"<result> "+("x"*241)+" </result>")
            case("repeated notifications",[call(),receipt("queued",{"isAsync":True,"agentId":"original"}),{"type":"queue-operation","content":first+last}])
    case("wrong tool id",[call(),{"type":"queue-operation","content":note("foreign","completed")}])
    case("notes in content blocks",[call(),{"type":"user","message":{"content":[{"type":"text","text":note("alpha","failed","<result>note</result>")}]}}])
    case("unbounded sibling count", [{"type":"assistant","message":{"content":[call(tool_id=f"call-{n}")["message"]["content"][0] for n in range(70)]}}])
    case("helper beside normal tool and text", [{"type":"assistant","message":{"content":[{"type":"text","text":"working"},call()["message"]["content"][0],call("Bash","b")["message"]["content"][0]]}},receipt("ok")])
    case("type is the fallback description",[call(subagent_type=" Reviewer ")])
    case("normal card remains normal",[call("Bash","regular",description="check")])
    case("error has priority over async",[call(),receipt("error",{"isAsync":True,"agentId":"a"},isError=1)])
    case("upper input and unrecognized completion",[call(),receipt("launch",{"status":"ASYNC_LAUNCHED"})])
    for result_len in (239,240,241):
        case("notification result width",[call(),{"type":"queue-operation","content":note("alpha","completed","<result>"+("β"*result_len)+"</result>")}])
    case("first alias wins",[call(),receipt("",{"agentId":"explicit"})],
         {"agent-explicit.jsonl":"","agent-link.jsonl":"","agent-link.meta.json":'{"toolUseId":"alpha"}'})
    case("ignored meta and synthetic records",[dict(call(),isMeta=True),
         dict(call("Task","synthetic"),isApiErrorMessage=True),call("Bash","shown")])
    for value in [None,{},[],{"toolUseId":12},{"toolUseId":""}]:
        case("bad metadata",[call()],{"agent-kid.jsonl":"","agent-kid.meta.json":json.dumps(value)})
    return stories

if __name__=="__main__":
    if "--record" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(record(Path(tmp)), ensure_ascii=True))
    else:
        records=json.loads(subprocess.check_output([sys.executable,"-I",__file__,"--record"],env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"}))
        OUT.write_text("[\n"+",\n".join(json.dumps(r,ensure_ascii=True) for r in records)+"\n]\n",encoding="ascii")
        print(len(records),"parent transcripts")
