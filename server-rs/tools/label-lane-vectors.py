#!/usr/bin/env python3
"""Run due label checks at a fixed time in disposable reference stores."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from contextlib import ExitStack

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/"server-rs/crates/clarp-kernel/tests/fixtures/label-lane.json"

def capture():
    from lib import agents, db, heartbeat, janitor_autonomy as service, janitor_builtins, janitors, judgments, settings_store
    with ExitStack() as st:
        st.enter_context(patch.object(service.backends,"active_handles",return_value=[]))
        st.enter_context(patch.object(judgments,"log_exception"))
        st.enter_context(patch.object(judgments,"api_key",return_value="example"))
        st.enter_context(patch.object(heartbeat,"outside_active_hours",return_value=False))
        owner=janitor_builtins.ensure_builtins(cwd="/tmp")["label-auditor"]
        service.setup()
        for key in [judgments.KEY_ENABLED,"judgments.labels"]:
            settings_store.set_bool(key,True)
        start=db.now_ms()+1_000_000
        now=[start]
        st.enter_context(patch.object(db,"now_ms",side_effect=lambda:now[0]))
        def unexpected(*args):raise AssertionError("unexpected dispatch")
        cases=[]
        def check(choice):
            def post(body,key,seconds):
                if choice=="timeout":raise TimeoutError("synthetic")
                return {"answers":{q:{"choice":choice,"probabilities":{choice:1}} for q in body["questions"]}}
            with patch.object(judgments,"_post",side_effect=post):
                service.AutonomyJanitors(unexpected,unexpected).label_audit_once()
            lane=janitors.get(owner["session"])["cadence"][0]
            runs=janitors.list_runs(owner["session"])
            cases.append(dict(choice=choice,interval=lane["current_interval_seconds"],activity=lane["last_activity"],
                              summary=lane["last_summary"],runs=len(runs)))
            now[0]=lane["next_run_at"]
        check("accurate")
        aid=agents.create_agent(persona="Example",voice_id="",cwd="/tmp",session="sample")
        agents.record_state(aid,"background",{})
        agents.set_custom_status(aid,"Reviewing")
        now[0]+=900000
        for choice in ["finished","finished","accurate","finished","timeout"]:check(choice)
        return cases

if __name__=="__main__":
    spec=importlib.util.spec_from_file_location("replay",ROOT/"tests/replay/host.py")
    replay=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        (root/"scenario-config.toml").write_text("")
        replay.isolate(root)
        replay.loopback_only()
        sys.path.insert(0,str(ROOT/"server"))
        OUT.write_text(json.dumps(capture(),indent=1)+"\n")
