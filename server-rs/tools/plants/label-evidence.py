#!/usr/bin/env python3
"""Candidate selection, packet minimization, and selective history reads."""
import runner

FILE="clarp-kernel/src/janitors/label_evidence.rs"
changes=[
    ("!c.excluded","c.excluded"),("!c.busy","c.busy"),
    (">= 900_000","> 900_000"),(">= 900_000",">= 899_999"),
    ('c.state == "background"','c.state == "done"'),
    ("eligible.sort_by_key(|c| c.idle_since)","eligible.sort_by_key(|c| std::cmp::Reverse(c.idle_since))"),
    ("eligible.truncate(12)","eligible.truncate(13)"),("eligible.truncate(12)","eligible.truncate(11)"),
    ("jobs.iter().take(3)","jobs.iter().take(4)"),("jobs.iter().take(3)","jobs.iter().take(2)"),
    ("helpers.iter().take(4)","helpers.iter().take(5)"),("helpers.iter().take(4)","helpers.iter().take(3)"),
    ("messages.len().saturating_sub(3)","messages.len().saturating_sub(2)"),
    ("now.saturating_sub(n).max(0)","now.saturating_sub(n).abs() + 60000"),
    ("/ 60_000","/59_000"),("at.filter(|n| *n != 0)","at.filter(|n|*n>=0)"),
    ("redact(&unescape(text))","unescape(text)"),('TAG.replace_all(&clean, " ")','TAG.replace_all(&clean, "!")'),
    ("tokens.next().is_some()","tokens.next().is_none()"),("chars - 1","chars"),("rsplit_once(' ')","split_once(' ')"),
    ("Working in the background","Working"),("char::REPLACEMENT_CHARACTER","'?'"),
    ("TABLE.numeric.get(&n)","TABLE.numeric.get(&0)"),
    ("TABLE.named.get(&name[..end])",'TABLE.named.get("unknown-entity")'),
]
# Make each partial or renamed packet visible in the expected JSON.
for field in ["state_age_min","turn_ended_min_ago","recent_messages","heartbeat_age_min","progress_age_min","state_detail","age_min"]:
    old='"'+field+'":'
    changes.append((old, '"wrong_'+field+'":'))
RUNS={"vectors":["test","--test","label_evidence"],"host":["test","--lib","history_is_opened_only_for_the_twelve_oldest_labels"]}
plants=[(FILE,"vectors",f"change {a} to {b}",a,b) for a,b in changes]
plants.append(("clarp-host/src/label_checker.rs","host","omit the selected targets' history",
               "let messages = history(c.agent_id.clone()).await?;","let messages:Vec<Value>=Vec::new();"))
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,plants))
