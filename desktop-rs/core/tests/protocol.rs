//! Ports of the protocol-level cases in `desktop/tests/tst_native_core.cpp`.
//! Assertions that need Qt's QTextDocument (rich-text round trips) belong to
//! the app crate and are tracked in PARITY.md.

use clarp_core::protocol::{Agent, AudioClip, Message, describe_subagent_cell, parse_iso_date};
use clarp_core::sse::SseParser;
use clarp_core::text::*;
use serde_json::{Value, json};

fn obj(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().cloned().expect("object literal")
}

#[test]
fn sidebar_preview_is_plain_text() {
    assert_eq!(
        plain_preview_text("| # | City | Country |\n|---|---|---|\n| 1 | Tokyo | Japan |"),
        "# City Country 1 Tokyo Japan"
    );
    assert_eq!(plain_preview_text("## Cities\n| a | b |\n|---|--…"), "Cities a b");
    assert_eq!(
        plain_preview_text("**Done** — see `main.py` and [the docs](https://x.test)."),
        "Done — see main.py and the docs."
    );
    assert_eq!(plain_preview_text("- first *point*\n> quoted"), "first point quoted");
    assert_eq!(plain_preview_text("snake_case_name stays"), "snake_case_name stays");
}

#[test]
fn spoken_markup_leaves_no_gaps_in_the_text() {
    let shown = |text: &str| Message::from_json(&obj(json!({"text": text}))).display_text;
    assert_eq!(
        shown("<speak><speed ratio=\"0.85\"/>Sure <break time=\"300ms\"/> here's a table.</speak>"),
        "Sure, here's a table."
    );
    assert_eq!(
        shown("Twelve keyboard lines, <vox>um</vox>, comma-heavy as requested <break time=\"300ms\"/> they're on your screen now."),
        "Twelve keyboard lines, comma-heavy as requested, they're on your screen now."
    );
    assert_eq!(shown("I <vox>um</vox> think so <vox>uh</vox>."), "I think so.");
    assert_eq!(shown("Done. <break time=\"1s\"/> Next step."), "Done. Next step.");
    assert_eq!(shown("Line one <break/>\nLine two"), "Line one\nLine two");
    // Spacing that is not next to spoken markup is left alone.
    assert_eq!(shown("```\na  =  1\n```\n| a |  b |"), "```\na  =  1\n```\n| a |  b |");
}

#[test]
fn streaming_reply_hides_an_unfinished_voice_tag() {
    let live = |text: &str| Message::from_json(&obj(json!({"text": text, "kind": "live"}))).display_text;
    assert_eq!(live("Hello <vox>um"), "Hello");
    assert_eq!(live("Hello <bre"), "Hello");
    assert_eq!(live("1 < 2"), "1 < 2");
}

#[test]
fn subagent_cells_describe_phase_name_and_task() {
    let cell = |title: &str, status: &str, summary: &str, lines: Value| {
        obj(json!({"kind": "subagents", "title": title, "status": status, "summary": summary, "lines": lines}))
    };
    let task_line = json!([{"label": "Task", "text": "Audit the parser"}]);
    let info = describe_subagent_cell(&cell("Spawning agent", "running", "Kepler (explorer)", task_line.clone()));
    assert_eq!(info["phase"], "spawned");
    assert_eq!(info["running"], true);
    assert_eq!(info["name"], "Kepler (explorer)");
    assert_eq!(info["task"], "Audit the parser");
    let info = describe_subagent_cell(&cell("Waiting for agents", "running", "2 agents", json!([])));
    assert_eq!(info["phase"], "waiting");
    let info = describe_subagent_cell(&cell(
        "Finished waiting", "ok", "agents",
        json!([{"label": "Kepler", "text": "Completed", "kind": "status"}]),
    ));
    assert_eq!(info["phase"], "finished");
    assert_eq!(info["task"], "Kepler: Completed");
    let info = describe_subagent_cell(&cell(
        "Closed agent", "recorded", "agent", json!([{"label": "Agent", "text": "019a-thread"}]),
    ));
    assert_eq!(info["phase"], "finished");
    assert_eq!(info["name"], "019a-thread");
    let info = describe_subagent_cell(&cell("Interrupted subagent", "error", "", json!([])));
    assert_eq!(info["phase"], "failed");
    assert_eq!(info["name"], "sub-agent");
    assert!(describe_subagent_cell(&obj(json!({"kind": "command"}))).is_empty());
}

#[test]
fn backend_quota_notice_names_reason_reset_and_fallback() {
    let now = parse_iso_date("2026-09-17T06:00:00Z").unwrap();
    let mut base = json!({"agent_id": "a", "session": "gordon", "backend": "codex", "latest_state": "interrupted"});
    let notice = |row: &Value| Agent::from_json(row.as_object().unwrap()).quota_notice(now);
    assert_eq!(notice(&base), "");
    base["backend_quota"] = Value::Null;
    assert_eq!(notice(&base), "");
    base["backend_quota"] = json!({"state": "exhausted", "provider_id": "codex", "reason": "credits_depleted",
        "window": "seven_day", "resets_at": "2026-09-22T11:30:00Z", "fallback_model": null});
    assert_eq!(
        notice(&base),
        "Codex workspace is out of credits  ·  included usage resets in 5d 5h  ·  a message will likely fail"
    );
    base["backend_quota"] = json!({"state": "exhausted", "provider_id": "codex", "reason": "usage_limit",
        "window": "unknown", "resets_at": null, "fallback_model": "claude-sonnet-5"});
    assert_eq!(notice(&base), "Codex is out of quota  ·  runs on claude-sonnet-5");
    // A running turn is not a moment to warn about the next one.
    base["busy"] = json!(true);
    assert_eq!(notice(&base), "");
    // A state this build does not know is not a warning.
    base["busy"] = json!(false);
    base["backend_quota"] = json!({"state": "throttled"});
    assert_eq!(notice(&base), "");
}

#[test]
fn agent_row_parses_background_and_helper_fields_safely() {
    let old = Agent::from_json(&obj(json!({"session": "a"})));
    assert_eq!((old.background_job_count, old.background_sub_agent_count), (0, 0));
    assert_eq!(old.role, "agent");
    assert!(old.parent_agent_id.is_empty() && old.helper_state.is_empty());
    assert_eq!((old.child_count, old.running_children), (0, 0));
    assert!(!old.is_helper());

    let current = Agent::from_json(&obj(json!({"session": "h",
        "background_jobs": {"count": 3, "sub_agents": 2}, "parent_agent_id": "p", "role": "helper",
        "helper_state": "reported", "child_count": 4, "running_children": 1})));
    assert_eq!((current.background_job_count, current.background_sub_agent_count), (3, 2));
    assert_eq!(current.parent_agent_id, "p");
    assert!(current.is_helper() && current.helper_finished() && !current.helper_running());
    assert_eq!((current.child_count, current.running_children), (4, 1));

    // Wrong types, nulls and impossible numbers fall back instead of leaking.
    let hostile = Agent::from_json(&obj(json!({"session": "x",
        "background_jobs": {"count": -2, "sub_agents": 9}, "parent_agent_id": null, "role": 7,
        "helper_state": null, "child_count": "many", "running_children": -1})));
    assert_eq!(hostile.background_job_count, 0);
    // Helpers are counted apart from processes, so sub_agents may exceed count.
    assert_eq!(hostile.background_sub_agent_count, 9);
    assert_eq!(hostile.role, "agent");
    assert_eq!((hostile.child_count, hostile.running_children), (0, 0));
    let not_object = Agent::from_json(&obj(json!({"session": "y", "background_jobs": 5})));
    assert_eq!(not_object.background_job_count, 0);
    // A helper without a parent is not nested anywhere.
    assert!(!Agent::from_json(&obj(json!({"role": "helper"}))).is_helper());
}

#[test]
fn clip_source_precedence_matches_contract() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../contract/fixtures/audio/clip-precedence.json");
    let fixture: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let sources: Vec<String> = fixture["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| AudioClip::from_json(s["clip"].as_object().unwrap()).preferred_source().to_owned())
        .collect();
    assert_eq!(sources, ["/audio/a.mp3", "/clips/2/stream", "/clips/3/list.m3u8", ""]);
}

#[test]
fn markdown_paragraphs_become_visible_display_blocks() {
    assert_eq!(markdown_display_blocks("First paragraph.\n\nSecond paragraph."), ["First paragraph.", "Second paragraph."]);
    assert_eq!(markdown_display_blocks("One visual line\nsoft continuation"), ["One visual line\nsoft continuation"]);
    assert_eq!(markdown_display_blocks("1. First item\n\n\n2. Second item"), ["1. First item\n\n2. Second item"]);
    assert_eq!(markdown_display_blocks("- First item\r\n\r\n- Second item"), ["- First item\n\n- Second item"]);
    assert_eq!(
        markdown_display_blocks("```text\nfirst line\n\nsecond line\n```\n\nAfter code."),
        ["```text\nfirst line\n\nsecond line\n```", "After code."]
    );
    assert!(markdown_display_blocks("  \n\n").is_empty());
}

#[test]
fn tool_output_links_are_anchored_without_changing_the_text() {
    let output = "  branch pushed\n\tsee https://example.com/pr/7 (open it)\ncontact www.example.com now\n<not a tag> & \"quoted\"";
    let rich = linkified_plain_text(output);
    assert!(rich.contains("<a href=\"https://example.com/pr/7\">"));
    assert!(rich.contains("<a href=\"https://www.example.com\">"));
    assert!(!rich.contains("href=\"https://example.com/pr/7("));
    assert!(rich.contains("&lt;not a tag&gt; &amp; &quot;quoted&quot;"));
    assert!(rich.starts_with("<div style=\"white-space: pre-wrap;\">"));
    assert_eq!(linkified_plain_text("plain\n  text"), "<div style=\"white-space: pre-wrap;\">plain\n  text</div>");
    // Stripping the markup and unescaping must give back the exact text
    // (the C++ test checks this with QTextDocument).
    let tags = fancy_regex::Regex::new("<[^>]+>").unwrap();
    let plain = tags.replace_all(&rich, "").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", "\"").replace("&amp;", "&");
    assert_eq!(plain, output);
}

#[test]
fn report_html_cannot_fetch_remote_resources() {
    let host = "https://host.example";
    let report = "<html><head><style>@import url(\"http://tracker.example/imported.css\");\
        body{background-image:url('http://tracker.example/bg.png')}\
        .x{background:url(http://tracker.example/shorthand.png)}</style></head><body>\
        <p style=\"background-image:url(http://tracker.example/inline.png)\">styled</p>\
        <img src=\"http://tracker.example/pixel.png\">\
        <table background=\"http://tracker.example/tablebg.png\"><tr><td>c</td></tr></table>\
        <script>fetch('http://tracker.example/beacon')</script></body></html>";
    let safe = sanitized_report_html(report, host);
    assert!(!safe.contains("tracker.example"));
    assert!(!safe.to_lowercase().contains("@import"));
    assert!(!safe.to_lowercase().contains("<script"));
    assert!(!safe.contains("beacon"));
    assert!(safe.contains("styled") && safe.contains("<table"));

    let kept = sanitized_report_html(
        "<img src=\"data:image/gif;base64,R0lGODlh\"><img src=\"https://host.example/static/avatars/a.png\">\
         <p style=\"background-image:url('data:image/png;base64,AAA')\">x</p>",
        host,
    );
    assert!(kept.contains("data:image/gif;base64,R0lGODlh"));
    assert!(kept.contains("https://host.example/static/avatars/a.png"));
    assert!(kept.contains("url('data:image/png;base64,AAA')"));
    assert!(!sanitized_report_html("<img src=\"https://host.example/x.png\">", "").contains("host.example"));
    let tricky = sanitized_report_html(
        "<img src=\"//tracker.example/p.png\"><img src=' http://tracker.example/pad.png '>\
         <IMG SRC=HTTP://TRACKER.EXAMPLE/UPPER.PNG>",
        host,
    );
    assert!(!tricky.to_lowercase().contains("tracker"));
    assert!(looks_like_html_report("<html><body><h1>R</h1></body></html>"));
    assert!(looks_like_html_report("<div class=\"card\">x</div>"));
    assert!(!looks_like_html_report("# Heading\n\nPlain **markdown** only."));
}

#[test]
fn report_html_keeps_structure_but_never_fetches_remote_resources() {
    let host = "https://host.example";
    let report = "<html><head><style>@import url(\"http://tracker.example/x.css\");\
        body{background-image:url('http://tracker.example/bg.png')}\
        .c{background:url(http://tracker.example/short.png)}</style></head><body>\
        <h1>Quarterly report</h1><p style=\"background-image:url(http://tracker.example/inline.png)\">Body</p>\
        <img src=\"http://tracker.example/pixel.png\"><img src=\"data:image/gif;base64,R0lGODlh\">\
        <img src=\"https://host.example/static/chart.png\">\
        <table background=\"http://tracker.example/tablebg.png\"><tr><td>1</td></tr></table>\
        <script>fetch('http://tracker.example/beacon')</script>\
        <a href=\"https://example.com/source\">source</a></body></html>";
    let safe = sanitized_report_html(report, host);
    assert!(!safe.contains("tracker.example") && !safe.contains("beacon"));
    assert!(safe.contains("data:image/gif;base64,R0lGODlh"));
    assert!(safe.contains("https://host.example/static/chart.png"));
    assert!(safe.contains("<h1>Quarterly report</h1>"));
    assert!(safe.contains("https://example.com/source"));
    let no_host = sanitized_report_html(report, "");
    assert!(!no_host.contains("tracker.example") && !no_host.contains("host.example/static"));
    assert!(no_host.contains("data:image/gif"));
    assert!(looks_like_html_report("<h1>Title</h1><p>x</p>"));
    assert!(looks_like_html_report("<!DOCTYPE html><html><body>x</body></html>"));
    assert!(!looks_like_html_report("# Title\n\nA paragraph with <not-a-tag>."));
    assert!(!looks_like_html_report("Plain text 1 < 2 and 3 > 2."));
}

#[test]
fn ported_urls_become_links_without_changing_visible_text() {
    assert_eq!(
        markdown_with_explicit_autolinks("Open: https://elitebook.tailf14237.ts.net:14443/final/"),
        "Open: <https://elitebook.tailf14237.ts.net:14443/final/>"
    );
    assert!(markdown_with_explicit_autolinks("http://127.0.0.1:8080/x").contains("<http://127.0.0.1:8080/x>"));
    assert!(markdown_with_explicit_autolinks("see https://host:7699 now").contains("<https://host:7699>"));
    assert_eq!(markdown_with_explicit_autolinks("https://example.com/final/"), "https://example.com/final/");
    let labelled = "[the lab](https://host:14443/final/) and <https://host:9/x>";
    assert_eq!(markdown_with_explicit_autolinks(labelled), labelled);
    assert_eq!(markdown_with_explicit_autolinks("run `curl https://host:14443/x`"), "run `curl https://host:14443/x`");
    let fenced = markdown_with_explicit_autolinks("```\ncurl https://host:14443/x\n```\nthen https://host:14443/y");
    assert!(fenced.contains("curl https://host:14443/x\n"));
    assert!(!fenced.contains("<https://host:14443/x>"));
    assert!(fenced.contains("then <https://host:14443/y>"));
    assert_eq!(markdown_with_explicit_autolinks("    https://host:14443/x"), "    https://host:14443/x");
    assert_eq!(markdown_with_explicit_autolinks("go to https://host:14443/x)."), "go to <https://host:14443/x>).");
}

#[test]
fn sse_parser_handles_chunks_comments_and_replay_ids() {
    let mut parser = SseParser::default();
    assert!(parser.feed(b": connected\r\n\r\nid: 41\r\ndata: {\"type\":\"agent-").is_empty());
    let messages = parser.feed(b"roster\"}\r\n\r\n");
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].id, "41");
    assert_eq!(messages[0].data["type"], "agent-roster");
}

#[test]
fn sse_parser_joins_multiline_data_and_survives_a_split_crlf() {
    let mut parser = SseParser::default();
    assert!(parser.feed(b"event: x\r").is_empty());
    let messages = parser.feed(b"\ndata: {\"a\":\ndata: 1}\r\n\r\ndata: not json\n\n");
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].event, "x");
    assert_eq!(messages[0].data["a"], 1);
}

#[test]
fn only_web_and_mail_links_are_openable() {
    for ok in ["https://example.com/a?b=1#c", "http://example.com", "  https://example.com/padded  ",
               "HTTPS://Example.com/upper", "mailto:team@example.com"] {
        assert!(is_openable_link(ok), "{ok}");
    }
    for bad in ["file:///etc/passwd", "https:///etc/passwd", "javascript:alert(1)", "smb://host/share",
                "ssh://box/repo", "mailto:", "/plain/path", "", "http:example.com"] {
        assert!(!is_openable_link(bad), "{bad}");
    }
}
