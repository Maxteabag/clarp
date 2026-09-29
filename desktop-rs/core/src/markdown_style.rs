//! How a chat message's Markdown looks (C++ `MarkdownStyle`): GitHub-flavoured
//! Markdown rendered straight to the rich-text HTML Qt Quick's `Text` lays
//! out, with sizes that suit a chat column. Headings scale with the body
//! (h1 1.3x, h2 1.18x, h3 1.08x, then bold body size) and are written as
//! paragraphs, so the viewer never applies its own heading scale on top;
//! code, quotes, lists, links and tables carry their styling inline, so the
//! text is laid out once at its final height. Raw HTML stays text.

use pulldown_cmark::{Alignment, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use serde_json::Value;

use crate::json::Object;

#[derive(Debug, Clone, PartialEq)]
pub struct StyleOptions {
    pub body_pixel_size: i32,
    pub mono_family: String,
    pub code_background: String,
    /// Empty keeps the body colour.
    pub code_text: String,
    pub quote_text: String,
    pub link: String,
    pub rule: String,
    /// Empty keeps the viewer's font.
    pub body_family: String,
}

impl Default for StyleOptions {
    fn default() -> Self {
        Self {
            body_pixel_size: 15,
            mono_family: "JetBrains Mono".into(),
            code_background: "#20212e".into(),
            code_text: String::new(),
            quote_text: "#9ca1bd".into(),
            link: "#82aaff".into(),
            rule: "#303342".into(),
            body_family: String::new(),
        }
    }
}

/// `#rgb`, `#rrggbb` or `#aarrggbb` (what QML's colour strings are).
fn valid_colour(value: &str) -> bool {
    value.strip_prefix('#').is_some_and(|hex| matches!(hex.len(), 3 | 6 | 8) && hex.chars().all(|c| c.is_ascii_hexdigit()))
}

/// Options from QML; invalid colours keep their defaults (C++
/// `markdownStyleOptions`).
pub fn options_from(values: &Object) -> StyleOptions {
    let mut options = StyleOptions::default();
    if let Some(size) = values.get("bodyPixelSize").and_then(Value::as_f64) {
        options.body_pixel_size = (size as i32).clamp(8, 64);
    }
    for (key, target) in [
        ("codeBackground", &mut options.code_background),
        ("codeText", &mut options.code_text),
        ("quoteText", &mut options.quote_text),
        ("link", &mut options.link),
        ("rule", &mut options.rule),
    ] {
        if let Some(value) = values.get(key).and_then(Value::as_str).filter(|v| valid_colour(v)) {
            *target = value.to_owned();
        }
    }
    if let Some(mono) = values.get("monoFamily").and_then(Value::as_str).filter(|v| !v.is_empty()) {
        options.mono_family = mono.to_owned();
    }
    options.body_family = values.get("bodyFamily").and_then(Value::as_str).unwrap_or_default().to_owned();
    options
}

/// A stable key for caching rendered HTML.
pub fn cache_key(markdown: &str, values: &Object) -> String {
    let mut parts: Vec<String> = values.iter().map(|(k, v)| format!("{k}={}", v.as_str().map_or_else(|| v.to_string(), str::to_owned))).collect();
    parts.sort();
    format!("{}|{markdown}", parts.join(";"))
}

fn heading_scale(level: HeadingLevel) -> f64 {
    match level {
        HeadingLevel::H1 => 1.3,
        HeadingLevel::H2 => 1.18,
        HeadingLevel::H3 => 1.08,
        _ => 1.0,
    }
}

fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(c),
        }
    }
    out
}

fn px(value: f64) -> String {
    let rounded = (value * 100.0).round() / 100.0;
    format!("{rounded}px")
}

struct Renderer<'a> {
    options: &'a StyleOptions,
    body: f64,
    code_size: i64,
    html: String,
    /// Blockquote depth; text inside takes the quote colour.
    quote: usize,
    /// List item depth; items hold their paragraphs without <p>.
    list_items: usize,
    item_paragraphs: Vec<usize>,
    in_code_block: bool,
    /// The code block's text, written line by line when it ends.
    code: String,
    table_head: bool,
    alignments: Vec<Alignment>,
    cell: usize,
    first_block: bool,
}

impl<'a> Renderer<'a> {
    fn new(options: &'a StyleOptions) -> Self {
        let body = f64::from(options.body_pixel_size);
        Self {
            options,
            body,
            code_size: ((body * 0.9).round() as i64).max(8),
            html: String::new(),
            quote: 0,
            list_items: 0,
            item_paragraphs: Vec::new(),
            in_code_block: false,
            code: String::new(),
            table_head: false,
            alignments: Vec::new(),
            cell: 0,
            first_block: true,
        }
    }

    fn paragraph_open(&mut self, top: f64, bottom: f64) {
        let indent = if self.quote > 0 { format!(" margin-left:{};", px(self.body * 0.9 * self.quote as f64)) } else { String::new() };
        self.html.push_str(&format!("<p style=\"margin-top:{}; margin-bottom:{};{indent}\">", px(top), px(bottom)));
        if self.quote > 0 {
            self.html.push_str(&format!("<span style=\"color:{};\">", self.options.quote_text));
        }
    }

    fn paragraph_close(&mut self) {
        if self.quote > 0 {
            self.html.push_str("</span>");
        }
        self.html.push_str("</p>\n");
    }

    fn mono_style(&self, inline: bool) -> String {
        let mut style = format!("font-family:'{}'; font-size:{}px;", self.options.mono_family, self.code_size);
        if inline {
            style.push_str(&format!(" background-color:{};", self.options.code_background));
        }
        if !self.options.code_text.is_empty() {
            style.push_str(&format!(" color:{};", self.options.code_text));
        }
        style
    }

    fn start(&mut self, tag: Tag) {
        match tag {
            Tag::Paragraph => {
                if let Some(count) = self.item_paragraphs.last_mut() {
                    // Items hold their text directly; later paragraphs break.
                    if *count > 0 {
                        self.html.push_str("<br />");
                    }
                    *count += 1;
                } else {
                    let (top, bottom) = (self.body * 0.15, self.body * 0.15);
                    self.paragraph_open(top, bottom);
                }
            }
            Tag::Heading { level, .. } => {
                let top = if self.first_block { 0.0 } else { self.body * 0.55 };
                self.paragraph_open(top, self.body * 0.2);
                let size = (self.body * heading_scale(level)).round() as i64;
                self.html.push_str(&format!("<span style=\"font-size:{size}px; font-weight:600;\">"));
            }
            Tag::BlockQuote(_) => self.quote += 1,
            Tag::CodeBlock(_) => {
                self.in_code_block = true;
                self.code.clear();
            }
            Tag::List(start) => {
                let margins = "margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-list-indent: 1;";
                match start {
                    Some(first) => self.html.push_str(&format!("<ol start=\"{first}\" style=\"{margins}\">")),
                    None => self.html.push_str(&format!("<ul style=\"{margins}\">")),
                }
            }
            Tag::Item => {
                self.list_items += 1;
                self.item_paragraphs.push(0);
                let color = if self.quote > 0 { format!(" color:{};", self.options.quote_text) } else { String::new() };
                self.html.push_str(&format!(
                    "<li style=\"margin-top:{}; margin-bottom:{};{color}\">",
                    px(self.body * 0.05),
                    px(self.body * 0.05)
                ));
            }
            Tag::Table(alignments) => {
                self.alignments = alignments;
                self.html.push_str(&format!(
                    "<table border=\"0.5\" cellspacing=\"0\" cellpadding=\"{}\" style=\"border-color:{}; border-style:solid;\">",
                    (self.body * 0.3).round(),
                    self.options.rule
                ));
            }
            Tag::TableHead => {
                self.table_head = true;
                self.cell = 0;
                self.html.push_str("<tr>");
            }
            Tag::TableRow => {
                self.cell = 0;
                self.html.push_str("<tr>");
            }
            Tag::TableCell => {
                let align = match self.alignments.get(self.cell) {
                    Some(Alignment::Center) => " align=\"center\"",
                    Some(Alignment::Right) => " align=\"right\"",
                    _ => "",
                };
                self.html.push_str(if self.table_head { "<th" } else { "<td" });
                self.html.push_str(align);
                self.html.push('>');
            }
            Tag::Emphasis => self.html.push_str("<i>"),
            Tag::Strong => self.html.push_str("<span style=\"font-weight:600;\">"),
            Tag::Strikethrough => self.html.push_str("<s>"),
            Tag::Link { dest_url, .. } => self.html.push_str(&format!(
                "<a href=\"{}\"><span style=\"color:{}; text-decoration: underline;\">",
                escape(&dest_url),
                self.options.link
            )),
            Tag::Image { dest_url, .. } => self.html.push_str(&format!("<img src=\"{}\" alt=\"", escape(&dest_url))),
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph => {
                if self.item_paragraphs.is_empty() {
                    self.paragraph_close();
                }
                self.first_block = false;
            }
            TagEnd::Heading(_) => {
                self.html.push_str("</span>");
                self.paragraph_close();
                self.first_block = false;
            }
            TagEnd::BlockQuote(_) => self.quote = self.quote.saturating_sub(1),
            TagEnd::CodeBlock => {
                self.in_code_block = false;
                // One block per line, like Qt's own import: a multi-line
                // <pre> would take its background on the first line only.
                let code = std::mem::take(&mut self.code);
                let style = self.mono_style(false);
                for line in code.strip_suffix('\n').unwrap_or(&code).split('\n') {
                    let text = if line.is_empty() { "&nbsp;".to_owned() } else { escape(line) };
                    self.html.push_str(&format!(
                        "<pre style=\"margin-top:0px; margin-bottom:0px; margin-left:{}; margin-right:{}; background-color:{};\"><span style=\"{style}\">{text}</span></pre>\n",
                        px(self.body * 0.6),
                        px(self.body * 0.4),
                        self.options.code_background,
                    ));
                }
                self.first_block = false;
            }
            TagEnd::List(ordered) => {
                self.html.push_str(if ordered { "</ol>\n" } else { "</ul>\n" });
                self.first_block = false;
            }
            TagEnd::Item => {
                self.list_items = self.list_items.saturating_sub(1);
                self.item_paragraphs.pop();
                self.html.push_str("</li>");
            }
            TagEnd::Table => {
                self.html.push_str("</table>\n");
                self.first_block = false;
            }
            TagEnd::TableHead => {
                self.table_head = false;
                self.html.push_str("</tr>");
            }
            TagEnd::TableRow => self.html.push_str("</tr>"),
            TagEnd::TableCell => {
                self.html.push_str(if self.table_head { "</th>" } else { "</td>" });
                self.cell += 1;
            }
            TagEnd::Emphasis => self.html.push_str("</i>"),
            TagEnd::Strong => self.html.push_str("</span>"),
            TagEnd::Strikethrough => self.html.push_str("</s>"),
            TagEnd::Link => self.html.push_str("</span></a>"),
            TagEnd::Image => self.html.push_str("\" />"),
            _ => {}
        }
    }

    fn text(&mut self, text: &str) {
        if self.in_code_block {
            self.code.push_str(text);
        } else {
            self.html.push_str(&escape(text));
        }
    }
}

/// Renders `markdown` as styled rich-text HTML.
pub fn styled_markdown_html(markdown: &str, options: &StyleOptions) -> String {
    let parser_options = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS;
    let mut renderer = Renderer::new(options);
    let family = if options.body_family.is_empty() { "sans-serif" } else { &options.body_family };
    renderer.html.push_str(&format!(
        "<html><body style=\"font-family:'{}'; font-size:{}px;\">\n",
        escape(family),
        options.body_pixel_size
    ));
    for event in Parser::new_ext(markdown, parser_options) {
        match event {
            Event::Start(tag) => renderer.start(tag),
            Event::End(tag) => renderer.end(tag),
            Event::Text(text) => renderer.text(&text),
            Event::Code(code) => {
                let style = renderer.mono_style(true);
                renderer.html.push_str(&format!("<span style=\"{style}\">{}</span>", escape(&code)));
            }
            // Chat text never renders as markup: raw HTML shows as written.
            Event::Html(html) | Event::InlineHtml(html) => renderer.text(&html),
            Event::SoftBreak => renderer.html.push(' '),
            Event::HardBreak => renderer.html.push_str("<br />"),
            Event::Rule => renderer.html.push_str("<hr />\n"),
            Event::TaskListMarker(done) => renderer.html.push_str(if done { "☑ " } else { "☐ " }),
            Event::FootnoteReference(name) => renderer.html.push_str(&escape(&format!("[{name}]"))),
            _ => {}
        }
    }
    renderer.html.push_str("</body></html>");
    renderer.html
}
