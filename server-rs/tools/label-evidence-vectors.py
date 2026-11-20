#!/usr/bin/env python3
"""Drive the Python candidate scan with isolated roster, activity, and history ports."""
import importlib.util
import json
import sys
from contextlib import ExitStack
from html import entities, _invalid_charrefs
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT/"server-rs/crates/clarp-kernel"


def capture():
    from lib import label_audit as audit
    now=4000000
    def row(i=1, **updates):
        return dict(dict(agent_id=f"id-{i}",name=f"Agent {i}",busy=False,excluded=False,idle_since=now-900000,
                         label="Testing",state="done",detail="",state_at=0,ended_at=None,jobs=[],helpers=[]),**updates)
    def one(candidates, messages):
        reads=[]
        def history(*,agent_id,backend_session_id,limit):
            assert limit==3
            reads.append(agent_id)
            return messages[-limit:]
        with ExitStack() as f:
            def fake(module,name,value):
                f.enter_context(patch.object(module,name,return_value=value))
            ids={c["agent_id"]:c for c in candidates}
            agents=[dict(agent_id=i,session=c["name"],custom_status=c["label"],is_janitor=c["excluded"]) for i,c in ids.items()]
            states={i:dict(kind=c["state"],ts=c["state_at"],last_turn_end=c["ended_at"],detail={"message":c["detail"]}) for i,c in ids.items()}
            fake(audit.agents_db,"list_agents",agents)
            fake(audit.agents_db,"dashboard_states",states)
            fake(audit.janitors,"visible_labels",{})
            fake(audit.background_jobs,"active_by_agent",{i:c["jobs"] for i,c in ids.items()})
            fake(audit.snapshot,"helper_tree",SimpleNamespace(helper_sessions={},helper_activity={i:c["helpers"] for i,c in ids.items()}))
            fake(audit.stale_work,"activities",{i:SimpleNamespace(busy=c["busy"],idle_since_ms=c["idle_since"]) for i,c in ids.items()})
            fake(audit.agents_db,"live_backend_session","synthetic-session")
            f.enter_context(patch.object(audit.agents_db,"list_messages",side_effect=history))
            result=audit.candidates(now)
        return dict(candidates=candidates,messages=messages,now=now,reads=reads,result=result)
    cases=[one([],[]),one([row(i,idle_since=now-900000-i%7) for i in range(18)],[])]
    for age in (899999,900000,900001):
        for busy in (False,True):
            for excluded in (False,True):
                cases.append(one([row(idle_since=now-age,busy=busy,excluded=excluded)],[]))
    for elapsed in (0, 1, 59_000, 59_999, 60_000, 60_001, 119_999):
        cases.append(one([row(state_at=now-elapsed,ended_at=now-elapsed)],[]))
    for state in ("done","background"):
        for label in ("","Testing"):
            cases.append(one([row(state=state,label=label)],[]))
    for size in (0,1,2,3,4,5):
        value="&lt;b&gt;A&lt;/b&gt; &copy; &amp; &#x80; &#0; &#55296; &#11; sk-synthetic Authorization: Bearer demo"
        jobs=[dict(title=value,kind="test",progress_text=("data "*39)+str(i),active_at=now-i*60000,heartbeat_at=now+5000) for i in range(size)]
        helpers=[dict(session=f"helper-{i}",bucket="waiting",text="123 "*36,ts=now-60000*i) for i in range(size)]
        cases.append(one([row(detail=value,jobs=jobs,helpers=helpers,ended_at=now-59999)], [{"role":"assistant","text":value+str(i)} for i in range(size)]))
    for text in ("x"*299,"x"*300,"x"*301,"A "*49,"A "*50,"A "*51,"a "*10+"Z"*299,
                 "&#13;&#x10ffff;&#65536;&#99999999999;&notin;&notit;&bogus; &fjlig;",
                 "-----BEGIN PRIVATE KEY-----test-----END PRIVATE KEY----- sk-one sk-two"):
        cases.append(one([row(detail=text,ended_at=now-60000)],[{"role":"user","text":text}]))
    return cases


if __name__=="__main__":
    spec=importlib.util.spec_from_file_location("replay",ROOT/"tests/replay/host.py")
    replay=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    with tool_scratch.directory() as directory:
        root=Path(directory)
        (root/"scenario-config.toml").write_text("")
        replay.isolate(root)
        replay.loopback_only()
        sys.path.insert(0,str(ROOT/"server"))
        (KERNEL/"tests/fixtures/label-evidence.json").write_text(json.dumps(capture(),separators=(",",":"))+"\n",encoding="ascii")
        (KERNEL/"src/janitors/label-entities.json").write_text(json.dumps(dict(named=entities.html5,numeric=_invalid_charrefs),sort_keys=True,separators=(",",":"))+"\n",encoding="ascii")
