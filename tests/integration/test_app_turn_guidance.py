"""Every app turn tells the agent, once, to prefer the Clarp skills.

Real Host dispatch to the QA Codex app-server: a typed and a spoken user turn,
a peer message, and a goal's continuation, each resuming the same thread;
and a spoken message steered into a running typed turn.
"""
import json
import time

from lib.clarp_guidance import CLARP_SKILLS_GUIDANCE
from tests.integration.test_peer_requests import cli, host  # noqa: F401
from tests.integration.test_task_goal_recovery import create, wait_wake


def test_every_kind_of_app_turn_carries_the_guidance_once(host, tmp_path):
    p = create(host)   # typed user turn
    host.request("/send", {"session": "rachel", "text": "Spoken question", "client_msg_id": "spoken",
                           "synthesize_audio": True})
    host.wait_reply("rachel", "Spoken question")
    host.request("/send", {"session": "rachel", "text": "Peer note", "sender": "mike", "origin": "agent",
                           "client_msg_id": "peer-note", "synthesize_audio": False})
    host.wait_reply("rachel", "Peer note")
    cli(host, tmp_path, "request", "--to", "mike", "--from", "rachel", "--goal", p["plan_id"],
        "--deadline", "3", "--text", "Anything")
    wait_wake(host)
    host.wait_reply("rachel", "deadline passed")

    host.request("/send", {"session": "rachel", "text": "[qa-slow] typed work", "client_msg_id": "slow",
                           "synthesize_audio": False})
    time.sleep(0.5)
    host.request("/send", {"session": "rachel", "text": "Spoken aside", "client_msg_id": "aside",
                           "synthesize_audio": True})
    host.wait_reply("rachel", "typed work")

    requests = [json.loads(line) for line in
                (host.root / "provider" / "turn-requests.jsonl").read_text().splitlines()]
    starts = [r for r in requests if r["method"] == "turn/start"]
    steer = next(r for r in requests if r["method"] == "turn/steer")
    assert "Spoken aside" in steer["input"][0]["text"]
    # The steered turn already has the guidance; the follow-up adds only voice.
    follow_up = json.dumps(steer["additionalContext"])
    assert "<speak>" in follow_up and CLARP_SKILLS_GUIDANCE not in follow_up
    rachel = [s for s in starts if s["threadId"] == starts[0]["threadId"]]
    said = ["".join(i["text"] for i in s["input"]) for s in rachel]
    assert any("Spoken question" in t for t in said) and any("Peer note" in t for t in said)
    assert any("deadline passed" in t for t in said) and len(rachel) >= 4
    for start, text in zip(rachel, said):
        context = json.dumps(start["additionalContext"])
        assert context.count(CLARP_SKILLS_GUIDANCE) == 1
        assert CLARP_SKILLS_GUIDANCE not in text   # not repeated in the message
    spoken = rachel[said.index(next(t for t in said if "Spoken question" in t))]
    assert "<speak>" in spoken["additionalContext"]["clarp-app"]["value"]
