"""Account recovery through real CLI subprocesses and transcript drainers."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from lib import agents, backends, config, turn_dispatch
from lib.claude_failover import ClaudeFailover
from lib.protocol import AgentState


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX subprocess groups")
def test_two_claude_processes_stop_and_resume_existing_conversations(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\n" + '''
import json,os,sys,time
from pathlib import Path
root=Path(os.environ['FAILOVER_TEST_ROOT'])
args=sys.argv[1:]
flag='--resume' if '--resume' in args else '--session-id'
sid=args[args.index(flag)+1]
session=os.environ['CLAUDE_PWA_SESSION']
record={'pid':os.getpid(),'sid':sid,'session':session,'flag':flag}
with (root/'spawns.jsonl').open('a') as f: f.write(json.dumps(record)+'\\n')
message=json.loads(sys.stdin.readline())
transcript=root/'.claude/projects/test'/f'{sid}.jsonl'
transcript.parent.mkdir(parents=True,exist_ok=True)
with transcript.open('a') as f: f.write(json.dumps({'sessionId':sid,'type':'user','message':message['message']})+'\\n')
print(json.dumps({'type':'system','subtype':'init','session_id':sid}),flush=True)
if flag=='--resume':
 print(json.dumps({'type':'result','subtype':'success','result':'finished'}),flush=True)
else:
 (root/(session+'.ready')).touch()
 while len(list(root.glob('*.ready')))<2: time.sleep(.02)
 if session=='one':
  print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'rejected'}}),flush=True)
 while True: time.sleep(.05)
''')
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAILOVER_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: config.Config(
        claude_account_switch_command=("fake-selector",)))
    switched = []
    def switch(command, models):
        rows = [json.loads(line) for line in (tmp_path / "spawns.jsonl").read_text().splitlines()]
        assert len(rows) == 2
        for row in rows:
            try:
                os.kill(row["pid"], 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("Old Claude process was not reaped")
        switched.append(models)
        return True
    coordinator = ClaudeFailover(turn_dispatch._TURN_LOCK, switch=switch)
    monkeypatch.setattr(turn_dispatch, "_CLAUDE_FAILOVER", coordinator)
    ctx = SimpleNamespace(default_session="one", agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda event: None))
    service = turn_dispatch.TurnDispatchService(ctx, home=tmp_path)
    ids = []
    try:
        for session in ("one", "two"):
            agent_id = agents.create_agent(persona=session, voice_id="v", cwd=str(tmp_path),
                                           session=session, backend="claude")
            ids.append(agent_id)
            agents.start_runtime(agent_id, session)
            service.dispatch(text="complete the work", requested_session=session,
                             forced_session=session, trace_id=str(uuid.uuid4()),
                             synthesize_audio=False)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all((agents.latest_state(agent_id) or {}).get("kind") == AgentState.DONE
                   for agent_id in ids):
                break
            time.sleep(.05)
        else:
            raise AssertionError("Both conversations did not finish after recovery")
        assert switched == [[""]]
        rows = [json.loads(line) for line in (tmp_path / "spawns.jsonl").read_text().splitlines()]
        assert len(rows) == 4
        for session in ("one", "two"):
            attempts = [row for row in rows if row["session"] == session]
            assert [row["flag"] for row in attempts] == ["--session-id", "--resume"]
            assert attempts[0]["sid"] == attempts[1]["sid"]
            assert attempts[0]["pid"] != attempts[1]["pid"]
            from lib.transcript_log import parse_turns
            transcript = tmp_path / ".claude/projects/test" / f'{attempts[0]["sid"]}.jsonl'
            user_texts = [turn["text"] for turn in parse_turns(transcript)
                          if turn["role"] == "user"]
            assert user_texts == ["complete the work"]
    finally:
        for agent_id in ids:
            turn_dispatch.clear_for_agent(agent_id)
            for handle in backends.active_handles("claude", agent_id):
                handle.kill()
                handle.wait(timeout=5)
