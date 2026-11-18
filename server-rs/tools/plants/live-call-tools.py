#!/usr/bin/env python3
"""Wrong rules in the tools of a live call, planted against the Python Host's receipts."""
import runner

RUNS = {'receipts':['test','--test','live_call_tools_vectors']}
RECEIPTS = 'clarp-kernel/src/live_call/receipts.rs'
PLANTS = []
for what,old,new in [
    ('a refusal does not require a refusal status','code != Some(200)','code == Some(200)'),
    ('absence of intent is accepted','!answer["intent"].is_string()','false'),
    ('a direction may omit its session','answer.get("sesjon").is_none()','false'),
    ('a number says a send was observed','!answer["sent"].is_boolean()','false'),
    ('an unobserved forward is confirmed','answer["sent"] != true','false'),
    ('an unknown non-forward is definite','intent_status == "ukjent"','intent_status == "unknown"'),
    ('a task started now loses its note','answer["base_task"] == "started_now"','answer["base_task"] == "next"'),
    ('receipt lines have no separator','current.push_str(" | ")','current.push_str(" ")'),
    ('UTF-8 truncation loses an extra byte','limit.saturating_sub(3)','limit.saturating_sub(4)'),
    ('an empty agent label changes the action name','"action"','"unknown action"'),
    ('the member name exceeds its byte cap','&or(&r["agent"], ""), 40','&or(&r["agent"], ""), 41'),
    ('the status exceeds its byte cap','&or(&r["status"], "unknown"), 60','&or(&r["status"], "unknown"), 61'),
    ('an empty result loses its default reason','"missing fields"','"unavailable fields"'),
]:
    PLANTS.append((RECEIPTS,'receipts',what,old,new))

if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
