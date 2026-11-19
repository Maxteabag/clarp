#!/usr/bin/env python3
"""The label site's questions, transport, and lifecycle."""
import runner
K="clarp-kernel/src/janitors/label_audit.rs"
H="clarp-host/src/label_checker.rs"
RUNS={"vectors":["test","--test","label_site"],"transport":["test","--lib","a_loopback_judge_receives_one_batch_and_returns_each_choice"]}
plants=[]
def plant(file,run,old,new):
    plants.append((file,run,"change "+old+" to "+new,old,new))
for old,new in [
    ('entries.is_empty()', 'entries.len()==1'),
    ('format!("a{number}")','format!("b{number}")'),
    ('format!("l_{key}")','format!("q_{key}")'),
    ('"agents.a1"','"agents.a0"'),
    ('entry.packet.clone()','json!(entry.name)'),
    ('"model":"jev-latest"','"model":"other"'),
    ('"state":{"agents":agents}','"state":{"labels":agents}'),
    ('"questions":questions','"question":questions'),
    ('JUDGE_SECONDS: u64 = 15','JUDGE_SECONDS: u64 = 14')]:
    plant(K,"vectors",old,new)
plant("clarp-host/src/vocabulary/judge.rs","transport",'.header("Authorization", format!("Bearer {key}"))','.header("Authorization", format!("Token {key}"))')
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,plants))
