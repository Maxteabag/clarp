//! The visual rubric over the desktop's own text (issue #888): every string
//! the `.slint` files draw and every prose string in the Rust that builds
//! screen text is read through `ui_text::breaks`, and the check fails on
//! internal words, raw ids, HTTP statuses and the error catalogue's phrasing
//! (CL-070, CL-071, CL-077, CL-078, CL-079). Where such a word must stay (a search
//! alias, a shell line, a log header), the line or the line above carries
//! `ui-text: allow (<why>)`; an allow without a reason fails too. Strings on
//! today's screen that break a rule and whose fix waits for the owner's
//! approval are named in `WAITING`, so the check holds new text to the rubric
//! without changing what the app shows; an entry no string matches any more
//! fails, so each approved fix takes its entry out.

use std::fs;
use std::path::{Path, PathBuf};

use clarp_core::ui_text::breaks;

/// The desktop's crates, from `core/`.
fn desktop() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..")
}

/// Where screen text is written: the crates' sources and the Slint UI.
const SOURCES: &[&str] = &["core/src", "engine/src", "net/src", "slint-app/src", "slint-app/ui", "switcher-slint/src", "switcher-slint/ui"];

/// Files that draw nothing: the headless checks and their driver.
fn draws_nothing(path: &Path) -> bool {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or_default();
    name.ends_with("_checks.rs") || matches!(name, "driver.rs" | "headless.rs" | "ui_text.rs")
}

fn files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            files(&path, out);
        } else if matches!(path.extension().and_then(|e| e.to_str()), Some("rs" | "slint")) && !draws_nothing(&path) {
            out.push(path);
        }
    }
}

/// A string literal and the line it starts on.
struct Literal {
    line: usize,
    text: String,
}

/// The string literals of Rust or Slint source, comments skipped. Raw
/// strings (`r#"…"#`) and escapes are read; char literals are stepped over.
fn literals(source: &str) -> Vec<Literal> {
    let bytes = source.as_bytes();
    let mut out = Vec::new();
    let (mut i, mut line) = (0, 1);
    let ident = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'\n' {
            line += 1;
            i += 1;
        } else if source[i..].starts_with("//") {
            i = source[i..].find('\n').map_or(bytes.len(), |n| i + n);
        } else if source[i..].starts_with("/*") {
            let end = source[i + 2..].find("*/").map_or(bytes.len(), |n| i + 2 + n + 2);
            line += source[i..end].matches('\n').count();
            i = end;
        } else if b == b'r' && (i == 0 || !ident(bytes[i - 1])) && matches!(bytes.get(i + 1), Some(b'"' | b'#')) {
            let hashes = source[i + 1..].bytes().take_while(|c| *c == b'#').count();
            if bytes.get(i + 1 + hashes) != Some(&b'"') {
                i += 1;
                continue;
            }
            let start = i + 2 + hashes;
            let close = format!("\"{}", "#".repeat(hashes));
            let end = source[start..].find(&close).map_or(bytes.len(), |n| start + n);
            out.push(Literal { line, text: source[start..end].to_owned() });
            line += source[start..end].matches('\n').count();
            i = end + close.len();
        } else if b == b'\'' {
            // A char literal ('a', '\n', '\u{2026}'); a lifetime has no closing quote.
            let rest = &source[i + 1..];
            let len = if rest.starts_with('\\') { rest.find('\'').filter(|n| *n <= 10) } else { rest.chars().next().filter(|c| rest[c.len_utf8()..].starts_with('\'')).map(char::len_utf8) };
            i += len.map_or(1, |n| n + 2);
        } else if b == b'"' {
            let mut text = String::new();
            let mut j = i + 1;
            while j < bytes.len() && bytes[j] != b'"' {
                let c = source[j..].chars().next().expect("a char");
                if c == '\\' {
                    let next = source[j + 1..].chars().next().unwrap_or(' ');
                    text.push(c);
                    text.push(next);
                    j += 1 + next.len_utf8();
                } else {
                    text.push(c);
                    j += c.len_utf8();
                }
            }
            out.push(Literal { line, text: text.clone() });
            line += text.matches('\n').count();
            i = j + 1;
        } else {
            i += source[i..].chars().next().map_or(1, char::len_utf8);
        }
    }
    out
}

/// Words a reader could meet: a string with a space in it, or one
/// capitalised word ("Spool"). A lone lowercase token is a key or an id
/// the code reads, never text it draws.
fn prose(text: &str) -> bool {
    let text = text.trim();
    text.contains(' ') || (text.len() > 1 && text.starts_with(|c: char| c.is_ascii_uppercase()) && text[1..].chars().all(|c| c.is_ascii_lowercase()))
}

/// A line that only logs or asserts: the reader never sees it.
fn logs_only(line: &str) -> bool {
    ["eprintln!", "println!", "panic!", "debug!(", "info!(", "warn!(", "error!(", "trace!(", "log::", ".expect(", "assert", "unreachable!", "tracing::"]
        .iter()
        .any(|m| line.contains(m))
}

/// The line's allow, if any: `Some(true)` with a reason, `Some(false)` without.
fn allowed(lines: &[&str], line: usize) -> Option<bool> {
    let marked = |l: &str| l.split_once("ui-text: allow").map(|(_, why)| why.trim().trim_start_matches('(').trim_end_matches(')').trim().len() > 3);
    lines.get(line - 1).and_then(|l| marked(l)).or_else(|| line.checked_sub(2).and_then(|p| lines.get(p)).and_then(|l| marked(l)))
}

/// Known, waiting for approval: screen text today's app draws that the
/// rubric refuses, each kept as it is until its fix is approved one by one
/// (issue #888). File, the literal as written, the catalogue entry.
const WAITING: &[(&str, &str, &str)] = &[
    ("core/src/catalog.rs", "Harness default", "CL-078"),
    ("core/src/conversation.rs", "{persona} is working", "CL-070"),
    ("core/src/narrator.rs", "Off \u{2014} no background requests", "CL-077"),
    ("core/src/prefs.rs", "{} \u{2014} {} (:set {}; aliases: {})", "CL-077"),
    ("core/src/prefs.rs", "{} \u{2014} {}", "CL-077"),
    ("core/src/prefs.rs", "Font size \u{2014} Size of message text in the chat (:set fontsize)", "CL-077"),
    ("engine/src/artifact_summary.rs", "{message} (HTTP {status})", "CL-078"),
    ("engine/src/form_events.rs", "{message} (HTTP {status})", "CL-078"),
    ("engine/src/lib.rs", "{message} (HTTP {status})", "CL-078"),
    ("engine/src/lifecycle.rs", "{} \u{2014} {}", "CL-077"),
    ("engine/src/panels.rs", "{message} (HTTP {status})", "CL-078"),
    ("engine/src/profile.rs", "HTTP {status}", "CL-078"),
    ("engine/src/queue.rs", "{message} (HTTP {status})", "CL-078"),
    ("net/src/api.rs", "HTTP {} {reason}", "CL-078"),
    ("net/src/api.rs", "HTTP {}", "CL-078"),
    ("net/src/credentials/keychain.rs", "no stdin for the Keychain tool", "CL-078"),
    ("net/src/pairing.rs", "Pairing failed with HTTP {}", "CL-078"),
    ("net/src/sse.rs", "Event stream failed: HTTP {}", "CL-078"),
    ("slint-app/src/keymap.rs", "Harness", "CL-078"),
    ("slint-app/src/platform/audio.rs", "{message} (HTTP {status})", "CL-078"),
    ("slint-app/ui/app.slint", " \u{2014} Clarp", "CL-077"),
    ("slint-app/ui/launch.slint", "Harness", "CL-078"),
    ("slint-app/ui/overview.slint", "harness default", "CL-078"),
    ("slint-app/ui/preview.slint", "Pinned to an older build \u{2014} automatic updates paused", "CL-077"),
];

#[test]
fn the_desktop_draws_no_text_the_rubric_refuses() {
    let root = desktop();
    let mut paths = Vec::new();
    for dir in SOURCES {
        files(&root.join(dir), &mut paths);
    }
    paths.sort();
    assert!(paths.len() > 50, "the sources were found: {}", paths.len());
    let mut found = Vec::new();
    let mut waited = vec![false; WAITING.len()];
    for path in &paths {
        let source = fs::read_to_string(path).expect("source reads");
        // A Rust file's tests draw nothing.
        let source = source.split("#[cfg(test)]").next().unwrap_or_default();
        let lines: Vec<&str> = source.lines().collect();
        let name = path.strip_prefix(&root).unwrap_or(path).display().to_string();
        for literal in literals(source) {
            if !prose(&literal.text) || lines.get(literal.line - 1).is_some_and(|l| logs_only(l)) {
                continue;
            }
            let wrong = breaks(&literal.text);
            match allowed(&lines, literal.line) {
                Some(true) => {}
                Some(false) => found.push(format!("{name}:{}: an allow needs its reason in the allow", literal.line)),
                None if !wrong.is_empty() => match WAITING.iter().position(|(file, text, _)| name == *file && literal.text == *text) {
                    Some(at) => waited[at] = true,
                    None => found.push(format!("{name}:{}: {} in {:?}", literal.line, wrong.join(", "), literal.text)),
                },
                None => {}
            }
        }
    }
    for ((file, text, id), seen) in WAITING.iter().zip(waited) {
        if !seen {
            found.push(format!("{file}: {text:?} ({id}) is in WAITING but no longer on screen: take its entry out"));
        }
    }
    assert!(found.is_empty(), "screen text the rubric refuses:\n{}", found.join("\n"));
}

#[test]
fn the_literal_reader_reads_strings_and_skips_comments_and_chars() {
    let source = "let a = 'x'; // \"not this\"\nlet b = \"one {name}\"; /* \"nor this\" */ let c = r#\"raw \"two\"\"#;\nfn f<'a>(s: &'a str) {}\nText { text: \"Slint\"; }";
    let read: Vec<(usize, String)> = literals(source).into_iter().map(|l| (l.line, l.text)).collect();
    assert_eq!(read, [(2, "one {name}".to_owned()), (2, "raw \"two\"".to_owned()), (4, "Slint".to_owned())]);
}

#[test]
fn the_rules_refuse_internal_words_raw_ids_statuses_and_the_catalogue_s_phrasing() {
    for wrong in [
        "Lane memory",
        "Reading the spool",
        "Missed events after lseq {n}",
        "A new epoch",
        "Harness default",
        "Open the trace",
        "Stop for {name}: Stop acknowledged · Queue paused · No active goals paused · Actor: user (verified)",
        "{persona} is working",
        "Thinking: We need to write",
        "Thought for 0s: The user wants me to run s",
        "{message} (HTTP {status})",
        "Pairing failed with HTTP 409",
        "the Host answered 500",
        "Missing turn_id",
        "Agent 7bb2db18b7f94888 left",
        "Chat 3f2a9c1e-77b0-4d1a-9e0e-1b2c3d4e5f60",
        "Off \u{2014} no background requests",
        "Showing cached messages: refresh pending",
        "Showing cached messages \u{2014} refresh pending",
    ] {
        assert!(!breaks(wrong).is_empty(), "refused: {wrong:?}");
    }
    for fine in [
        "Stopped.",
        "Worked for 11s · 3 tools",
        "Thought for 4s: Finding the flaky test",
        "Thinking",
        "Working",
        "Change backend",
        "Queue paused",
        "{lanes} · {busy} busy · {idle} idle",
        "Draws the chats at 100% again.",
        "Could not load the plan: Gateway Timeout",
        "Default",
        "Off: no background requests",
    ] {
        assert_eq!(breaks(fine), Vec::<String>::new(), "allowed: {fine:?}");
    }
}
