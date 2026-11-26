//! Rows carried in `transcript-updated` (feature `events_rows`, contract 199)
//! against the fake Host: where the Host offers it, the stream asks with
//! `rows=1` and a change the event carries whole is drawn with no `/log`
//! read; anything else (tool rows, a gap, a withheld agent) reads `/log` as
//! before. Every step is run against a Host with the feature and one without,
//! and the two chats must be the same row for row.

mod common;

use clarp_core::protocol::Message;
use common::{Driver, Host};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

fn events_queries(host: &Host) -> Vec<Value> {
    host.requests("GET", "/events").into_iter().map(|r| r["query"].clone()).collect()
}

fn log_reads(host: &Host) -> usize {
    host.requests("GET", "/log").len()
}

fn uuid_like(text: &str) -> bool {
    text.len() == 36 && text.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
}

/// What the chat draws of a row. A send of this desktop is named by its text
/// (its id carries a fresh client id on each run).
fn drawn(rows: &[Message]) -> Vec<Value> {
    rows.iter()
        .map(|m| {
            let own = m.id.strip_prefix("u-").is_some_and(|rest| uuid_like(rest));
            let id = if own { format!("sent:{}", m.text) } else { m.id.clone() };
            json!({"id": id, "role": m.role, "text": m.text, "display": m.display_text, "kind": m.kind,
                   "revision": m.revision, "pending": m.pending, "sent_here": m.sent_here,
                   "failed": m.delivery_failed, "send_state": m.send_state, "tools": m.tools, "cells": m.display_cells,
                   "details": m.tool_details_available, "activity_count": m.activity_count, "phase": m.phase,
                   "origin": m.origin, "automated": m.automated, "activity": m.activity})
        })
        .collect()
}

struct Side {
    host: Host,
    driver: Driver,
}

impl Side {
    fn start(name: &str, rows: bool, withheld: &[&str], measure: bool) -> Self {
        let host = Host::start(name);
        host.control("/__control/events-rows", json!({"on": true, "offered": rows, "withheld": withheld, "measure": measure}));
        let mut driver = Driver::new(&host.base);
        driver.connect();
        Side { host, driver }
    }

    fn open(&mut self, session: &str) {
        self.driver.engine.select(session);
        self.driver.until("the chat's tail", |e| e.conversation(session).is_some_and(|c| c.latest_revision() > 0));
        // The first event after the Host started carries no rows_from: one
        // write so the next ones do.
        self.upsert(session, json!([{"id": format!("{session}-prime"), "role": "assistant", "text": "ready"}]), false);
    }

    fn head(&self, session: &str) -> i64 {
        self.host.control_json("/__control/head", json!({"session": session}))["head"].as_i64().unwrap_or(0)
    }

    /// One Host write, and the time until the chat holds it.
    fn upsert(&mut self, session: &str, turns: Value, missed: bool) -> Duration {
        let started = Instant::now();
        self.host.control("/__control/upsert", json!({"session": session, "turns": turns, "missed": missed}));
        if missed {
            return Duration::ZERO;
        }
        let head = self.head(session);
        self.driver.until("the write reaches the chat", |e| e.conversation(session).is_some_and(|c| c.latest_revision() >= head));
        started.elapsed()
    }

    fn rows(&self, session: &str) -> Vec<Value> {
        drawn(self.driver.engine.conversation(session).map(|c| c.rows()).unwrap_or_default())
    }
}

fn tool_card(n: usize) -> Value {
    json!({"id": format!("t{n}"), "name": "Bash", "summary": "cargo test", "action": "run", "status": "ok",
           "input": {"command": "cargo test --workspace"}, "output": "test result: ok. 412 passed".repeat(4)})
}

/// One answer, step by step: a message from another device, a reply that
/// streams, tool work, a heartbeat the chat never draws, the final reply.
fn answer_steps(session: &str, n: usize) -> Vec<(Value, bool)> {
    let live = format!("{session}-live-{n}");
    let reply = format!("{session}-a-{n}");
    vec![
        (json!([{"id": format!("{session}-u-{n}"), "role": "user", "origin": "user", "text": format!("Question {n}?")}]), false),
        (json!([{"id": live, "role": "assistant", "kind": "live", "text": "Let me"}]), false),
        (json!([{"id": live, "role": "assistant", "kind": "live", "text": "Let me look at the parser"}]), false),
        (json!([{"id": format!("{session}-c-{n}"), "role": "assistant", "phase": "commentary", "text": "Running the tests.",
                 "tools": [tool_card(1), tool_card(2)]}]), false),
        (json!([{"id": format!("{session}-hb-{n}"), "role": "user", "origin": "heartbeat", "automated": true, "automation_kind": "heartbeat",
                 "text": "Automated heartbeat"},
                {"id": format!("{session}-hba-{n}"), "role": "assistant", "origin": "heartbeat", "automated": true, "text": "Nothing to do",
                 "tools": [tool_card(3)]}]), false),
        (json!([{"id": reply, "role": "assistant", "phase": "final", "text": format!("All {n} tests pass.")}]), false),
        (json!([{"id": reply, "role": "assistant", "phase": "final", "text": format!("All {n} tests pass. The parser is fixed.")}]), false),
    ]
}

#[test]
fn the_stream_asks_for_rows_only_where_the_host_offers_them() {
    let with = Side::start("rows-on-query", true, &[], false);
    assert!(events_queries(&with.host).iter().all(|q| q["rows"] == "1"), "rows=1 on every stream: {:?}", events_queries(&with.host));
    let without = Side::start("rows-off-query", false, &[], false);
    assert!(events_queries(&without.host).iter().all(|q| q.get("rows").is_none()), "no rows=1 without the feature: {:?}", events_queries(&without.host));
}

#[test]
fn carried_rows_draw_the_same_chat_as_reading_the_log() {
    let mut with = Side::start("rows-on", true, &[], false);
    let mut without = Side::start("rows-off", false, &[], false);
    with.open("rachel");
    without.open("rachel");
    let before = log_reads(&with.host);
    for n in 0..2 {
        for (step, (turns, missed)) in answer_steps("rachel", n).into_iter().enumerate() {
            with.upsert("rachel", turns.clone(), missed);
            without.upsert("rachel", turns, missed);
            assert_eq!(with.rows("rachel"), without.rows("rachel"), "answer {n}, step {step}");
        }
    }
    // A send of this desktop: its own row, delivered, and the reply.
    for side in [&mut with, &mut without] {
        side.driver.engine.send("Thanks, ship it");
        side.driver.until("the echo", |e| e.conversation("rachel").is_some_and(|c| c.rows().iter().any(|m| m.text == "Echo: Thanks, ship it")));
        side.driver.until("delivered", |e| e.conversation("rachel").is_some_and(|c| c.rows().iter().any(|m| m.text == "Thanks, ship it" && !m.pending)));
        side.driver.settle(Duration::from_millis(300));
    }
    assert_eq!(with.rows("rachel"), without.rows("rachel"), "after a send of this desktop");
    // Plain rows came with no read; tool rows and sends still read.
    let with_reads = log_reads(&with.host) - before;
    assert!(with_reads < log_reads(&without.host), "fewer /log reads with rows: {with_reads} against {}", log_reads(&without.host));
}

#[test]
fn a_missed_event_reads_the_log_and_draws_the_same_chat() {
    let mut with = Side::start("rows-gap-on", true, &[], false);
    let mut without = Side::start("rows-gap-off", false, &[], false);
    with.open("rachel");
    without.open("rachel");
    let gap = json!([{"id": "missed", "role": "assistant", "text": "This one's event was lost"}]);
    let next = json!([{"id": "after", "role": "assistant", "text": "And this one came"}]);
    for side in [&mut with, &mut without] {
        side.upsert("rachel", gap.clone(), true);
    }
    let reads = log_reads(&with.host);
    with.upsert("rachel", next.clone(), false);
    without.upsert("rachel", next, false);
    assert!(log_reads(&with.host) > reads, "a gap in rows_from reads /log");
    assert_eq!(with.rows("rachel"), without.rows("rachel"));
    assert!(with.rows("rachel").iter().any(|r| r["id"] == "missed"), "the missed row is drawn");
}

#[test]
fn a_withheld_agent_reads_the_log_as_before() {
    let mut with = Side::start("rows-withheld-on", true, &["rachel"], false);
    let mut without = Side::start("rows-withheld-off", false, &[], false);
    with.open("rachel");
    without.open("rachel");
    let before = log_reads(&with.host);
    let steps = answer_steps("rachel", 0);
    let count = steps.len();
    for (turns, missed) in steps {
        with.upsert("rachel", turns.clone(), missed);
        without.upsert("rachel", turns, missed);
    }
    assert_eq!(with.rows("rachel"), without.rows("rachel"));
    assert!(log_reads(&with.host) - before >= count, "every event without rows reads /log");
}

// ---- measurement ---------------------------------------------------------
//
// Run on the build machine with
//   cargo test -p clarp-engine --test events_rows measure -- --ignored --nocapture
// Requests and bytes of one answer, the time from the Host's write to the
// chat holding it, and the CPU time of the thread that pumps the engine (the
// UI thread in the app), with and without the feature, for each case.

fn thread_cpu() -> Duration {
    let stat = std::fs::read_to_string("/proc/thread-self/schedstat").unwrap_or_default();
    Duration::from_nanos(stat.split_whitespace().next().and_then(|n| n.parse().ok()).unwrap_or(0))
}

/// (/log, events) bytes since `since`: as sent plain (`bytes`) or packed as
/// the Rust Host packs them for the desktop (`gz`).
fn bytes(host: &Host, since: f64, field: &str) -> (u64, u64) {
    let log = host.log();
    let sum = |method: &str| -> u64 {
        log.iter().filter(|l| l["method"] == method && l["at"].as_f64().unwrap_or(0.0) >= since).map(|l| l[field].as_u64().unwrap_or(0)).sum()
    };
    (sum("RESPONSE"), sum("SSE"))
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64()
}

struct Measured {
    reads: f64,
    plain_bytes: f64,
    gzip_log: f64,
    gzip_events: f64,
    write_to_drawn_ms: f64,
    cpu_us: f64,
}

fn measure_case(name: &str, rows: bool, withheld: &[&str], steps: &dyn Fn(usize) -> Vec<(Value, bool)>, answers: usize) -> Measured {
    let mut side = Side::start(&format!("measure-{name}-{rows}"), rows, withheld, true);
    side.open("rachel");
    side.driver.settle(Duration::from_millis(300));
    let (reads, since) = (log_reads(&side.host), now());
    let (mut latency, mut writes, mut cpu) = (Duration::ZERO, 0u32, Duration::ZERO);
    for n in 0..answers {
        for (turns, missed) in steps(n) {
            let start = thread_cpu();
            let took = side.upsert("rachel", turns, missed);
            cpu += thread_cpu() - start;
            if !missed {
                latency += took;
                writes += 1;
            }
        }
    }
    side.driver.settle(Duration::from_millis(500));
    let (log_bytes, event_bytes) = bytes(&side.host, since, "bytes");
    let (gzip_log, gzip_events) = bytes(&side.host, since, "gz");
    let per = answers as f64;
    Measured {
        reads: (log_reads(&side.host) - reads) as f64 / per,
        plain_bytes: (log_bytes + event_bytes) as f64 / per,
        gzip_log: gzip_log as f64 / per,
        gzip_events: gzip_events as f64 / per,
        write_to_drawn_ms: latency.as_secs_f64() * 1000.0 / f64::from(writes.max(1)),
        cpu_us: cpu.as_secs_f64() * 1e6 / per,
    }
}

#[test]
#[ignore = "a measurement, run by hand"]
fn measure() {
    let plain = |n: usize| -> Vec<(Value, bool)> {
        vec![
            (json!([{"id": format!("u-{n}"), "role": "user", "origin": "user", "text": format!("Question {n}?")}]), false),
            (json!([{"id": format!("live-{n}"), "role": "assistant", "kind": "live", "text": "Let me"}]), false),
            (json!([{"id": format!("live-{n}"), "role": "assistant", "kind": "live", "text": "Let me think about it"}]), false),
            (json!([{"id": format!("a-{n}"), "role": "assistant", "phase": "final", "text": format!("Answer {n}.")}]), false),
        ]
    };
    let tools = |n: usize| -> Vec<(Value, bool)> {
        (0..4)
            .map(|k| (json!([{"id": format!("c-{n}"), "role": "assistant", "text": "Working", "tools": (0..=k).map(tool_card).collect::<Vec<_>>()}]), false))
            .collect()
    };
    let mixed = |n: usize| answer_steps("rachel", n);
    let gap = |n: usize| -> Vec<(Value, bool)> {
        vec![
            (json!([{"id": format!("m-{n}"), "role": "assistant", "text": "lost"}]), true),
            (json!([{"id": format!("g-{n}"), "role": "assistant", "text": "after the gap"}]), false),
        ]
    };
    let cases: Vec<(&str, &[&str], &dyn Fn(usize) -> Vec<(Value, bool)>)> = vec![
        ("plain answer", &[], &plain),
        ("tool work only", &[], &tools),
        ("answer with tools and a heartbeat", &[], &mixed),
        ("missed event (gap)", &[], &gap),
        ("withheld agent", &["rachel"], &plain),
    ];
    const ANSWERS: usize = 10;
    let mut table = String::from(
        "| case | /log reads per answer | gzip /log bytes | gzip event bytes | gzip total bytes | plain total bytes | write to drawn (ms) | pump thread CPU (us) |\n|---|---|---|---|---|---|---|---|\n",
    );
    // Each side three times, alternating; the median of each figure.
    const ROUNDS: usize = 3;
    let median = |mut values: Vec<f64>| {
        values.sort_by(f64::total_cmp);
        values[values.len() / 2]
    };
    for (name, withheld, steps) in cases {
        let (mut before, mut after) = (Vec::new(), Vec::new());
        for round in 0..ROUNDS {
            before.push(measure_case(&format!("{}-{round}", name.replace(' ', "-")), false, withheld, steps, ANSWERS));
            after.push(measure_case(&format!("{}-{round}", name.replace(' ', "-")), true, withheld, steps, ANSWERS));
        }
        let pair = |figure: fn(&Measured) -> f64, digits: usize| {
            let (a, b) = (median(before.iter().map(figure).collect()), median(after.iter().map(figure).collect()));
            format!("{a:.digits$} → {b:.digits$}")
        };
        table += &format!(
            "| {name} | {} | {} | {} | {} | {} | {} | {} |\n",
            pair(|m| m.reads, 1),
            pair(|m| m.gzip_log, 0),
            pair(|m| m.gzip_events, 0),
            pair(|m| m.gzip_log + m.gzip_events, 0),
            pair(|m| m.plain_bytes, 0),
            pair(|m| m.write_to_drawn_ms, 2),
            pair(|m| m.cpu_us, 0),
        );
    }
    println!("\n{table}");
}
