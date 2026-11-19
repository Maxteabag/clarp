#!/usr/bin/env python3
"""The port of the Python Host's PR 451 (R92): every failed turn's state names its trace, a Stop
names the turn it stopped, a job names the turn it was registered in and none between turns,
and a Stop while another message waits ends the turn that ran and leaves the other waiting."""
import runner

W = "clarp-host/src/wire.rs"
A = "clarp-host/src/agent.rs"
T = "clarp-host/src/tools.rs"
K = "clarp-kernel/src/agent.rs"
RUNS = {
    "wire": ["test", "-p", "clarp-host", "--lib", "wire::tests::events_have_the_shapes_the_apps_read"],
    "stop": ["test", "-p", "clarp-host", "--test", "host", "a_stop_while_another_message_waits"],
    "job": ["test", "-p", "clarp-host", "--test", "host", "a_job_registered_between_turns"],
}
PLANTS = [
    (W, "wire", "a failure without its trace",
     '// Every failure names its turn\'s trace.\n                "trace_id": trace,', '// Every failure names its turn\'s trace.'),
    (W, "wire", "the trace of any turn that ended",
     '.filter(|end| end.turn == last.id.0)\n        .and_then(|end| end.trace_id.clone())', '.and_then(|end| end.trace_id.clone())'),
    (W, "stop", "a Stop that names no trace",
     'if !trace.is_empty() {\n                detail.insert("trace_id".to_string(), json!(trace));', 'if trace.is_empty() {\n                detail.insert("trace_id".to_string(), json!(trace));'),
    (A, "stop", "the Stop told at once without its end",
     'wire::stopped(&record, &rested, self.ended.as_ref())', 'wire::stopped(&record, &rested, None)'),
    (W, "stop", "an ended state told without its end",
     '(None, Some(last)) => ending_detail(last, backend, &record.model, ended),', '(None, Some(last)) => ending_detail(last, backend, &record.model, None),'),
    (T, "job", "a job between turns given a trace",
     '.and_then(|live| live.trace_id.clone())\n                .unwrap_or_default();', '.and_then(|live| live.trace_id.clone())\n                .unwrap_or_else(|| "the-last-turn".to_string());'),
    (T, "job", "a job in a turn given none",
     '.and_then(|live| live.trace_id.clone())\n                .unwrap_or_default();', '.and_then(|_| None::<String>)\n                .unwrap_or_default();'),
    (K, "stop", "a Stop that lets what waits run",
     'work.record(AgentEvent::Paused {\n                    by: by.actor,\n                    verified: by.verified,\n                    reason: by.reason,\n                    at_ms: now_ms,\n                });', 'let _ = by;'),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
