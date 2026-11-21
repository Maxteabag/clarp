//! Leader decision-memory text and deterministic matching rules.
use sha2::{Digest, Sha256};

use crate::python;

const CORE: [&str; 3] = ["## Current Value Model", "## Anti-Goals", "## Risk Posture"];
const LIMIT: usize = 1_800;
const EMPTY: &str = "No user values document has been seeded yet.";

pub fn words(text: &str) -> String {
    text.split(python::blank)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn question_key(question: &str) -> String {
    format!("{:x}", Sha256::digest(words(question).to_lowercase()))
}

pub fn compact_user_values(document: &str) -> String {
    let mut kept = Vec::new();
    let mut skipping = false;
    for line in python::splitlines(document) {
        if python::strip(line) == "## Recent Promotions" {
            skipping = true;
            continue;
        }
        if skipping && line.starts_with("## ") {
            skipping = false;
        }
        if !skipping {
            kept.push(line);
        }
    }
    let curated = kept.join("\n");
    let curated = python::strip(&curated);
    if curated.is_empty() {
        return EMPTY.to_string();
    }
    let mut sections: [Vec<&str>; 3] = Default::default();
    let mut at = None;
    for line in python::splitlines(curated) {
        let stripped = python::strip(line);
        if stripped.starts_with("## ") {
            at = CORE.iter().position(|heading| *heading == stripped);
        }
        if let Some(at) = at {
            sections[at].push(line);
        }
    }
    let text = if sections.iter().all(Vec::is_empty) {
        curated.to_string()
    } else {
        let mut out = vec![
            "# User Values",
            "",
            "Core per-turn value model. Search SQLite memory for promoted facts.",
        ];
        for section in sections.into_iter().filter(|section| !section.is_empty()) {
            out.push("");
            out.extend(section);
        }
        python::strip(&out.join("\n")).to_string()
    };
    if let Some((cut, _)) = text.char_indices().nth(LIMIT) {
        let clipped = text[..cut].trim_end_matches(python::blank);
        format!("{clipped}\n\n[Compact user values truncated for turn context.]")
    } else {
        text
    }
}

/// A substring pattern as SQLite LIKE reads it. Its fold is ASCII and the caller
/// supplies the surrounding percent signs, so this is always a substring search.
pub fn matches_like(text: &str, query: &str) -> bool {
    if query.contains('\0') {
        return false;
    }
    let text = text.to_ascii_lowercase();
    if !query.contains(['%', '_']) {
        return text.contains(query);
    }
    let mut pattern = String::from("(?s)");
    for sign in query.chars() {
        match sign {
            '%' => pattern.push_str(".*"),
            '_' => pattern.push('.'),
            sign => pattern.push_str(&regex::escape(&sign.to_string())),
        }
    }
    regex::Regex::new(&pattern).is_ok_and(|pattern| pattern.is_match(&text))
}

pub fn instruction(session: &str, document: &str) -> String {
    let orders = include_str!("teams/leader-standing-orders.txt");
    let helper = concat!(
        "## Decision Memory CLI\n",
        "- Search before asking: `clarp-admin leader-decision search --question \"...\" --tags tag1,tag2`\n",
        "- Covered: `clarp-admin leader-decision apply --decision-id dec_... --reason \"reused for this task\"`\n",
        "- Novel: ask once with recommended default/consequence, then `clarp-admin leader-decision log --question \"...\" --answer \"...\" --type workflow --tags tag1,tag2`\n",
        "- Durable user values: `clarp-admin leader-decision user-values --statement \"...\" --category preference --promote` or `--decision-id dec_... --promote`\n",
    );
    let session_line = if session.is_empty() {
        String::new()
    } else {
        format!("\nDelegate with `--from {session}`.")
    };
    format!(
        "{orders}\n\n{helper}{session_line}\n\n## Compact User Values\n{}",
        compact_user_values(document)
    )
}
