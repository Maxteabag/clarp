//! A message's Markdown as display blocks, for toolkits whose rich text
//! covers inline styling only (Slint's StyledText: bold, italics, code,
//! links, lists). Headings, code blocks, quotes, tables and rules become
//! blocks of their own; everything else keeps its Markdown for inline
//! rendering.

use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Block {
    /// Inline Markdown (paragraphs and lists).
    Prose(String),
    Heading { level: u8, markdown: String },
    Code { language: String, text: String },
    /// A quote's inner Markdown.
    Quote(String),
    /// Header row first; cells hold inline Markdown.
    Table { rows: Vec<Vec<String>>, source: String },
    Rule,
}

fn options() -> Options {
    Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS
}

/// Splits `markdown` into display blocks, keeping each block's source.
pub fn blocks(markdown: &str) -> Vec<Block> {
    let mut out = Vec::new();
    let mut prose_start: Option<usize> = None;
    let mut prose_end = 0usize;
    let flush = |out: &mut Vec<Block>, start: &mut Option<usize>, end: usize| {
        if let Some(from) = start.take() {
            let text = markdown[from..end].trim();
            if !text.is_empty() {
                out.push(Block::Prose(text.to_owned()));
            }
        }
    };
    let mut depth = 0usize;
    // After a code block lifted out of a list: the next content starts prose.
    let mut resume = false;
    let mut events = Parser::new_ext(markdown, options()).into_offset_iter().peekable();
    while let Some((event, range)) = events.next() {
        match event {
            Event::Start(Tag::Heading { level, .. }) if depth == 0 => {
                flush(&mut out, &mut prose_start, prose_end);
                let mut inner_end = range.end;
                let mut inner_start = None;
                for (event, inner) in events.by_ref() {
                    if matches!(event, Event::End(TagEnd::Heading(_))) {
                        break;
                    }
                    inner_start.get_or_insert(inner.start);
                    inner_end = inner.end;
                }
                let text = inner_start.map_or("", |s| &markdown[s..inner_end]).trim().to_owned();
                let level = match level {
                    HeadingLevel::H1 => 1,
                    HeadingLevel::H2 => 2,
                    HeadingLevel::H3 => 3,
                    HeadingLevel::H4 => 4,
                    HeadingLevel::H5 => 5,
                    HeadingLevel::H6 => 6,
                };
                out.push(Block::Heading { level, markdown: text });
            }
            Event::Start(Tag::CodeBlock(kind)) => {
                // Inside a list too: Slint's inline text has no code blocks,
                // and one left in a list's prose fails the whole list over to
                // plain text. The list's prose resumes after it.
                if depth > 0 {
                    prose_end = range.start;
                    resume = true;
                }
                flush(&mut out, &mut prose_start, prose_end);
                let language = match kind {
                    CodeBlockKind::Fenced(info) => info.split_whitespace().next().unwrap_or("").to_owned(),
                    CodeBlockKind::Indented => String::new(),
                };
                let mut text = String::new();
                for (event, _) in events.by_ref() {
                    match event {
                        Event::Text(t) => text.push_str(&t),
                        Event::End(TagEnd::CodeBlock) => break,
                        _ => {}
                    }
                }
                out.push(Block::Code { language, text: text.trim_end_matches('\n').to_owned() });
            }
            Event::Start(Tag::BlockQuote(_)) if depth == 0 => {
                flush(&mut out, &mut prose_start, prose_end);
                let mut nesting = 1;
                let mut end = range.end;
                for (event, inner) in events.by_ref() {
                    match event {
                        Event::Start(Tag::BlockQuote(_)) => nesting += 1,
                        Event::End(TagEnd::BlockQuote(_)) => {
                            nesting -= 1;
                            if nesting == 0 {
                                end = inner.end;
                                break;
                            }
                        }
                        _ => {}
                    }
                }
                let inner: Vec<&str> = markdown[range.start..end]
                    .lines()
                    .map(|line| line.trim_start().strip_prefix('>').map_or(line, |rest| rest.strip_prefix(' ').unwrap_or(rest)))
                    .collect();
                out.push(Block::Quote(inner.join("\n").trim().to_owned()));
            }
            Event::Start(Tag::Table(_)) if depth == 0 => {
                flush(&mut out, &mut prose_start, prose_end);
                let mut rows: Vec<Vec<String>> = Vec::new();
                let mut cell_start: Option<usize> = None;
                let mut cell_end = 0;
                for (event, inner) in events.by_ref() {
                    match event {
                        Event::Start(Tag::TableHead) | Event::Start(Tag::TableRow) => rows.push(Vec::new()),
                        Event::Start(Tag::TableCell) => cell_start = None,
                        Event::End(TagEnd::TableCell) => {
                            let text = cell_start.map_or("", |s| &markdown[s..cell_end]).trim().to_owned();
                            if let Some(row) = rows.last_mut() {
                                row.push(text);
                            }
                        }
                        Event::End(TagEnd::Table) => break,
                        _ => {
                            cell_start.get_or_insert(inner.start);
                            cell_end = inner.end;
                        }
                    }
                }
                out.push(Block::Table { rows, source: markdown[range].trim_end_matches(['\r', '\n']).to_owned() });
            }
            Event::Rule if depth == 0 => {
                flush(&mut out, &mut prose_start, prose_end);
                out.push(Block::Rule);
            }
            // Each top-level list item is a block of its own (with its own
            // marker: a lone "2." still reads as 2), so the chat's paragraph
            // spacing falls between items: Slint's text draws a paragraph
            // break as a bare line break, so inside one block they ran
            // together. Nested lists stay inside their item.
            Event::Start(Tag::Item) if depth == 1 => {
                if prose_start.is_some_and(|from| prose_end > from) {
                    flush(&mut out, &mut prose_start, prose_end);
                }
                prose_start = Some(range.start);
                resume = false;
                depth += 1;
            }
            Event::Start(_) => {
                if depth == 0 || resume {
                    prose_start.get_or_insert(range.start);
                    resume = false;
                }
                depth += 1;
            }
            Event::End(end) => {
                depth = depth.saturating_sub(1);
                if depth == 1 && matches!(end, TagEnd::Item) {
                    prose_end = range.end;
                }
                if depth == 0 {
                    prose_end = range.end;
                    // Each top-level paragraph (and the end of a list) closes
                    // its block, for the same reason.
                    flush(&mut out, &mut prose_start, prose_end);
                }
            }
            _ => {
                if depth == 0 || resume {
                    prose_start.get_or_insert(range.start);
                    resume = false;
                }
                if depth == 0 {
                    prose_end = range.end;
                }
            }
        }
    }
    flush(&mut out, &mut prose_start, prose_end);
    out
}

/// Where a bare web address in `text` starting at `start` ends: up to the
/// first space or angle bracket, without the sentence's closing punctuation
/// (a `)` stays when the address opened one).
fn url_end(text: &str, start: usize) -> usize {
    let rest = &text[start..];
    let mut end = start + rest.find(|c: char| c.is_whitespace() || matches!(c, '<' | '>' | '"' | '`')).unwrap_or(rest.len());
    loop {
        let url = &text[start..end];
        let Some(last) = url.chars().last() else { return end };
        let unbalanced = |open: char, close: char| last == close && url.matches(open).count() < url.matches(close).count();
        if matches!(last, '.' | ',' | ';' | ':' | '!' | '?' | '\'' | '*' | '_' | '~') || unbalanced('(', ')') || unbalanced('[', ']') {
            end -= last.len_utf8();
        } else {
            return end;
        }
    }
}

/// The bare web addresses in `text` as byte ranges, skipping `skip`ped ones.
fn bare_urls(text: &str, skip: &[std::ops::Range<usize>]) -> Vec<std::ops::Range<usize>> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(at) = text[from..].find("http").map(|i| i + from) {
        let scheme = ["https://", "http://"].iter().find(|s| text[at..].starts_with(*s)).map(|s| s.len());
        let boundary = text[..at].chars().last().is_none_or(|c| !c.is_alphanumeric());
        match scheme {
            Some(length) if boundary && !skip.iter().any(|r| r.contains(&at)) => {
                let end = url_end(text, at);
                if end > at + length {
                    found.push(at..end);
                }
                from = end.max(at + length);
            }
            _ => from = at + 4,
        }
    }
    found
}

/// `markdown` with its bare web addresses made links (`<https://…>`), as
/// GitHub shows them; addresses already in a link or code are left alone.
pub fn linkify(markdown: &str) -> String {
    let mut skip = Vec::new();
    let mut link_start = None;
    for (event, range) in Parser::new_ext(markdown, options()).into_offset_iter() {
        match event {
            Event::Start(Tag::Link { .. } | Tag::Image { .. }) => {
                link_start.get_or_insert(range.start);
            }
            Event::End(TagEnd::Link | TagEnd::Image) => {
                if let Some(start) = link_start.take() {
                    skip.push(start..range.end);
                }
            }
            Event::Code(_) | Event::Html(_) | Event::InlineHtml(_) => skip.push(range),
            Event::Start(Tag::CodeBlock(_)) => skip.push(range),
            _ => {}
        }
    }
    let mut out = String::with_capacity(markdown.len() + 16);
    let mut copied = 0;
    for url in bare_urls(markdown, &skip) {
        out.push_str(&markdown[copied..url.start]);
        out.push('<');
        out.push_str(&markdown[url.clone()]);
        out.push('>');
        copied = url.end;
    }
    out.push_str(&markdown[copied..]);
    out
}

/// Literal text (the user's own words) as Markdown whose only formatting is
/// its web addresses as links, or None when it has none. Every other
/// punctuation mark is escaped, so nothing else becomes Markdown; blank
/// lines and leading spaces are kept with no-break spaces.
pub fn literal_with_links(text: &str) -> Option<String> {
    let urls = bare_urls(text, &[]);
    if urls.is_empty() {
        return None;
    }
    Some(literal_markdown_with(text, urls))
}

/// Literal text as Markdown that shows it as typed (its web addresses as
/// links), with or without links: the find bar marks words in it.
pub fn literal_markdown(text: &str) -> String {
    literal_markdown_with(text, bare_urls(text, &[]))
}

fn literal_markdown_with(text: &str, urls: Vec<std::ops::Range<usize>>) -> String {
    let escape = |plain: &str, out: &mut String| {
        let mut line_start = out.is_empty() || out.ends_with('\n');
        for c in plain.chars() {
            match c {
                '\n' => {
                    // An empty line would end the paragraph and vanish.
                    if line_start {
                        out.push('\u{a0}');
                    }
                    out.push('\n');
                    line_start = true;
                    continue;
                }
                ' ' | '\t' if line_start => {
                    out.push('\u{a0}');
                    continue;
                }
                c if c.is_ascii_punctuation() => {
                    out.push('\\');
                    out.push(c);
                }
                c => out.push(c),
            }
            line_start = false;
        }
    };
    let mut out = String::with_capacity(text.len() * 2);
    let mut copied = 0;
    for url in urls {
        escape(&text[copied..url.start], &mut out);
        out.push('<');
        out.push_str(&text[url.clone()]);
        out.push('>');
        copied = url.end;
    }
    escape(&text[copied..], &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reply_splits_into_the_blocks_it_shows() {
        let markdown = "## Build plan\n\nRun `cargo test` first, then read the [guide](https://example.com).\n\n```rust\nfn main() {\n    println!(\"hi\");\n}\n```\n\n> Keep the **main** checkout untouched.\n\n- core models\n- **Qt bridges**\n\n| step | state |\n|---|---|\n| port | done |\n| verify | *running* |\n\n---\n\nThe end.";
        let blocks = blocks(markdown);
        assert_eq!(
            blocks,
            vec![
                Block::Heading { level: 2, markdown: "Build plan".into() },
                Block::Prose("Run `cargo test` first, then read the [guide](https://example.com).".into()),
                Block::Code { language: "rust".into(), text: "fn main() {\n    println!(\"hi\");\n}".into() },
                Block::Quote("Keep the **main** checkout untouched.".into()),
                Block::Prose("- core models".into()),
                Block::Prose("- **Qt bridges**".into()),
                Block::Table { source: "| step | state |\n|---|---|\n| port | done |\n| verify | *running* |".into(), rows: vec![
                    vec!["step".into(), "state".into()],
                    vec!["port".into(), "done".into()],
                    vec!["verify".into(), "*running*".into()],
                ] },
                Block::Rule,
                Block::Prose("The end.".into()),
            ]
        );
    }

    #[test]
    fn a_code_block_inside_a_list_is_a_block_of_its_own() {
        // Slint's StyledText has no code blocks: one left in a list's prose
        // fails the whole list over to plain text (raw ** and backticks).
        let markdown = "Steps:\n\n1. Switch the profile:\n   ```bash\n   hotseat switch personal\n   ```\n2. Verify it:\n   - **Session:** `orion`";
        assert_eq!(
            blocks(markdown),
            vec![
                Block::Prose("Steps:".into()),
                Block::Prose("1. Switch the profile:".into()),
                Block::Code { language: "bash".into(), text: "hotseat switch personal".into() },
                Block::Prose("2. Verify it:\n   - **Session:** `orion`".into()),
            ]
        );
    }

    #[test]
    fn plain_text_is_one_prose_block() {
        assert_eq!(blocks("Hello there"), vec![Block::Prose("Hello there".into())]);
        assert_eq!(blocks(""), vec![]);
        assert_eq!(blocks("One\n\nTwo"), vec![Block::Prose("One".into()), Block::Prose("Two".into())]);
    }

    /// Peter, 23 Nov: "the paragraph spaces not showing". Slint draws a
    /// paragraph break inside one block as a bare line break, so every
    /// paragraph and every top-level list item is a block of its own and the
    /// chat's paragraph spacing falls between them. Numbers stay (a lone "2."
    /// reads as 2), and a nested list stays inside its item.
    #[test]
    fn paragraphs_and_list_items_are_blocks_of_their_own() {
        let markdown = "**The cause is the cache.**\n\nHere is why:\n\n1. It keeps the old order.\n2. It is saved on close.\n   - even when the send failed\n3. Opening reads it first.\n\nThat is all.";
        assert_eq!(
            blocks(markdown),
            vec![
                Block::Prose("**The cause is the cache.**".into()),
                Block::Prose("Here is why:".into()),
                Block::Prose("1. It keeps the old order.".into()),
                Block::Prose("2. It is saved on close.\n   - even when the send failed".into()),
                Block::Prose("3. Opening reads it first.".into()),
                Block::Prose("That is all.".into()),
            ]
        );
        // A loose list (blank lines between items) splits the same way.
        assert_eq!(blocks("- one\n\n- two"), vec![Block::Prose("- one".into()), Block::Prose("- two".into())]);
    }

    #[test]
    fn bare_web_addresses_become_links() {
        assert_eq!(linkify("Raw: https://bare.example/path?x=1."), "Raw: <https://bare.example/path?x=1>.");
        assert_eq!(linkify("See (https://example.com/a_(b)) now"), "See (<https://example.com/a_(b)>) now");
        assert_eq!(linkify("**https://example.com/x**"), "**<https://example.com/x>**");
        // Already links, or code: untouched.
        let kept = "[guide](https://example.com/guide), <https://example.com/a>, `https://example.com/code` and [https://x.example](https://x.example)";
        assert_eq!(linkify(kept), kept);
        assert_eq!(linkify("no links here, just http talk"), "no links here, just http talk");
        assert_eq!(linkify("xhttps://example.com"), "xhttps://example.com", "only at a word's start");
    }

    #[test]
    fn literal_text_keeps_its_characters_and_gets_links() {
        assert_eq!(literal_with_links("no address *here*"), None);
        let markdown = literal_with_links("Also check https://user.example/page please, **now**\n\n  - ok").expect("has a link");
        assert_eq!(markdown, "Also check <https://user.example/page> please\\, \\*\\*now\\*\\*\n\u{a0}\n\u{a0}\u{a0}\\- ok");
    }
}
