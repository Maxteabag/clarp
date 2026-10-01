//! Transcript and preview text rules. Port of the free functions in
//! the C++ client's `ProtocolTypes.cpp`.

use std::sync::LazyLock;

use fancy_regex::{Captures, Regex};
use serde_json::Value;

use crate::json::Object;

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static pattern compiles")
}

fn replace_all(regex: &Regex, text: &str, replacement: &str) -> String {
    regex.replace_all(text, replacement).into_owned()
}

fn starts_with_ignore_ascii_case(text: &str, prefix: &str) -> bool {
    text.len() >= prefix.len()
        && text.is_char_boundary(prefix.len())
        && text[..prefix.len()].eq_ignore_ascii_case(prefix)
}

/// Case-insensitive first, like QString::localeAwareCompare for en_US names
/// (not full ICU collation).
pub fn name_order(left: &str, right: &str) -> std::cmp::Ordering {
    left.to_lowercase().cmp(&right.to_lowercase()).then_with(|| left.cmp(right))
}

/// Qt's QString::simplified: trim, then collapse inner whitespace runs.
pub fn simplified(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Qt's QString::toHtmlEscaped.
pub fn html_escaped(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '&' => out.push_str("&amp;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(c),
        }
    }
    out
}

const FILLER: char = '\u{E000}';
const PAUSE: char = '\u{E001}';

/// The written form of a reply: voice markup removed, and the gaps a spoken
/// pause or filler leaves closed only where they were. A still-streaming
/// reply also hides a voice tag that has not finished arriving.
pub fn cleaned_display_text(text: &str, streaming: bool) -> String {
    static VOX_BLOCK: LazyLock<Regex> = LazyLock::new(|| re(r"(?is)<vox\b[^>]*>.*?</vox>"));
    static SPEAK_TAG: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)</?speak\b[^>]*>"));
    static AUDIO_TAG: LazyLock<Regex> =
        LazyLock::new(|| re(r"(?i)</?(?:break|speed|volume|emotion)\b[^>]*/?>"));
    // Spoken-only parts leave gaps in the written text: a pause often
    // stands in for a comma ("Sure <break/> here's") and a filler sits
    // between two ("lines, <vox>um</vox>, comma"). Mark where they were,
    // then close the gap there only; spacing elsewhere (code, tables) stays.
    static PAUSE_TAG: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)[ \t]*<break\b[^>]*/?>[ \t]*"));
    static SPOKEN_BLOCK: LazyLock<Regex> =
        LazyLock::new(|| re(r"(?is)[ \t]*<vox\b[^>]*>.*?</vox>[ \t]*"));
    static DOUBLED_MARK: LazyLock<Regex> = LazyLock::new(|| re(r"([,;:])\x{E000}[,;:]"));
    static BEFORE_MARK: LazyLock<Regex> =
        LazyLock::new(|| re(r"(?<=\S)[\x{E000}\x{E001}]+(?=[,.;:!?)])"));
    static WORD_PAUSE: LazyLock<Regex> = LazyLock::new(|| {
        re(r"(?<=[\p{L}\p{N}])[\x{E000}\x{E001}]*\x{E001}[\x{E000}\x{E001}]*(?=[\p{L}\p{N}])")
    });
    static BETWEEN: LazyLock<Regex> = LazyLock::new(|| re(r"(?<=\S)[\x{E000}\x{E001}]+(?=\S)"));
    static LEFTOVER: LazyLock<Regex> = LazyLock::new(|| re(r"[\x{E000}\x{E001}]+"));

    let mut text = replace_all(&SPOKEN_BLOCK, text, &FILLER.to_string());
    text = replace_all(&PAUSE_TAG, &text, &PAUSE.to_string());
    text = replace_all(&VOX_BLOCK, &text, "");
    text = replace_all(&SPEAK_TAG, &text, "");
    text = replace_all(&AUDIO_TAG, &text, "");
    text = replace_all(&DOUBLED_MARK, &text, "$1");
    text = replace_all(&BEFORE_MARK, &text, "");
    text = replace_all(&WORD_PAUSE, &text, ", ");
    text = replace_all(&BETWEEN, &text, " ");
    text = replace_all(&LEFTOVER, &text, "");
    if streaming {
        let lower = text.to_ascii_lowercase();
        let open_vox = lower.rfind("<vox");
        let close_vox = lower.rfind("</vox>");
        if let Some(open) = open_vox.filter(|open| close_vox.is_none_or(|close| *open > close)) {
            text.truncate(open);
        }
        if let Some(marker) = text.rfind('<') {
            let tail = text[marker..].to_lowercase();
            const PREFIXES: [&str; 12] = [
                "<speak", "</speak", "<vox", "</vox", "<break", "</break", "<speed", "</speed",
                "<volume", "</volume", "<emotion", "</emotion",
            ];
            let incomplete = !tail.contains('>')
                && PREFIXES.iter().any(|p| p.starts_with(&tail) || tail.starts_with(p));
            if incomplete {
                text.truncate(marker);
            }
        }
    }
    text.trim().to_owned()
}

/// One line of plain text for a sidebar preview: markdown tables, headings,
/// quotes, list markers, emphasis, code fences and link syntax are removed so
/// a reply that opens with a table reads as words, not pipes and dashes.
pub fn plain_preview_text(markdown: &str) -> String {
    // Also matches a separator the Host cut short with an ellipsis.
    static TABLE_SEPARATOR: LazyLock<Regex> = LazyLock::new(|| re(r"^[|:\s…-]*--[|:\s…-]*$"));
    static LINE_PREFIX: LazyLock<Regex> = LazyLock::new(|| re(r"^(#{1,6}\s+|>\s*|[-*+]\s+)"));
    static LINK: LazyLock<Regex> = LazyLock::new(|| re(r"!?\[([^\]]*)\]\([^)]*\)"));
    static EMPHASIS: LazyLock<Regex> = LazyLock::new(|| re(r"(\*\*|__|~~|`)"));
    static SINGLE_EMPHASIS: LazyLock<Regex> =
        LazyLock::new(|| re(r"(^|\W)[*_](\S(?:[^*_]*\S)?)[*_](?=\W|$)"));
    let mut parts = Vec::new();
    for line in markdown.split('\n') {
        let line = line.trim();
        if line.starts_with("```") || TABLE_SEPARATOR.is_match(line).unwrap_or(false) {
            continue;
        }
        let mut line = replace_all(&LINE_PREFIX, line, "");
        if line.starts_with('|') || line.ends_with('|') {
            line = line.replace('|', " ");
        }
        if !line.is_empty() {
            parts.push(line);
        }
    }
    let mut text = parts.join(" ");
    text = replace_all(&LINK, &text, "$1");
    text = replace_all(&EMPHASIS, &text, "");
    text = replace_all(&SINGLE_EMPHASIS, &text, "$1$2");
    simplified(&text)
}

/// Links reach the transcript from model output, tool results and fetched web
/// pages, so the scheme is never trustworthy. Only hand the desktop handler the
/// schemes a chat link legitimately needs; anything else stays inert text.
pub fn is_openable_link(link: &str) -> bool {
    let link = link.trim();
    let Some((scheme, rest)) = link.split_once(':') else {
        return false;
    };
    let Ok(url) = url::Url::parse(link) else {
        return false;
    };
    match scheme.to_ascii_lowercase().as_str() {
        "mailto" => !url.path().is_empty(),
        // A host is required so "https:///etc/passwd" cannot reach the handler.
        // The WHATWG parser would repair that into host "etc", so check the
        // authority as written, as Qt's strict parser does.
        "http" | "https" => {
            rest.strip_prefix("//").is_some_and(|authority| {
                !authority.is_empty() && !authority.starts_with(['/', '?', '#'])
            }) && url.host_str().is_some_and(|host| !host.is_empty())
        }
        _ => false,
    }
}

/// Verbatim output can be a whole file. Above this size the caller keeps the
/// cheaper plain-text path rather than building a rich-text document.
pub const MAX_LINKIFIED_TEXT_LENGTH: usize = 20_000;

/// Wrap the URLs inside verbatim tool output in anchors without altering the
/// text itself. The result is Qt rich text: every original character is escaped
/// and `white-space: pre-wrap` keeps indentation and line breaks.
pub fn linkified_plain_text(text: &str) -> String {
    static CANDIDATE: LazyLock<Regex> = LazyLock::new(|| re(r#"(?:https?://|www\.)[^\s<>"']+"#));
    let mut result = String::with_capacity(text.len() + 64);
    result.push_str("<div style=\"white-space: pre-wrap;\">");
    let mut cursor = 0;
    for found in CANDIDATE.find_iter(text).filter_map(Result::ok) {
        // Stop before the closing punctuation that usually follows a URL in
        // prose so a trailing ")" or "." is not swallowed into the target.
        let url = found.as_str().trim_end_matches(|c| ".,;:!?)]}'\"".contains(c));
        if url.is_empty() {
            continue;
        }
        let target =
            if url.starts_with("www.") { format!("https://{url}") } else { url.to_owned() };
        if !is_openable_link(&target) {
            continue;
        }
        result += &html_escaped(&text[cursor..found.start()]);
        result += &format!("<a href=\"{}\">{}</a>", html_escaped(&target), html_escaped(url));
        cursor = found.start() + url.len();
    }
    result += &html_escaped(&text[cursor..]);
    result.push_str("</div>");
    result
}

/// md4c's permissive autolinker does not recognise a URL with an explicit port,
/// so wrap the ones it would miss in CommonMark autolink brackets. Code spans,
/// fenced blocks, indented code and existing links are left untouched.
pub fn markdown_with_explicit_autolinks(markdown: &str) -> String {
    static PORTED: LazyLock<Regex> = LazyLock::new(|| {
        re(r#"https?://[^\s<>"'`\]\)]*:\d{1,5}(?:/[^\s<>"'`\]\)]*)?"#)
    });
    let mut output = Vec::new();
    let mut in_fence = false;
    for line in markdown.split('\n') {
        let trimmed = line.trim();
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_fence = !in_fence;
            output.push(line.to_owned());
            continue;
        }
        if in_fence || line.starts_with("    ") || line.starts_with('\t') {
            output.push(line.to_owned());
            continue;
        }
        let mut rewritten = String::with_capacity(line.len() + 16);
        let mut cursor = 0;
        let mut in_code_span = false;
        for found in PORTED.find_iter(line).filter_map(Result::ok) {
            let before = &line[cursor..found.start()];
            // Track backtick parity so a URL inside `code` is left alone.
            in_code_span ^= before.matches('`').count() % 2 != 0;
            rewritten += before;
            let preceding = line[..found.start()].chars().next_back().unwrap_or(' ');
            // Already a markdown link target, an existing autolink, or code.
            if in_code_span || preceding == '(' || preceding == '<' {
                rewritten += found.as_str();
            } else {
                rewritten += &format!("<{}>", found.as_str());
            }
            cursor = found.end();
        }
        rewritten += &line[cursor..];
        output.push(rewritten);
    }
    output.join("\n")
}

/// True when an artifact body should be rendered as HTML rather than Markdown.
pub fn looks_like_html_report(content: &str) -> bool {
    static MARKUP: LazyLock<Regex> = LazyLock::new(|| {
        re(r"(?i)<(!doctype\s+html|html|head|body|div|table|h[1-6]|p|ul|ol|section|article|style)\b")
    });
    MARKUP.is_match(content).unwrap_or(false)
}

/// A resource may load only if it carries its own bytes or comes from the Host
/// the user is already authenticated against.
fn report_resource_allowed(reference: &str, host_origin: &str) -> bool {
    let mut reference = reference.trim();
    while reference.len() >= 2
        && ((reference.starts_with('"') && reference.ends_with('"'))
            || (reference.starts_with('\'') && reference.ends_with('\'')))
    {
        reference = reference[1..reference.len() - 1].trim();
    }
    if reference.is_empty() {
        return false;
    }
    starts_with_ignore_ascii_case(reference, "data:")
        || (!host_origin.is_empty() && starts_with_ignore_ascii_case(reference, host_origin))
}

/// Qt's rich text engine renders a useful subset of HTML with no web engine but
/// it does fetch remote resources: `<img src>`, `<table background>`, CSS
/// `url(...)` and `@import`. Rewrite every reference that is not
/// self-contained (`data:`) or served by the user's own Host.
pub fn sanitized_report_html(html: &str, host_origin: &str) -> String {
    static SCRIPTS: LazyLock<Regex> = LazyLock::new(|| re(r"(?is)<script\b[^>]*>.*?</script\s*>"));
    static CSS_IMPORT: LazyLock<Regex> = LazyLock::new(|| re(r"(?is)@import\s+[^;}]*;?"));
    static CSS_URL: LazyLock<Regex> = LazyLock::new(|| re(r"(?is)url\(\s*([^)]*)\s*\)"));
    static RESOURCE_ATTRIBUTE: LazyLock<Regex> = LazyLock::new(|| {
        re(r#"(?is)\b(src|background)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)"#)
    });
    let mut result = replace_all(&SCRIPTS, html, "");
    result = replace_all(&CSS_IMPORT, &result, "");
    result = CSS_URL
        .replace_all(&result, |caps: &Captures| {
            if report_resource_allowed(&caps[1], host_origin) {
                caps[0].to_owned()
            } else {
                // "none" keeps the declaration valid without a fetch.
                "none".to_owned()
            }
        })
        .into_owned();
    RESOURCE_ATTRIBUTE
        .replace_all(&result, |caps: &Captures| {
            if report_resource_allowed(&caps[2], host_origin) {
                caps[0].to_owned()
            } else {
                // Keep the attribute present but unresolvable, so the layout
                // still reserves the element and nothing leaves the machine.
                format!("{}=\"\"", &caps[1])
            }
        })
        .into_owned()
}

/// Split markdown into the blocks the transcript renders separately: blank
/// lines end a block, except inside a fence or between items of one list.
pub fn markdown_display_blocks(markdown: &str) -> Vec<String> {
    static BULLET: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*[-+*]\s+\S"));
    static ORDERED: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*\d+[.)]\s+\S"));
    let normalized = markdown.replace("\r\n", "\n").replace('\r', "\n");
    if normalized.trim().is_empty() {
        return Vec::new();
    }
    let list_kind = |line: &str| -> u8 {
        if BULLET.is_match(line).unwrap_or(false) {
            1
        } else if ORDERED.is_match(line).unwrap_or(false) {
            2
        } else {
            0
        }
    };
    let lines: Vec<&str> = normalized.split('\n').collect();
    let mut blocks = Vec::new();
    let mut current: Vec<String> = Vec::new();
    let flush = |current: &mut Vec<String>, blocks: &mut Vec<String>| {
        while current.last().is_some_and(|line| line.trim().is_empty()) {
            current.pop();
        }
        if !current.is_empty() {
            blocks.push(current.join("\n"));
            current.clear();
        }
    };
    let mut fence = String::new();
    for (index, line) in lines.iter().enumerate() {
        let trimmed = line.trim();
        if !fence.is_empty() {
            current.push((*line).to_owned());
            if trimmed.starts_with(&fence) {
                fence.clear();
            }
            continue;
        }
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            fence = trimmed[..3].to_owned();
            current.push((*line).to_owned());
            continue;
        }
        if !trimmed.is_empty() {
            current.push((*line).to_owned());
            continue;
        }
        let next = lines[index + 1..].iter().find(|l| !l.trim().is_empty());
        let previous_kind = current
            .iter()
            .rev()
            .find(|l| !l.trim().is_empty())
            .map_or(0, |l| list_kind(l));
        let next_kind = next.map_or(0, |l| list_kind(l));
        if previous_kind != 0 && previous_kind == next_kind {
            if current.last().is_some_and(|l| !l.is_empty()) {
                current.push(String::new());
            }
        } else {
            flush(&mut current, &mut blocks);
        }
    }
    flush(&mut current, &mut blocks);
    blocks
}


/// Report artifacts whose body the desktop shows itself.
pub fn report_type_has_body(kind: &str) -> bool {
    matches!(kind, "document" | "research" | "html_form")
}

pub fn artifact_is_viewable_report(artifact: &Object) -> bool {
    report_type_has_body(&crate::json::string(artifact, "type")) && !crate::json::string(artifact, "content").trim().is_empty()
}

/// What the report view shows for an artifact: HTML is sanitised against
/// the Host (only it can reference remote resources); Markdown goes to the
/// Markdown reader unchanged. None when it is not a viewable report.
pub fn report_for_artifact(artifacts: &[Value], artifact_id: &str, host_origin: &str) -> Option<Object> {
    let artifact = artifacts.iter().filter_map(Value::as_object).find(|a| crate::json::string(a, "artifact_id") == artifact_id)?;
    if !artifact_is_viewable_report(artifact) {
        return None;
    }
    let content = crate::json::string(artifact, "content");
    let html = looks_like_html_report(&content);
    let field = |key: &str| artifact.get(key).cloned().unwrap_or(Value::Null);
    let report = serde_json::json!({
        "artifact_id": artifact_id, "title": field("title"), "summary": field("summary"), "type": field("type"),
        "session": field("session"), "isHtml": html,
        "body": if html { sanitized_report_html(&content, host_origin) } else { content },
    });
    report.as_object().cloned()
}
